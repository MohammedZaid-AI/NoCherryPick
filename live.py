"""Live demo mode: a virtual clock driving the real engine, one tick at a time.

Nothing here re-implements matching. The world keeps an in-memory book -- open
orders and open bank lines -- and each tick hands that book to the SAME
match_exact / match_fuzzy / match_batch / verify_fees / build_exceptions
functions the batch report uses. Every confidence score on screen came out of
engine.challenge(); every rupee came out of engine.fees().

The only thing this module invents is *when* records appear. That is the point:
the virtual clock lets five days of settlement lag play out in thirty seconds.
"""
import json
import queue
import random
import threading
import time
from datetime import date, timedelta

import engine
from engine import (ROOT, Order, Settlement, fees, match_exact, match_fuzzy,
                    match_batch, verify_fees, build_exceptions, rupees)

START = date(2026, 9, 1)
DAYS_PER_SECOND = 0.5          # 1 virtual day per 2 real seconds at 1x
TICK = 0.1                     # real seconds between engine ticks
METHODS = ["upi", "credit_card", "visa_debit", "netbanking", "rupay_debit", "wallet"]


# ------------------------------------------------------------------ event bus

class Bus:
    """Fan-out to every connected browser. One queue per subscriber."""

    def __init__(self):
        self._subs = []
        self._lock = threading.Lock()
        self.seq = 0

    def subscribe(self):
        q = queue.Queue(maxsize=2000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def emit(self, kind, **payload):
        with self._lock:
            self.seq += 1
            payload["kind"] = kind
            payload["seq"] = self.seq
            dead = []
            for q in self._subs:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    dead.append(q)          # a browser that stopped reading
            for q in dead:
                self._subs.remove(q)


BUS = Bus()


# ------------------------------------------------------------------ clock

class Clock:
    def __init__(self):
        self.vday = 0.0
        self.speed = 1.0

    def advance(self, dt):
        self.vday += dt * DAYS_PER_SECOND * self.speed

    @property
    def date(self):
        return START + timedelta(days=int(self.vday))


# ------------------------------------------------------------------ world

class World:
    """The live book. Everything the engine needs, held in memory."""

    def __init__(self):
        self.contract = json.loads((ROOT / "contract.json").read_text())
        # engine.THRESHOLD is normally set by engine.load(); live mode never
        # loads CSVs, so set it here or Match.confident uses a stale default
        engine.THRESHOLD = self.contract["confidence_threshold"]

        self.clock = Clock()
        self.lock = threading.RLock()

        self.orders, self.setts, self.matches = [], [], []
        self.open_o, self.open_s = set(), []
        # a demoted pair is quarantined from further matching (otherwise every
        # tick would re-match and re-demote it) but still counts as open for
        # exception building, exactly as reconcile() reports it
        self.quar_o, self.quar_s = set(), []

        self.pending = []            # [(release_vday, "order"|"settlement", record)]
        self.exceptions = []
        self._exc_key = None
        self._seen_findings = set()
        self._findings = []          # accumulated; a finding never changes once made
        self._new_setts = []
        self._explained = set()
        self.explain_q = queue.Queue()
        self.engine_ms = 0.0
        self.n_o = self.n_s = 2000

    # -------------------------------------------------- record construction

    def oid(self):
        self.n_o += 1
        return "ORD-%d" % self.n_o

    def sid(self):
        self.n_s += 1
        return "SET-%d" % self.n_s

    def schedule(self, when_vday, kind, record):
        self.pending.append((when_vday, kind, record))

    def new_order(self, method=None, gross=None, customer=None, status="captured"):
        o = Order(self.oid(), customer or "CUST-%03d" % random.randrange(1, 60),
                  method or random.choice(METHODS),
                  gross or random.randrange(250, 15000) * 100 + random.randrange(1, 100),
                  self.clock.date, status)
        return o

    def bank_line(self, o, *, lag=None, ref=True, cust_ref=True, txn_type="credit",
                  mdr_bps=None):
        """A settlement line for an order, priced by the real fee function."""
        window = self.contract["settlement_days"][o.method]
        lag = window if lag is None else lag
        f = fees(o.gross_paise, o.method, self.contract, mdr_bps=mdr_bps)
        if txn_type == "refund":
            f = {"mdr": 0, "platform": 0, "gst": 0, "net": -o.gross_paise}
        return Settlement(
            self.sid(), "UTR%09d" % random.randrange(10 ** 8),
            o.order_id if ref else "", o.customer_id if cust_ref else "",
            o.method, o.gross_paise, f["mdr"], f["platform"], f["gst"], f["net"],
            o.order_date + timedelta(days=lag), txn_type), lag

    def emit_order(self, o, lag=None, **kw):
        """Release an order now and queue its bank credit for T+lag."""
        s, lag = self.bank_line(o, lag=lag, **kw)
        self.schedule(self.clock.vday, "order", o)
        self.schedule(self.clock.vday + lag, "settlement", s)
        return o, s

    # -------------------------------------------------- the tick

    def tick(self):
        with self.lock:
            self._release()
            t0 = time.perf_counter()
            self._run_engine()
            self.engine_ms = (time.perf_counter() - t0) * 1000

    def _release(self):
        self._new_setts = []           # this tick's arrivals, for incremental pricing
        due = [p for p in self.pending if p[0] <= self.clock.vday]
        if not due:
            return
        self.pending = [p for p in self.pending if p[0] > self.clock.vday]
        for _, kind, rec in sorted(due, key=lambda p: p[0]):
            if kind == "order":
                self.orders.append(rec)
                self.open_o.add(rec.order_id)
                BUS.emit("order", id=rec.order_id, customer=rec.customer_id,
                         method=rec.method, gross=rec.gross_paise,
                         date=rec.order_date.isoformat(), status=rec.status)
            else:
                self.setts.append(rec)
                self.open_s.append(rec)
                self._new_setts.append(rec)
                BUS.emit("settlement", id=rec.settlement_id, ref=rec.order_ref,
                         method=rec.method, gross=rec.gross_paise, net=rec.net_paise,
                         mdr=rec.mdr_paise, type=rec.txn_type,
                         date=rec.settled_date.isoformat())

    def _run_engine(self):
        """One pass of the real engine over the current book.

        Per-tick cost is bounded by what is still *open*, not by how long the
        demo has run. Matching already only sees the open pools. Fee findings
        are computed once per match and per bank line and then kept, because
        neither can change afterwards: the orders, the settlement and the
        contract behind a finding are all frozen by the time it exists.
        """
        fresh = []
        for fn in (match_exact, match_fuzzy, match_batch):
            fresh += fn(self.orders, self.setts, self.contract, self.open_o, self.open_s)

        by_s = {s.settlement_id: s for s in self.setts}
        for m in fresh:
            self.matches.append(m)
            BUS.emit("match", orders=m.order_ids, settlement=m.settlement_id,
                     pass_name=m.pass_name, confidence=m.confidence,
                     confident=m.confident, signals=m.signals,
                     gross=sum(o.gross_paise for o in self.orders
                               if o.order_id in m.order_ids))
            if not m.confident:
                # quarantine: out of the matching pool, still open for exceptions
                self.quar_o.update(m.order_ids)
                self.quar_s.append(by_s[m.settlement_id])

        # Price only what is new: this tick's matches, and this tick's bank
        # lines for the statutory 0% MDR check. Both arrive together, so the
        # cross-check that suppresses a duplicate FEE_VARIANCE on a line already
        # flagged for illegal MDR still sees both halves in the same call.
        self._findings += verify_fees(fresh, self.orders, self.setts, self.contract,
                                      scan=self._new_setts)
        findings = self._findings
        for f in findings:
            key = (f["code"], f["settlement"].settlement_id)
            if key in self._seen_findings:
                continue
            self._seen_findings.add(key)
            BUS.emit("flag", code=f["code"], settlement=f["settlement"].settlement_id,
                     overcharge=max(f["overcharge"], 0), expected_mdr=f["expected_mdr"],
                     actual_mdr=f["actual_mdr"], detail=f["detail"])

        exc_o = self.open_o | self.quar_o
        exc_s = self.open_s + self.quar_s
        self.exceptions = build_exceptions(
            self.orders, self.setts, self.matches, findings,
            exc_o, exc_s, self.contract, self.clock.date)

        for e in self.exceptions:
            # LATE_SETTLEMENT is "not yet due" -- explaining every one of those
            # buries the demotions and fee flags that actually need a human.
            # Keyed on (code, record) so an escalation gets a fresh explanation.
            if e.code == "LATE_SETTLEMENT":
                continue
            if (e.code, e.record_id) not in self._explained:
                self._explained.add((e.code, e.record_id))
                self.explain_q.put(e)

        key = tuple((e.code, e.record_id, e.amount_paise) for e in self.exceptions)
        if key != self._exc_key:
            self._exc_key = key
            BUS.emit("exceptions", items=[
                dict(code=e.code, record=e.record_id, record_type=e.record_type,
                     amount=e.amount_paise, age_days=e.age_days, risk=e.risk_score,
                     explanation=e.explanation, action=e.action,
                     llm_explanation=e.llm_explanation) for e in self.exceptions])
        BUS.emit("stats", **self.stats())

    def stats(self):
        conf = [m for m in self.matches if m.confident]
        fee = sum(e.amount_paise for e in self.exceptions
                  if e.code in ("FEE_VARIANCE", "ZERO_MDR_VIOLATION"))
        return dict(orders=len(self.orders), settlements=len(self.setts),
                    matched=sum(len(m.order_ids) for m in conf),
                    demoted=sum(len(m.order_ids) for m in self.matches if not m.confident),
                    exceptions=len(self.exceptions),
                    risk=sum(e.amount_paise for e in self.exceptions),
                    fee_leak=fee, engine_ms=round(self.engine_ms, 3),
                    vdate=self.clock.date.isoformat(), speed=self.clock.speed,
                    pending=len(self.pending))

    def snapshot(self):
        with self.lock:
            return dict(
                kind="snapshot",
                orders=[dict(id=o.order_id, customer=o.customer_id, method=o.method,
                             gross=o.gross_paise, date=o.order_date.isoformat(),
                             status=o.status) for o in self.orders],
                settlements=[dict(id=s.settlement_id, ref=s.order_ref, method=s.method,
                                  gross=s.gross_paise, net=s.net_paise, mdr=s.mdr_paise,
                                  type=s.txn_type, date=s.settled_date.isoformat())
                             for s in self.setts],
                matches=[dict(orders=m.order_ids, settlement=m.settlement_id,
                              pass_name=m.pass_name, confidence=m.confidence,
                              confident=m.confident, signals=m.signals)
                         for m in self.matches],
                exceptions=[dict(code=e.code, record=e.record_id,
                                 record_type=e.record_type, amount=e.amount_paise,
                                 age_days=e.age_days, risk=e.risk_score,
                                 explanation=e.explanation, action=e.action,
                                 llm_explanation=e.llm_explanation)
                            for e in self.exceptions],
                stats=self.stats())


WORLD = World()


# ------------------------------------------------------------------ scenarios

def scenario(name):
    """Inject a prepared case into the live stream. Same shapes generate.py plants."""
    w = WORLD
    with w.lock:
        c = w.contract
        if name == "lookalike":
            # two orders, one amount, one credit that names neither of them
            amt = random.randrange(3000, 9000) * 100 + 12
            a = w.new_order("credit_card", amt, "CUST-012")
            b = w.new_order("credit_card", amt, "CUST-037")
            for o in (a, b):
                w.schedule(w.clock.vday, "order", o)
            s, _ = w.bank_line(a, lag=c["settlement_days"]["credit_card"] + 1,
                               ref=False, cust_ref=False)
            w.schedule(w.clock.vday + 3, "settlement", s)
            return "two orders of {} and one credit that proves neither".format(rupees(amt))

        if name == "illegal-mdr":
            o = w.new_order("upi", random.randrange(2000, 12000) * 100 + 33)
            w.emit_order(o, mdr_bps=90)      # 0.90% MDR on UPI: statutory 0%
            return "UPI order with a 0.90% MDR cut by the bank"

        if name == "batch":
            batch = [w.new_order("upi", random.randrange(300, 4000) * 100 + i)
                     for i in range(1, 7)]
            net = plat = gst = gross = 0
            for o in batch:
                w.schedule(w.clock.vday, "order", o)
                f = fees(o.gross_paise, o.method, c)
                net += f["net"]; plat += f["platform"]; gst += f["gst"]
                gross += o.gross_paise
            lump = Settlement(w.sid(), "UTR%09d" % random.randrange(10 ** 8), "", "",
                              "upi", gross, 0, plat, gst, net,
                              w.clock.date + timedelta(days=1), "batch")
            w.schedule(w.clock.vday + 1, "settlement", lump)
            return "6 orders lumped into one net deposit of {}".format(rupees(net))

        if name == "late-refund":
            o = w.new_order(random.choice(METHODS),
                            random.randrange(2000, 9000) * 100 + 7, status="refunded")
            w.schedule(w.clock.vday, "order", o)
            s, _ = w.bank_line(o, lag=4, ref=False, txn_type="refund")
            w.schedule(w.clock.vday + 4, "settlement", s)
            return "refund raised now, bank reflects it at T+4"

    raise KeyError(name)


# ------------------------------------------------------------------ drivers

def _autoplay():
    """Steady merchant traffic on the virtual clock, plus the odd complication."""
    w = WORLD
    next_spawn = 0.4
    while True:
        time.sleep(TICK)
        if w.clock.speed <= 0:
            continue
        w.clock.advance(TICK)
        with w.lock:
            while w.clock.vday >= next_spawn:
                next_spawn += random.uniform(0.35, 0.9)
                roll = random.random()
                o = w.new_order()
                window = w.contract["settlement_days"][o.method]
                if roll < 0.10:
                    # late arrival, and the bank line lost the order reference
                    w.emit_order(o, lag=window + random.randint(1, 3), ref=False)
                elif roll < 0.16:
                    w.emit_order(o, ref=False)          # no reference, on time
                elif roll < 0.20:
                    # gateway retry: a second identical order that never settles
                    w.emit_order(o)
                    twin = w.new_order(o.method, o.gross_paise, o.customer_id)
                    w.schedule(w.clock.vday, "order", twin)
                elif roll < 0.23:
                    w.new_order()                        # order the bank never credits
                    w.schedule(w.clock.vday, "order", o)
                else:
                    w.emit_order(o)
        w.tick()


def _explainer():
    """Real explain.py output for new exceptions, or the rules wording if the
    LLM layer is unavailable. Never blocks the engine."""
    try:
        import explain
        have_llm = True
    except ImportError:                                  # requests missing
        have_llm = False
    if have_llm:
        explain.warmup()                                 # ~40s cold, then ~3s/call

    while True:
        batch = [WORLD.explain_q.get()]
        time.sleep(0.4)                                  # let a few accumulate
        # each exception costs one ~3s Ollama call, so keep batches small enough
        # that the feed stays close to the events it is describing
        while not WORLD.explain_q.empty() and len(batch) < 4:
            batch.append(WORLD.explain_q.get())

        if have_llm:
            run = {"exceptions": batch}
            try:
                explain.enrich(run)
            except Exception:                            # noqa: BLE001
                pass
        for e in batch:
            # e.llm_explanation is set only if the model's text survived the
            # digit guard in explain.py; otherwise the rules wording stands
            BUS.emit("explain", record=e.record_id, code=e.code,
                     text=e.llm_explanation or e.explanation,
                     source="llm" if e.llm_explanation else "rules")


_started = False


def start():
    global _started
    if _started:
        return
    _started = True
    for fn in (_autoplay, _explainer):
        threading.Thread(target=fn, daemon=True).start()


if __name__ == "__main__":
    # Headless check of all four scenarios. No threads and no sleeping: the
    # clock is driven by hand, so this exercises the same engine path the demo
    # does, deterministically and in under a second.
    import sys

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    random.seed(7)
    w = WORLD
    seen = []
    BUS.emit = lambda kind, **p: seen.append(dict(p, kind=kind))   # capture, don't fan out

    for name in ("lookalike", "illegal-mdr", "batch", "late-refund"):
        print("inject", name, "->", scenario(name))
    for _ in range(300):
        w.clock.advance(TICK * 4)
        w.tick()

    kinds = [e["kind"] for e in seen]
    print("virtual date", w.clock.date, "| events", len(seen),
          "| orders", len(w.orders), "| matches", len(w.matches))

    demoted = [m for m in w.matches if not m.confident]
    assert demoted, "lookalike did not demote"
    assert demoted[0].confidence <= 0.55, demoted[0].confidence
    print("  lookalike   demoted at conf", demoted[0].confidence,
          "|", demoted[0].signals[-1])

    flags = [e for e in seen if e["kind"] == "flag"]
    assert any(f["code"] == "ZERO_MDR_VIOLATION" for f in flags), "illegal MDR not flagged"
    print("  illegal MDR flagged, overcharge", rupees(flags[0]["overcharge"]))

    batch = [m for m in w.matches if m.pass_name == "batch"]
    assert batch and len(batch[0].order_ids) == 6, "batch decomposition failed"
    print("  batch       decomposed", len(batch[0].order_ids), "orders at conf",
          batch[0].confidence)

    codes = {e.code for e in w.exceptions}
    refund = [m for m in w.matches
              if any(s.settlement_id == m.settlement_id and s.txn_type == "refund"
                     for s in w.setts)]
    assert refund, "late refund never resolved against a bank line"
    print("  late refund resolved by", refund[0].settlement_id)

    assert "match" in kinds and "exceptions" in kinds and "stats" in kinds
    print("all live checks passed")
