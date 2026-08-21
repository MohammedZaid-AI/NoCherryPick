"""One runnable check over the money path. python test_engine.py"""
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import engine
from engine import Order, Settlement, fees, challenge, _subset_sum, reconcile, ROOT
from report import truth, score

C = json.loads((ROOT / "contract.json").read_text())


def test_fee_math():
    # 2% MDR + 0.2% platform on 10,000.00, then 18% GST on the two of them
    f = fees(1000000, "credit_card", C)
    assert f == {"mdr": 20000, "platform": 2000, "gst": 3960, "net": 974040}, f
    assert fees(1000000, "upi", C)["mdr"] == 0
    assert fees(1000000, "rupay_debit", C)["mdr"] == 0
    # rounding is half-up on the paise, never a float
    assert fees(12345, "visa_debit", C)["mdr"] == 111  # 12345*90/10000 = 111.105
    assert all(isinstance(v, int) for v in f.values())


def test_challenge_demotes_lookalikes():
    o = Order("O1", "CUST-001", "credit_card", 481200, date(2026, 8, 1), "captured")
    same = Settlement("S1", "U", "", "CUST-001", "credit_card", 481200, 0, 0, 0, 0,
                      date(2026, 8, 3), "credit")
    conf, _ = challenge(o, same, C, 1)
    assert conf >= C["confidence_threshold"], conf

    # identical amount, everything else disagrees -> must not survive
    impostor = Settlement("S2", "U", "", "CUST-041", "upi", 481200, 0, 0, 0, 0,
                          date(2026, 8, 4), "credit")
    conf, why = challenge(o, impostor, C, 1)
    assert conf < C["confidence_threshold"], (conf, why)

    # amount alone, with two orders competing for it -> must not survive either
    conf, _ = challenge(o, same, C, 2)
    assert conf < C["confidence_threshold"], conf

    # a credit dated before the order can never be that order's credit
    early = Settlement("S3", "U", "", "CUST-001", "credit_card", 481200, 0, 0, 0, 0,
                       date(2026, 7, 28), "credit")
    assert challenge(o, early, C, 1)[0] < 0.5


def test_subset_sum():
    items = [("a", 100), ("b", 250), ("c", 375)]
    assert set(_subset_sum(items, 475)) == {"a", "c"}
    assert _subset_sum(items, 999) is None
    assert _subset_sum(items, 0) == ()


def test_run_against_answer_key():
    run = reconcile()
    sc = score(run, truth())
    assert not sc["fp"], "wrong matches slipped past self-verification: %s" % sc["fp"]
    assert len(sc["tp"]) >= 50, len(sc["tp"])
    assert sc["caught"], "the planted same-amount trap was not caught"
    assert not sc["exp_miss"], sc["exp_miss"]

    codes = {e.code for e in run["exceptions"]}
    for required in ("ZERO_MDR_VIOLATION", "FEE_VARIANCE", "MISSING_BANK_CREDIT",
                     "DUPLICATE_RETRY", "LOW_CONFIDENCE_MATCH", "LATE_REFUND",
                     "LATE_SETTLEMENT", "UNKNOWN"):
        assert required in codes, "%s never fired" % required

    # every exception is ranked, and money at risk is never negative
    assert all(e.amount_paise >= 0 for e in run["exceptions"])
    risks = [e.risk_score for e in run["exceptions"]]
    assert risks == sorted(risks, reverse=True)

    # no order is both confidently matched and reported as unmatched
    matched = {o for m in run["matches"] if m.confident for o in m.order_ids}
    reported = {e.record_id for e in run["exceptions"] if e.record_type == "order"}
    assert not (matched & reported), matched & reported


def test_deterministic_across_processes():
    """Set iteration is hash-randomised; the engine must not be."""
    outs = []
    for seed in ("0", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONIOENCODING="utf-8")
        subprocess.run([sys.executable, "report.py", "--json"], cwd=ROOT, env=env,
                       check=True, capture_output=True)
        outs.append(json.loads((ROOT / "data" / "run.json").read_text(encoding="utf-8")))
    a, b = outs
    assert a["matches"] == b["matches"], "matching is not reproducible"
    assert a["exceptions"] == b["exceptions"], "exception list is not reproducible"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok  " + name)
    print("all checks passed")
