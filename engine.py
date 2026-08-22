"""Reconciliation + fee-verification engine.

Hard rule: no LLM ever touches a number. Every amount, match, tolerance and
confidence score below is deterministic Python. The LLM (see explain.py) may
only rewrite an already-computed exception into English.

All money is integer paise. No floats anywhere in the money path.
"""
import csv
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).parent


# ---------------------------------------------------------------- fee math

def fees(gross_paise, method, contract, mdr_bps=None):
    """Fee breakdown the contract says this transaction should carry."""
    bps = contract["mdr_bps"][method] if mdr_bps is None else mdr_bps
    mdr = (gross_paise * bps + 5000) // 10000
    platform = (gross_paise * contract["platform_fee_bps"] + 5000) // 10000
    gst = ((mdr + platform) * contract["gst_bps"] + 5000) // 10000
    return {"mdr": mdr, "platform": platform, "gst": gst,
            "net": gross_paise - mdr - platform - gst}


def rupees(paise):
    return "₹{:,.2f}".format(paise / 100)


# ------------------------------------------------------------ data model

@dataclass
class Order:
    order_id: str
    customer_id: str
    method: str
    gross_paise: int
    order_date: date
    status: str          # captured | refunded


@dataclass(eq=False)
class Settlement:
    settlement_id: str
    utr: str
    order_ref: str       # blank on batch deposits and on bank-side orphans
    customer_ref: str    # blank on batch deposits
    method: str
    gross_paise: int
    mdr_paise: int
    platform_fee_paise: int
    gst_paise: int
    net_paise: int
    settled_date: date
    txn_type: str        # credit | refund | batch


@dataclass
class Match:
    order_ids: list
    settlement_id: str
    pass_name: str       # exact | fuzzy | batch
    confidence: float
    signals: list = field(default_factory=list)

    @property
    def confident(self):
        return self.confidence >= THRESHOLD


@dataclass
class Exception_:
    code: str
    record_type: str     # order | settlement | match
    record_id: str
    amount_paise: int
    age_days: int
    explanation: str
    action: str
    llm_explanation: str = ""   # set only by explain.py, never used in any calculation
    llm_category: str = ""

    @property
    def risk_score(self):
        # rank by money at risk x how long it has been at risk, never by count
        return self.amount_paise * max(self.age_days, 1)


THRESHOLD = 0.7  # overwritten from contract.json on load


# ------------------------------------------------------------ ingest

def load(data_dir=ROOT / "data", contract_path=ROOT / "contract.json"):
    global THRESHOLD
    contract = json.loads(Path(contract_path).read_text())
    THRESHOLD = contract["confidence_threshold"]
    data_dir = Path(data_dir)

    with open(data_dir / "orders.csv", newline="") as f:
        orders = [Order(r["order_id"], r["customer_id"], r["method"].strip().lower(),
                        int(r["gross_paise"]), date.fromisoformat(r["order_date"]),
                        r["status"])
                  for r in csv.DictReader(f)]

    with open(data_dir / "settlement.csv", newline="") as f:
        setts = [Settlement(r["settlement_id"], r["utr"], r["order_ref"].strip(),
                            r["customer_ref"].strip(), r["method"].strip().lower(),
                            int(r["gross_paise"]), int(r["mdr_paise"]),
                            int(r["platform_fee_paise"]), int(r["gst_paise"]),
                            int(r["net_paise"]), date.fromisoformat(r["settled_date"]),
                            r["txn_type"])
                 for r in csv.DictReader(f)]

    return orders, setts, contract


# ------------------------------------------------------------ matching

def match_exact(orders, setts, contract, open_o, open_s):
    """Pass 1: the bank line carries the order reference and the gross agrees."""
    by_id = {o.order_id: o for o in orders}
    out = []
    for s in list(open_s):
        o = by_id.get(s.order_ref)
        if (o and o.order_id in open_o and o.gross_paise == s.gross_paise
                and (s.txn_type == "refund") == (o.status == "refunded")):
            out.append(Match([o.order_id], s.settlement_id, "exact", 1.0,
                             ["order reference present on the bank line",
                              "gross amount identical"]))
            open_o.discard(o.order_id)
            open_s.remove(s)
    return out


