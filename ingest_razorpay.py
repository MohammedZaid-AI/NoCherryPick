"""Razorpay settlement report adapter.

Reads a settlement report exported from the Razorpay dashboard and hands the
existing engine records it already understands. Nothing in engine.py changes:
the adapter's whole job is translation.

Three notes on the translation, because the source and target disagree:

* Money stays in **integer paise**. Razorpay reports paise and the engine's
  money path is integer paise end to end -- `test_fee_math` asserts it. Dividing
  by 100 into rupees would introduce exactly the float drift we are told to
  avoid, so the correct "conversion" here is no conversion at all.
* Razorpay's `fee` bundles MDR and platform fee; `tax` is GST on it. They map to
  `mdr_paise` and `gst_paise` with `platform_fee_paise = 0`. The existing
  verifier compares fee totals, so `fee + tax` lands against the contracted
  MDR + platform + GST without the verifier being touched.
* Razorpay's `settlement_id` is the *payout batch*, shared by many payments, so
  it cannot be the engine's per-line `settlement_id` (which must be unique).
  Each line keys on `payment_id`; the batch grouping is kept separately in
  `groups` and checked for one-UTR-per-batch.
"""
import csv
import json
from datetime import date, datetime, timezone
from pathlib import Path

import engine
from engine import (Order, Settlement, Exception_, build_exceptions, fees,
                    match_batch, match_exact, match_fuzzy, rupees, verify_fees)

ROOT = Path(__file__).parent

# Razorpay writes instrument names its own way; the contract uses ours.
METHOD_TOKENS = [
    ("upi", "upi"), ("rupay", "rupay_debit"), ("netbanking", "netbanking"),
    ("net banking", "netbanking"), ("wallet", "wallet"), ("credit", "credit_card"),
    ("debit", "visa_debit"), ("card", "credit_card"),
]
DEFAULT_METHOD = "credit_card"


def _int(v):
    """Paise as written. Blank, '-' and 'null' all mean zero."""
    v = (v or "").strip().replace(",", "")
    if not v or v.lower() in ("null", "none", "-", "na"):
        return 0
    return int(round(float(v)))


def _day(ts):
    """Unix seconds -> the date type the engine uses. None for a blank."""
    ts = (ts or "").strip()
    if not ts or ts.lower() in ("null", "none", "-"):
        return None
    return datetime.fromtimestamp(int(float(ts)), tz=timezone.utc).date()


def _method(row):
    """Real reports carry a `method` column; the documented schema does not.

    Fall back to sniffing the free-text fields, then to DEFAULT_METHOD. The
    method decides the contracted MDR and the settlement window, so a wrong
    guess here shows up as a fee variance -- see the README note.
    """
    explicit = (row.get("method") or "").strip().lower()
    blob = explicit or " ".join(
        (row.get(k) or "") for k in ("description", "notes", "entity_id")).lower()
    for token, method in METHOD_TOKENS:
        if token in blob:
            return method
    return DEFAULT_METHOD


def load_razorpay_report(path, contract=None):
    """Full translation: engine records, plus what the engine has no slot for."""
    contract = contract or json.loads((ROOT / "contract.json").read_text())
    orders, setts, extras, groups = [], [], [], {}

    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            kind = (row.get("type") or "").strip().lower()
            pay_id = (row.get("payment_id") or "").strip()
            batch = (row.get("settlement_id") or "").strip()
            utr = (row.get("utr") or "").strip()
            order_ref = (row.get("order_id") or "").strip()
            method = _method(row)
            amount = _int(row.get("amount"))
            fee, tax = _int(row.get("fee")), _int(row.get("tax"))
            credit, debit = _int(row.get("credit")), _int(row.get("debit"))
            made = _day(row.get("created_at")) or date.today()
            settled = _day(row.get("settled_at")) or made

            if batch:
                g = groups.setdefault(batch, {"utrs": set(), "credit": 0, "rows": 0})
                g["utrs"].add(utr)
                g["credit"] += credit
                g["rows"] += 1

            if kind in ("payment", "refund"):
                refund = kind == "refund"
                # customer_id is absent from the report. Keying it to the payment
                # keeps duplicate detection honest -- a shared blank would make
                # two unrelated same-amount sales on one day look like a retry.
                cust = "RZP-" + (pay_id or order_ref or batch)
                # Razorpay bundles MDR and platform fee into one `fee` column.
                # Booking the whole thing as MDR would make the statutory 0%
                # check fire on every UPI and RuPay row, because the legitimate
                # platform fee is inside it. Split the contracted platform fee
                # back out; the residual is the real MDR. The two still sum to
                # `fee`, so the total-variance check is unaffected.
                plat_due = fees(amount, method, contract)["platform"]
                plat = min(fee, plat_due)
                mdr = fee - plat
                orders.append(Order(
                    order_ref or pay_id, cust, method, amount, made,
                    "refunded" if refund else "captured"))
                setts.append(Settlement(
                    pay_id or (batch + "-" + str(len(setts))), utr,
                    order_ref or pay_id, cust, method, amount,
                    0 if refund else mdr, 0 if refund else plat,
                    0 if refund else tax,
                    -amount if refund else (credit or amount - fee - tax),
                    settled, "refund" if refund else "credit"))

            elif kind in ("adjustment", "transfer"):
                code = ("RAZORPAY_ADJUSTMENT" if kind == "adjustment"
                        else "RAZORPAY_TRANSFER")
                extras.append(dict(
                    code=code, record=pay_id or batch, amount=credit or debit or amount,
                    when=settled, batch=batch, utr=utr,
                    text=(row.get("description") or row.get("notes") or "").strip()))

    return dict(orders=orders, settlements=setts, extras=extras, groups=groups,
                contract=contract)


