"""Report + accuracy scoring against the answer key.

Every number printed here is computed from the run and from data/answer_key.csv.
Nothing is asserted that the key cannot confirm.

    python report.py            text report
    python report.py --json     machine-readable dump (used by the dashboard)
    python report.py --explain  route exception text through the LLM layer
"""
import csv
import json
import sys
from pathlib import Path

from engine import ROOT, reconcile, rupees, fees

# 2025 AFP Treasury Benchmarking Survey: mean time to resolve a single
# reconciliation discrepancy manually.
MANUAL_DAYS_PER_DISCREPANCY = 6.1


def truth(data_dir=ROOT / "data"):
    with open(Path(data_dir) / "answer_key.csv", newline="") as f:
        return list(csv.DictReader(f))


def score(run, key):
    """Compare the engine's matches to ground truth. Both directions."""
    truth_pairs = set()
    for k in key:
        if k["record_type"] == "order" and k["expected_verdict"].startswith("MATCH") \
                and k["expected_counterpart"]:
            truth_pairs.add((k["record_id"], k["expected_counterpart"]))

    confident, demoted = set(), set()
    for m in run["matches"]:
        (confident if m.confident else demoted).update(
            (oid, m.settlement_id) for oid in m.order_ids)

    tp = truth_pairs & confident
    fp = confident - truth_pairs
    fn = truth_pairs - confident
    caught = demoted - truth_pairs      # wrong matches self-verification refused to assert
    cautious = demoted & truth_pairs    # real matches it refused to assert anyway

    # exception-code accuracy
    emitted = {}
    for e in run["exceptions"]:
        for part in e.record_id.split("~"):
            emitted.setdefault(part, set()).add(e.code)

    exp_hits, exp_miss = [], []
    for k in key:
        want = k["expected_verdict"]
        codes = emitted.get(k["record_id"], set())
        if want.startswith("MATCH+"):          # matched, but a fee flag was planted
            want = want.split("+", 1)[1]
        elif want.startswith("MATCH"):
            continue
        (exp_hits if want in codes else exp_miss).append(
            dict(record=k["record_id"], expected=want, got=sorted(codes) or ["<nothing>"],
                 note=k["note"]))

    return dict(truth_pairs=truth_pairs, tp=tp, fp=fp, fn=fn, caught=caught,
                cautious=cautious, exp_hits=exp_hits, exp_miss=exp_miss)


def to_json(run, sc):
    return dict(
        as_of=run["as_of"].isoformat(),
        elapsed=run["elapsed"],
        orders=[dict(id=o.order_id, customer=o.customer_id, method=o.method,
                     gross=o.gross_paise, date=o.order_date.isoformat(), status=o.status)
                for o in run["orders"]],
        settlements=[dict(id=s.settlement_id, utr=s.utr, ref=s.order_ref,
                          customer=s.customer_ref, method=s.method, gross=s.gross_paise,
                          mdr=s.mdr_paise, platform=s.platform_fee_paise, gst=s.gst_paise,
                          net=s.net_paise, date=s.settled_date.isoformat(), type=s.txn_type)
                     for s in run["settlements"]],
        matches=[dict(orders=m.order_ids, settlement=m.settlement_id, pass_name=m.pass_name,
                      confidence=m.confidence, confident=m.confident, signals=m.signals)
                 for m in run["matches"]],
        exceptions=[dict(code=e.code, record_type=e.record_type, record=e.record_id,
                         amount=e.amount_paise, age_days=e.age_days, risk=e.risk_score,
                         explanation=e.explanation, action=e.action,
                         llm_explanation=e.llm_explanation, llm_category=e.llm_category)
                    for e in run["exceptions"]],
        accuracy=dict(true_positives=len(sc["tp"]), false_positives=sorted(sc["fp"]),
                      false_negatives=sorted(sc["fn"]),
                      false_matches_caught=sorted(sc["caught"]),
                      cautious_misses=sorted(sc["cautious"]),
                      exception_hits=len(sc["exp_hits"]), exception_misses=sc["exp_miss"]),
    )