def _signal_hits(o, s):
    return (o.method == s.method) + bool(s.customer_ref and s.customer_ref == o.customer_id)


def match_fuzzy(orders, setts, contract, open_o, open_s):
    """Pass 2: no reference. Match on amount, then challenge every other signal."""
    by_id = {o.order_id: o for o in orders}
    out = []
    for s in list(open_s):
        if s.txn_type == "batch":
            continue
        limit = contract["max_fuzzy_gap_days"]
        if s.txn_type == "refund":
            limit = contract["refund_window_days"]
        cands = [by_id[oid] for oid in sorted(open_o)
                 if by_id[oid].gross_paise == s.gross_paise
                 and (s.txn_type == "refund") == (by_id[oid].status == "refunded")
                 and 0 <= (s.settled_date - by_id[oid].order_date).days <= limit]
        if not cands:
            continue
        # prefer the candidate agreeing on the most non-amount signals
        cands.sort(key=lambda o: (-_signal_hits(o, s),
                                  abs((s.settled_date - o.order_date).days),
                                  o.order_id))  # order_id breaks exact ties, so a
        # genuinely ambiguous pair resolves the same way on every run
        best = cands[0]
        # only candidates tied on the BEST non-amount evidence are truly
        # ambiguous -- a customer reference that singles one out settles it
        top = _signal_hits(best, s)
        tied = sum(1 for c in cands if _signal_hits(c, s) == top)
        conf, signals = challenge(best, s, contract, tied)
        out.append(Match([best.order_id], s.settlement_id, "fuzzy", conf, signals))
        open_o.discard(best.order_id)
        open_s.remove(s)
    return out


def challenge(o, s, contract, n_candidates):
    """Self-verification: assume the match is wrong and see what survives.

    Amount agreement alone is worth almost nothing -- two unrelated records can
    trivially share an amount. Confidence is what is left after every
    disagreeing signal has taken its cut.
    """
    conf, signals = 1.0, []
    gap = (s.settled_date - o.order_date).days
    window = (contract["refund_window_days"] if s.txn_type == "refund"
              else contract["settlement_days"][o.method])

    if gap < 0:
        conf -= 0.6
        signals.append("bank credited {}d BEFORE the order existed".format(abs(gap)))
    elif gap > window:
        over = gap - window
        conf -= min(0.10 * over, 0.30)
        signals.append("settled T+{}, contract window is T+{} ({}d late)".format(gap, window, over))
    else:
        signals.append("settled T+{}, inside the T+{} window".format(gap, window))

    if o.method != s.method:
        conf -= 0.3
        signals.append("instrument disagrees: order={}, bank={}".format(o.method, s.method))
    else:
        signals.append("instrument agrees ({})".format(o.method))

    if s.customer_ref:
        if s.customer_ref != o.customer_id:
            conf -= 0.3
            signals.append("customer disagrees: order={}, bank={}".format(o.customer_id, s.customer_ref))
        else:
            signals.append("customer agrees ({})".format(o.customer_id))
    else:
        conf -= 0.05
        signals.append("bank line carries no customer reference")

    if n_candidates > 1:
        # two records that fit equally well cannot both be this credit, and
        # nothing in the file says which. Asserting either one is a coin flip,
        # so this is a ceiling, not a deduction.
        conf = min(conf - 0.25, 0.55)
        signals.append("amount is not unique: {} open orders fit {} equally well".format(
            n_candidates, rupees(o.gross_paise)))

    return round(max(conf, 0.0), 2), signals


def _subset_sum(items, target, skip=None):
    """One subset of (id, value) summing exactly to target, or None."""
    reach = {0: ()}
    for oid, val in items:
        if oid == skip or val > target:
            continue
        for total, combo in list(reach.items()):
            nt = total + val
            if nt <= target and nt not in reach:
                reach[nt] = combo + (oid,)
        if target in reach:
            break
    return reach.get(target)