def load_razorpay_settlement(path):
    """Adapter entry point named in the brief.

    Returns (orders, bank lines). The engine's bank-line type is
    `engine.Settlement`; there is no `BankLine` class to return.
    """
    r = load_razorpay_report(path)
    return r["orders"], r["settlements"]


def _extra_exceptions(extras, groups, as_of):
    """Rows the engine has no record type for, and broken payout batches."""
    out = []
    for x in extras:
        noun = "adjustment" if x["code"].endswith("ADJUSTMENT") else "transfer"
        out.append(Exception_(
            x["code"], "settlement", x["record"], abs(x["amount"]),
            max((as_of - x["when"]).days, 0),
            "Razorpay {} of {} on payout {}{}. It has no order behind it, so it "
            "cannot be reconciled against the sales book -- it moves money for a "
            "reason recorded only in the report.".format(
                noun, rupees(abs(x["amount"])), x["batch"] or "(none)",
                ", " + x["text"] if x["text"] else ""),
            "Confirm the {} against the Razorpay dashboard before it is booked "
            "to revenue or costs.".format(noun)))

    for batch, g in sorted(groups.items()):
        utrs = {u for u in g["utrs"] if u}
        if len(utrs) > 1:
            out.append(Exception_(
                "NET_DEPOSIT_UNRESOLVED", "settlement", batch, g["credit"], 0,
                "Payout {} carries {} different UTRs ({}) across {} rows. One "
                "settlement batch must land as one bank credit.".format(
                    batch, len(utrs), ", ".join(sorted(utrs)), g["rows"]),
                "Ask Razorpay for the payout breakup for this settlement id."))
    return out


def reconcile_report(path):
    """Run the real engine over an imported report, in isolation.

    Same passes, same self-verification, same fee verifier and exception
    builder the batch report and the live demo use -- only the source of the
    records is different.
    """
    import time
    t0 = time.perf_counter()
    r = load_razorpay_report(path)
    orders, setts, contract = r["orders"], r["settlements"], r["contract"]
    engine.THRESHOLD = contract["confidence_threshold"]

    dates = [o.order_date for o in orders] + [s.settled_date for s in setts]
    dates += [x["when"] for x in r["extras"]]
    as_of = max(dates) if dates else date.today()

    open_o = {o.order_id for o in orders}
    open_s = list(setts)
    matches = []
    for fn in (match_exact, match_fuzzy, match_batch):
        matches += fn(orders, setts, contract, open_o, open_s)

    findings = verify_fees(matches, orders, setts, contract)
    by_s = {s.settlement_id: s for s in setts}
    for m in matches:
        if not m.confident:
            open_o.update(m.order_ids)
            open_s.append(by_s[m.settlement_id])
    open_s.sort(key=lambda s: s.settlement_id)

    exceptions = build_exceptions(orders, setts, matches, findings,
                                  open_o, open_s, contract, as_of)
    exceptions += _extra_exceptions(r["extras"], r["groups"], as_of)
    exceptions.sort(key=lambda e: -e.risk_score)

    return dict(orders=orders, settlements=setts, matches=matches, exceptions=exceptions,
                fee_findings=findings, groups=r["groups"], extras=r["extras"],
                as_of=as_of, elapsed=time.perf_counter() - t0, contract=contract)


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sample = ROOT / "samples" / "razorpay_settlement_sample.csv"
    run = reconcile_report(sample)

    conf = [m for m in run["matches"] if m.confident]
    print("payments   ", len(run["orders"]))
    print("bank lines ", len(run["settlements"]))
    print("payouts    ", len(run["groups"]), "->",
          {b: sorted(g["utrs"]) for b, g in sorted(run["groups"].items())})
    print("matched    ", sum(len(m.order_ids) for m in conf), "of", len(run["orders"]),
          "in %.3fs" % run["elapsed"])
    print("fee findings", len(run["fee_findings"]))
    for f in run["fee_findings"]:
        print("   ", f["code"], f["settlement"].settlement_id,
              rupees(max(f["overcharge"], 0)), "|", f["detail"])
    print("exceptions ", len(run["exceptions"]))
    for e in run["exceptions"]:
        print("   ", e.code, e.record_id, rupees(e.amount_paise))

    assert run["orders"], "adapter read no payments"
    assert sum(len(m.order_ids) for m in conf) > 0, "engine matched nothing"
    codes = {e.code for e in run["exceptions"]}
    assert "RAZORPAY_ADJUSTMENT" in codes, "planted adjustment not surfaced"
    assert any(f["code"] == "FEE_VARIANCE" for f in run["fee_findings"]), \
        "planted fee variance not caught"
    # the money path must still be integers all the way through
    assert all(isinstance(s.gross_paise, int) and isinstance(s.mdr_paise, int)
               for s in run["settlements"])
    print("razorpay adapter checks passed")
