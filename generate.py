"""Synthetic dataset generator with planted, known-in-advance problems.

Writes data/orders.csv, data/settlement.csv and data/answer_key.csv.
The answer key is ground truth: it is never read by the engine, only by the
report, so every accuracy number can be checked rather than asserted.

Seeded, so the dataset is identical on every machine.
"""
import csv
import json
import random
from datetime import date, timedelta
from pathlib import Path

from engine import fees

ROOT = Path(__file__).parent
AS_OF = date(2026, 8, 21)
SEED = 2026

METHODS = ["upi", "credit_card", "visa_debit", "netbanking", "rupay_debit", "wallet"]

orders, setts, key = [], [], []
_used_amounts = set()


def amount(low=25000, high=1500000):
    """Unique gross in paise. Odd paise values keep accidental subset sums rare."""
    while True:
        a = random.randrange(low // 100, high // 100) * 100 + random.randrange(1, 100)
        if a not in _used_amounts:
            _used_amounts.add(a)
            return a


def order(oid, cust, method, gross, d, status="captured"):
    orders.append(dict(order_id=oid, customer_id=cust, method=method,
                       gross_paise=gross, order_date=d.isoformat(), status=status))
    return orders[-1]


def settle(sid, o, contract, *, day_offset=None, ref=True, cust_ref=True,
           txn_type="credit", mdr_override=None):
    d = date.fromisoformat(o["order_date"]) + timedelta(
        days=contract["settlement_days"][o["method"]] if day_offset is None else day_offset)
    f = fees(o["gross_paise"], o["method"], contract, mdr_bps=mdr_override)
    if txn_type == "refund":
        f = {"mdr": 0, "platform": 0, "gst": 0, "net": -o["gross_paise"]}
    setts.append(dict(settlement_id=sid, utr="UTR%09d" % random.randrange(10 ** 8),
                      order_ref=o["order_id"] if ref else "",
                      customer_ref=o["customer_id"] if cust_ref else "",
                      method=o["method"], gross_paise=o["gross_paise"],
                      mdr_paise=f["mdr"], platform_fee_paise=f["platform"],
                      gst_paise=f["gst"], net_paise=f["net"],
                      settled_date=d.isoformat(), txn_type=txn_type))
    return setts[-1]


def expect(rtype, rid, counterpart, verdict, planted="", note=""):
    key.append(dict(record_type=rtype, record_id=rid, expected_counterpart=counterpart,
                    expected_verdict=verdict, planted_problem=planted, note=note))


def build():
    random.seed(SEED)
    contract = json.loads((ROOT / "contract.json").read_text())
    n, sn = 1000, 5000

    def oid():
        nonlocal n
        n += 1
        return "ORD-%d" % n

    def sid():
        nonlocal sn
        sn += 1
        return "SET-%d" % sn

    def cust():
        return "CUST-%03d" % random.randrange(1, 60)

    # -- A. 38 clean, referenced, correctly priced ------------------------
    clean = []
    for _ in range(38):
        m = random.choice(METHODS)
        o = order(oid(), cust(), m, amount(),
                  AS_OF - timedelta(days=random.randrange(4, 18)))
        s = settle(sid(), o, contract)
        clean.append((o, s))
        expect("order", o["order_id"], s["settlement_id"], "MATCH")
        expect("settlement", s["settlement_id"], o["order_id"], "MATCH")

    # -- J. fee problems planted on 4 of the clean rows -------------------
    upis = [p for p in clean if p[0]["method"] in contract["zero_mdr_mandated"]][:2]
    for o, s in upis:
        f = fees(o["gross_paise"], o["method"], contract, mdr_bps=90)  # illegal 0.90%
        s.update(mdr_paise=f["mdr"], gst_paise=f["gst"], net_paise=f["net"])
        key_row = next(k for k in key if k["record_id"] == s["settlement_id"])
        key_row.update(expected_verdict="MATCH+ZERO_MDR_VIOLATION",
                       planted_problem="ZERO_MDR_VIOLATION",
                       note="0.90%% MDR deducted on %s, statutory rate is 0%%" % o["method"])

    cards = [p for p in clean if p[0]["method"] in ("credit_card", "visa_debit")][:2]
    for o, s in cards:
        inflated = contract["mdr_bps"][o["method"]] + 65
        f = fees(o["gross_paise"], o["method"], contract, mdr_bps=inflated)
        s.update(mdr_paise=f["mdr"], gst_paise=f["gst"], net_paise=f["net"])
        key_row = next(k for k in key if k["record_id"] == s["settlement_id"])
        key_row.update(expected_verdict="MATCH+FEE_VARIANCE", planted_problem="FEE_VARIANCE",
                       note="MDR %d bps vs contracted %d bps" % (inflated, contract["mdr_bps"][o["method"]]))

    # -- B. 5 late arrivals, bank line lost the order reference -----------
    for over in (1, 1, 2, 2, 3):
        m = random.choice(METHODS)
        o = order(oid(), cust(), m, amount(),
                  AS_OF - timedelta(days=contract["settlement_days"][m] + over + 1))
        s = settle(sid(), o, contract, ref=False,
                   day_offset=contract["settlement_days"][m] + over)
        expect("order", o["order_id"], s["settlement_id"], "MATCH", "LATE_SETTLEMENT",
               "settled %dd past window, no order reference on the bank line" % over)
        expect("settlement", s["settlement_id"], o["order_id"], "MATCH", "LATE_SETTLEMENT")

    # -- C. 3 refunds lagging 3-7 days ------------------------------------
    for i, lag in enumerate((3, 5, 7)):
        m = random.choice(METHODS)
        o = order(oid(), cust(), m, amount(), AS_OF - timedelta(days=lag + 1), "refunded")
        s = settle(sid(), o, contract, ref=(i == 0), day_offset=lag, txn_type="refund")
        expect("order", o["order_id"], s["settlement_id"], "MATCH", "LATE_REFUND",
               "refund reflected at T+%d" % lag)
        expect("settlement", s["settlement_id"], o["order_id"], "MATCH", "LATE_REFUND")

    # -- D. 2 refunds with no bank line at all ----------------------------
    o = order(oid(), cust(), "upi", amount(), AS_OF - timedelta(days=4), "refunded")
    expect("order", o["order_id"], "", "LATE_REFUND", "LATE_REFUND",
           "refund 4d old, still inside the T+7 window")
    o = order(oid(), cust(), "credit_card", amount(), AS_OF - timedelta(days=11), "refunded")
    expect("order", o["order_id"], "", "MISSING_BANK_CREDIT", "MISSING_BANK_CREDIT",
           "refund 11d old, past the T+7 window, never reflected")

    # -- E. 2 duplicate gateway retries -----------------------------------
    for o, s in clean[:2]:
        dup = order(oid(), o["customer_id"], o["method"], o["gross_paise"],
                    date.fromisoformat(o["order_date"]))
        expect("order", dup["order_id"], "", "DUPLICATE_RETRY", "DUPLICATE_RETRY",
               "retry of %s, only the original settled" % o["order_id"])

    # -- F. net batch deposit: 6 orders lumped into one credit ------------
    batch_day = AS_OF - timedelta(days=9)
    batch, net_total, gross_total = [], 0, 0
    for _ in range(6):
        o = order(oid(), cust(), "upi", amount(30000, 400000), batch_day)
        batch.append(o)
        f = fees(o["gross_paise"], "upi", contract)
        net_total += f["net"]
        gross_total += o["gross_paise"]
    bsid = sid()
    setts.append(dict(settlement_id=bsid, utr="UTR%09d" % random.randrange(10 ** 8),
                      order_ref="", customer_ref="", method="upi",
                      gross_paise=gross_total,
                      mdr_paise=0,
                      platform_fee_paise=sum(fees(o["gross_paise"], "upi", contract)["platform"] for o in batch),
                      gst_paise=sum(fees(o["gross_paise"], "upi", contract)["gst"] for o in batch),
                      net_paise=net_total,
                      settled_date=(batch_day + timedelta(days=1)).isoformat(),
                      txn_type="batch"))
    for o in batch:
        expect("order", o["order_id"], bsid, "MATCH", "NET_BATCH_DEPOSIT",
               "one of 6 orders inside a single lump-sum credit")
    expect("settlement", bsid, "|".join(o["order_id"] for o in batch), "MATCH",
           "NET_BATCH_DEPOSIT", "no per-transaction breakdown on this deposit")

    # -- G. trap 1: identical amount, unrelated records --------------------
    trap_amt = 481200
    _used_amounts.add(trap_amt)
    to = order(oid(), "CUST-090", "credit_card", trap_amt, AS_OF - timedelta(days=12))
    expect("order", to["order_id"], "", "MISSING_BANK_CREDIT", "SAME_AMOUNT_TRAP",
           "shares an amount with an unrelated bank credit; must NOT be matched to it")
    tsid = sid()
    setts.append(dict(settlement_id=tsid, utr="UTR%09d" % random.randrange(10 ** 8),
                      order_ref="", customer_ref="CUST-041", method="upi",
                      gross_paise=trap_amt, mdr_paise=0,
                      platform_fee_paise=fees(trap_amt, "upi", contract)["platform"],
                      gst_paise=fees(trap_amt, "upi", contract)["gst"],
                      net_paise=fees(trap_amt, "upi", contract)["net"],
                      settled_date=(AS_OF - timedelta(days=9)).isoformat(),
                      txn_type="credit"))
    expect("settlement", tsid, "", "UNKNOWN", "SAME_AMOUNT_TRAP",
           "different customer and different instrument to the order it collides with")

    # -- H. trap 2: two open orders share one amount, one credit arrives ---
    amb_amt = 734000
    _used_amounts.add(amb_amt)
    amb_day = AS_OF - timedelta(days=5)
    a = order(oid(), "CUST-012", "credit_card", amb_amt, amb_day)
    b = order(oid(), "CUST-037", "credit_card", amb_amt, amb_day)
    asid = sid()
    f = fees(amb_amt, "credit_card", contract)
    setts.append(dict(settlement_id=asid, utr="UTR%09d" % random.randrange(10 ** 8),
                      order_ref="", customer_ref="", method="credit_card",
                      gross_paise=amb_amt, mdr_paise=f["mdr"],
                      platform_fee_paise=f["platform"], gst_paise=f["gst"],
                      net_paise=f["net"],
                      settled_date=(amb_day + timedelta(days=3)).isoformat(),
                      txn_type="credit"))
    expect("order", a["order_id"], asid, "MATCH", "AMBIGUOUS_AMOUNT",
           "truly the settled one, but the bank line carries nothing that proves it")
    expect("order", b["order_id"], "", "MISSING_BANK_CREDIT", "AMBIGUOUS_AMOUNT",
           "same amount and day as %s" % a["order_id"])
    expect("settlement", asid, a["order_id"], "MATCH", "AMBIGUOUS_AMOUNT",
           "no reference, no customer ref: two orders fit equally well")

    # -- I. 2 genuinely missing bank credits ------------------------------
    for age in (8, 14):
        o = order(oid(), cust(), random.choice(["visa_debit", "netbanking"]), amount(),
                  AS_OF - timedelta(days=age))
        expect("order", o["order_id"], "", "MISSING_BANK_CREDIT", "MISSING_BANK_CREDIT",
               "captured %dd ago, no credit ever arrived" % age)

    # -- K. 1 orphan bank credit ------------------------------------------
    osid = sid()
    oamt = amount()
    f = fees(oamt, "wallet", contract)
    setts.append(dict(settlement_id=osid, utr="UTR%09d" % random.randrange(10 ** 8),
                      order_ref="", customer_ref="CUST-055", method="wallet",
                      gross_paise=oamt, mdr_paise=f["mdr"], platform_fee_paise=f["platform"],
                      gst_paise=f["gst"], net_paise=f["net"],
                      settled_date=AS_OF.isoformat(), txn_type="credit"))
    expect("settlement", osid, "", "UNKNOWN", "ORPHAN_CREDIT",
           "credit with no order behind it anywhere in our books")

    # -- L. 2 orders captured too recently to have settled yet -------------
    for age in (0, 1):
        o = order(oid(), cust(), "credit_card", amount(), AS_OF - timedelta(days=age))
        expect("order", o["order_id"], "", "LATE_SETTLEMENT", "LATE_SETTLEMENT",
               "captured %dd ago, T+2 instrument, not yet due" % age)

    return contract


def write():
    build()
    out = ROOT / "data"
    out.mkdir(exist_ok=True)
    for name, rows in (("orders.csv", orders), ("settlement.csv", setts),
                       ("answer_key.csv", key)):
        with open(out / name, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    print("orders {}  settlement {}  answer_key {}  -> {}".format(
        len(orders), len(setts), len(key), out))
    planted = {}
    for k in key:
        if k["planted_problem"]:
            planted[k["planted_problem"]] = planted.get(k["planted_problem"], 0) + 1
    for p, c in sorted(planted.items()):
        print("  planted {:<22} {}".format(p, c))


if __name__ == "__main__":
    write()