def match_batch(orders, setts, contract, open_o, open_s):
    """Pass 3: a net lump-sum deposit with no per-transaction breakdown.

    Back the contract fee structure out of every open order, then look for the
    combination whose expected NET sums to the deposit.
    """
    by_id = {o.order_id: o for o in orders}
    out = []
    for s in list(open_s):
        if s.txn_type != "batch":
            continue
        cands = sorted(
            (oid, fees(by_id[oid].gross_paise, by_id[oid].method, contract)["net"])
            for oid in sorted(open_o)
            if by_id[oid].status == "captured"
            and 0 <= (s.settled_date - by_id[oid].order_date).days
            <= contract["max_fuzzy_gap_days"])
        combo = _subset_sum(cands, s.net_paise)
        if not combo:
            continue
        # uniqueness challenge: another, different way to hit the same total?
        unique = _subset_sum(cands, s.net_paise, skip=combo[0]) is None
        out.append(Match(list(combo), s.settlement_id, "batch", 0.9 if unique else 0.55,
                         ["{} open orders reconstruct the deposit net exactly".format(len(combo)),
                          "no other combination reaches this total" if unique else
                          "another combination also reaches this total -- decomposition is ambiguous"]))
        open_o.difference_update(combo)
        open_s.remove(s)
    return out


# ------------------------------------------------------------ fee verification

def verify_fees(matches, orders, setts, contract, scan=None):
    """Second loop: what the contract says the fee should be vs what was cut.

    The statutory 0% MDR check runs on every bank line, matched or not -- it
    needs nothing from our side of the file. The contract-rate check runs only
    on matches we actually believe; pricing a match we have already demoted
    would be arithmetic on a guess.

    `scan` narrows which bank lines get the 0% MDR check. It defaults to all of
    them, which is what the batch report wants. A long-running live book passes
    only the lines that arrived this tick, because a fee finding never changes
    once computed -- re-pricing the whole book every tick is what made engine
    time grow with the length of the demo.
    """
    by_o = {o.order_id: o for o in orders}
    findings = []
    flagged = set()

    for s in (setts if scan is None else scan):
        if s.method in contract["zero_mdr_mandated"] and s.mdr_paise > 0:
            gst_on_mdr = (s.mdr_paise * contract["gst_bps"] + 5000) // 10000
            flagged.add(s.settlement_id)
            findings.append(dict(
                code="ZERO_MDR_VIOLATION", settlement=s, orders=[],
                expected_mdr=0, actual_mdr=s.mdr_paise,
                overcharge=s.mdr_paise + gst_on_mdr,
                detail="{} carries a statutory 0% MDR; {} was still deducted "
                       "(plus {} GST on it)".format(s.method, rupees(s.mdr_paise),
                                                    rupees(gst_on_mdr))))

    by_s = {s.settlement_id: s for s in setts}
    for m in matches:
        if not m.confident:
            continue
        # dict, not a scan: this ran once per match over every settlement, which
        # is the O(matches x settlements) term that dominated a long live run
        s = by_s[m.settlement_id]
        if s.txn_type == "refund" or s.settlement_id in flagged:
            continue
        exp = {"mdr": 0, "platform": 0, "gst": 0}
        for oid in m.order_ids:
            o = by_o[oid]
            e = fees(o.gross_paise, o.method, contract)
            for k in exp:
                exp[k] += e[k]
        methods = {by_o[oid].method for oid in m.order_ids}
        gross = sum(by_o[oid].gross_paise for oid in m.order_ids)
        # platform fee is legitimate revenue, but it is summed in here too so a
        # gateway cannot move an MDR overcharge into it and hide
        total_delta = ((s.mdr_paise + s.platform_fee_paise + s.gst_paise)
                       - (exp["mdr"] + exp["platform"] + exp["gst"]))
        if abs(total_delta) <= contract["fee_tolerance_paise"]:
            continue
        charged_bps = round(s.mdr_paise * 10000 / max(gross, 1))
        contracted = contract["mdr_bps"][sorted(methods)[0]]
        findings.append(dict(
            code="FEE_VARIANCE", settlement=s, orders=list(m.order_ids),
            expected_mdr=exp["mdr"], actual_mdr=s.mdr_paise, overcharge=total_delta,
            detail="MDR charged at {:.2f}% against a contracted {:.2f}%".format(
                charged_bps / 100, contracted / 100)))
    return findings