def text(run, sc):
    L = []
    p = L.append
    c = run["contract"]
    n_o, n_s = len(run["orders"]), len(run["settlements"])
    conf = [m for m in run["matches"] if m.confident]
    dem = [m for m in run["matches"] if not m.confident]
    matched_orders = sum(len(m.order_ids) for m in conf)

    p("=" * 78)
    p("  RECONCILIATION + FEE VERIFICATION -- {}".format(c["merchant"]))
    p("  book date {}   {} orders / {} bank lines = {} records".format(
        run["as_of"], n_o, n_s, run["records"]))
    p("=" * 78)

    p("\nTHROUGHPUT")
    p("  {} records in {:.3f}s  ({:,.0f} records/sec)".format(
        run["records"], run["elapsed"], run["records"] / max(run["elapsed"], 1e-9)))
    n_exc = len(run["exceptions"])
    p("  manual reconciliation averages {} business days per discrepancy".format(
        MANUAL_DAYS_PER_DISCREPANCY))
    p("  (2025 AFP Treasury Benchmarking Survey); {} discrepancies here would be".format(n_exc))
    p("  {:.0f} analyst-days. This engine: {:.3f} seconds.".format(
        n_exc * MANUAL_DAYS_PER_DISCREPANCY, run["elapsed"]))

    p("\nMATCH RATE, BY CONFIDENCE TIER")
    tiers = {"exact (1.00)": [m for m in conf if m.pass_name == "exact"],
             "fuzzy, verified": [m for m in conf if m.pass_name == "fuzzy"],
             "batch decomposition": [m for m in conf if m.pass_name == "batch"],
             "DEMOTED by self-verification": dem}
    for name, ms in tiers.items():
        got = sum(len(m.order_ids) for m in ms)
        p("  {:<32} {:>3} orders  ({:>5.1f}%)  across {} bank lines".format(
            name, got, 100 * got / n_o, len(ms)))
    p("  {:<32} {:>3} orders  ({:>5.1f}%)".format(
        "MATCHED (confident)", matched_orders, 100 * matched_orders / n_o))

    p("\nACCURACY vs ANSWER KEY (data/answer_key.csv, never read by the engine)")
    p("  true positives  (matched, and the key agrees) : {}".format(len(sc["tp"])))
    p("  FALSE POSITIVES (matched, and the key says no) : {}".format(len(sc["fp"])))
    for pair in sorted(sc["fp"]):
        p("      {} -> {}".format(*pair))
    p("  false negatives (key says match, we did not)   : {}".format(len(sc["fn"])))
    for pair in sorted(sc["fn"]):
        p("      {} -> {}".format(*pair))
    p("  false matches CAUGHT by self-verification      : {}".format(len(sc["caught"])))
    for pair in sorted(sc["caught"]):
        p("      {} -> {}  (amount agreed, everything else did not)".format(*pair))
    p("  cautious misses (real, but unprovable from the file): {}".format(len(sc["cautious"])))
    for pair in sorted(sc["cautious"]):
        p("      {} -> {}".format(*pair))
    prec = len(sc["tp"]) / max(len(sc["tp"]) + len(sc["fp"]), 1)
    rec = len(sc["tp"]) / max(len(sc["truth_pairs"]), 1)
    p("  precision {:.3f}   recall {:.3f}".format(prec, rec))
    p("  exception cause correctly identified: {}/{}".format(
        len(sc["exp_hits"]), len(sc["exp_hits"]) + len(sc["exp_miss"])))
    for m in sc["exp_miss"]:
        p("      MISSED {:<12} expected {:<22} got {}".format(
            m["record"], m["expected"], ", ".join(m["got"])))

    p("\nFEE VERIFICATION (second loop)")
    leak = sum(max(f["overcharge"], 0) for f in run["fee_findings"])
    p("  transactions priced against contract : {}".format(
        sum(len(m.order_ids) for m in run["matches"] if m.confident)))
    p("  fee findings                         : {}".format(len(run["fee_findings"])))
    for f in run["fee_findings"]:
        p("    {:<20} {:<10} {} overcharged  ({})".format(
            f["code"], f["settlement"].settlement_id, rupees(max(f["overcharge"], 0)),
            f["detail"]))
    p("  total fee leakage on this batch      : {}".format(rupees(leak)))
    p("  platform fee is contractual revenue and is verified separately from MDR,")
    p("  so it can never be used to hide an MDR overcharge.")

    p("\nEXCEPTIONS, RANKED BY MONEY AT RISK x AGE (not by count)")
    p("  {:<3} {:<22} {:<24} {:>13} {:>4}".format("#", "CODE", "RECORD", "AT RISK", "AGE"))
    total_risk = 0
    for i, e in enumerate(run["exceptions"], 1):
        total_risk += e.amount_paise
        p("  {:<3} {:<22} {:<24} {:>13} {:>3}d".format(
            i, e.code, e.record_id, rupees(e.amount_paise), e.age_days))
        p("      why    : {}".format(e.explanation))
        p("      action : {}".format(e.action))
    p("\n  {} exceptions, {} of exposure across the batch.".format(
        len(run["exceptions"]), rupees(total_risk)))

    counts = {}
    for e in run["exceptions"]:
        counts[e.code] = counts.get(e.code, 0) + 1
    p("\nEXCEPTION MIX")
    for code, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        p("  {:<24} {}".format(code, n))
    p("=" * 78)
    return "\n".join(L)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # ₹ on a cp1252 console
    run = reconcile()
    key = truth()
    sc = score(run, key)
    if "--explain" in sys.argv:
        from explain import enrich
        enrich(run)
        print(run.get("llm_note", "explanation layer unavailable"), file=sys.stderr)
    if "--json" in sys.argv:
        out = to_json(run, sc)
        Path(ROOT / "data" / "run.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
        print(json.dumps(out["accuracy"], indent=1))
    else:
        print(text(run, sc))


if __name__ == "__main__":
    main()