# ------------------------------------------------------------ exceptions

def build_exceptions(orders, setts, matches, fee_findings, open_o, open_s, contract, as_of):
    by_o = {o.order_id: o for o in orders}
    exs = []
    matched_orders = {oid for m in matches if m.confident for oid in m.order_ids}

    for m in matches:
        if m.confident:
            continue
        o = by_o[m.order_ids[0]]
        exs.append(Exception_(
            "LOW_CONFIDENCE_MATCH", "match", "{}~{}".format(m.order_ids[0], m.settlement_id),
            0, (as_of - o.order_date).days,
            "{} and {} share an amount, but self-verification scored the pairing {} "
            "and rejected it: {}. Exposure is booked on {} itself, not double-counted "
            "here.".format(m.order_ids[0], m.settlement_id, "%.2f" % m.confidence,
                           "; ".join(m.signals), m.order_ids[0]),
            "Do not treat as settled. Confirm against the customer record first."))

    # A retry twin shares customer + instrument + amount, so index on exactly
    # that and look in one bucket instead of rescanning every order per
    # unmatched order. Buckets keep insertion order, so the twin chosen is the
    # same one the linear scan found.
    twins = {}
    for t in orders:
        if t.order_id in matched_orders:
            twins.setdefault((t.customer_id, t.method, t.gross_paise), []).append(t)

    for oid in sorted(open_o):
        o = by_o[oid]
        age = (as_of - o.order_date).days
        window = contract["settlement_days"][o.method]
        twin = next((t for t in twins.get((o.customer_id, o.method, o.gross_paise), ())
                     if t.order_id != oid
                     and abs((t.order_date - o.order_date).days) <= 1), None)
        if twin:
            exs.append(Exception_(
                "DUPLICATE_RETRY", "order", oid, o.gross_paise, age,
                "Same customer, instrument and amount as {} within 1 day, and only {} "
                "settled. Gateway retry, not a second sale.".format(twin.order_id, twin.order_id),
                "Void this order. Do not chase a second bank credit."))
        elif o.status == "refunded":
            if age <= contract["refund_window_days"]:
                exs.append(Exception_(
                    "LATE_REFUND", "order", oid, o.gross_paise, age,
                    "Refund raised {}d ago; refunds reflect up to T+{}. Still inside "
                    "the window.".format(age, contract["refund_window_days"]),
                    "No action yet. Recheck after T+{}.".format(contract["refund_window_days"])))
            else:
                exs.append(Exception_(
                    "MISSING_BANK_CREDIT", "order", oid, o.gross_paise, age,
                    "Refund is {}d old, past the T+{} window, and has never appeared on "
                    "the bank file.".format(age, contract["refund_window_days"]),
                    "Raise a refund-status query with the gateway."))
        elif age <= window:
            exs.append(Exception_(
                "LATE_SETTLEMENT", "order", oid, fees(o.gross_paise, o.method, contract)["net"], age,
                "Captured {}d ago; {} settles at T+{}. Not yet due.".format(age, o.method, window),
                "No action. Expected on or before T+{}.".format(window)))
        else:
            exs.append(Exception_(
                "MISSING_BANK_CREDIT", "order", oid, fees(o.gross_paise, o.method, contract)["net"], age,
                "Captured {}d ago against a T+{} window and no bank credit exists. Money "
                "left the customer and never reached the merchant.".format(age, window),
                "Escalate to the gateway with this order id. Real cash shortfall."))

    rejected = {m.settlement_id: m for m in matches if not m.confident}
    for s in sorted(open_s, key=lambda x: x.settlement_id):
        age = (as_of - s.settled_date).days
        why = ""
        if s.settlement_id in rejected:
            r = rejected[s.settlement_id]
            why = (" The only candidate, {}, scored {} on self-verification and was "
                   "rejected.".format(r.order_ids[0], "%.2f" % r.confidence))
        if s.txn_type == "batch":
            # Two different reasons a deposit stays open, and they need different
            # actions: nothing summed to it, or something did but not uniquely.
            # Reporting the second as the first sends the controller chasing a
            # breakup file when the real problem is that we cannot prove which
            # orders these are.
            r = rejected.get(s.settlement_id)
            if r:
                detail = ("Lump-sum deposit with no per-transaction breakdown. {} open "
                          "orders do sum to this net, but so does another combination, "
                          "so the decomposition scored {} and was not asserted.".format(
                              len(r.order_ids), "%.2f" % r.confidence))
                action = ("Confirm which orders belong to UTR {} before booking them "
                          "as settled.".format(s.utr))
            else:
                detail = ("Lump-sum deposit with no per-transaction breakdown; no "
                          "combination of open orders reconstructs this net after "
                          "backing out contract fees.")
                action = "Request the settlement breakup file for UTR {}.".format(s.utr)
            exs.append(Exception_("NET_DEPOSIT_UNRESOLVED", "settlement",
                                  s.settlement_id, s.net_paise, age, detail, action))
        else:
            exs.append(Exception_(
                "UNKNOWN", "settlement", s.settlement_id, s.gross_paise, age,
                "Bank credit of {} on {} with no order it can belong to. Either an order is "
                "missing from our books or this is a misposted credit.".format(
                    rupees(s.gross_paise), s.settled_date) + why,
                "Trace UTR {} with the bank before recognising this revenue.".format(s.utr)))

    for f in fee_findings:
        s = f["settlement"]
        exs.append(Exception_(
            f["code"], "settlement", s.settlement_id, max(f["overcharge"], 0),
            (as_of - s.settled_date).days,
            "{}. Expected MDR {}, actually deducted {}.".format(
                f["detail"], rupees(f["expected_mdr"]), rupees(f["actual_mdr"])),
            "Claw back {} from the gateway.".format(rupees(max(f["overcharge"], 0)))))

    exs.sort(key=lambda e: -e.risk_score)
    return exs


# ------------------------------------------------------------ run

def reconcile(data_dir=ROOT / "data", contract_path=ROOT / "contract.json"):
    import time
    t0 = time.perf_counter()
    orders, setts, contract = load(data_dir, contract_path)
    as_of = max(max(s.settled_date for s in setts), max(o.order_date for o in orders))

    open_o = {o.order_id for o in orders}
    open_s = list(setts)
    matches = []
    for fn in (match_exact, match_fuzzy, match_batch):
        matches += fn(orders, setts, contract, open_o, open_s)

    fee_findings = verify_fees(matches, orders, setts, contract)

    # a demoted match is an advisory, not a resolution: put both sides back in
    # the exception pool so a real MISSING_BANK_CREDIT is never hidden by a
    # lookalike the engine already refused to trust
    by_s = {s.settlement_id: s for s in setts}
    for m in matches:
        if not m.confident:
            open_o.update(m.order_ids)
            open_s.append(by_s[m.settlement_id])
    open_s.sort(key=lambda s: s.settlement_id)

    exceptions = build_exceptions(orders, setts, matches, fee_findings,
                                  open_o, open_s, contract, as_of)
    elapsed = time.perf_counter() - t0

    return dict(orders=orders, settlements=setts, contract=contract, matches=matches,
                fee_findings=fee_findings, exceptions=exceptions, as_of=as_of,
                elapsed=elapsed, records=len(orders) + len(setts))
