# AI Finance Controller — Reconciliation & Fee Verification Engine

Razorpay Buildathon 2026, Track 4. Closes two finance-ops loops over a 113-record
batch: **reconciliation with self-verification**, and **fee verification against
contract**.

```bash
python generate.py     # synthetic batch + answer key (seeded, reproducible)
python report.py       # run the engine, print the report + accuracy
python test_engine.py  # self-checks over the money path
python live.py         # headless check of the four live scenarios (<1s)
python ingest_razorpay.py  # reconcile the sample Razorpay export
python app.py          # http://127.0.0.1:5000 live demo · /batch one-shot report
```

No dependencies for the engine (stdlib only). Flask only for the dashboard,
`requests` only for the optional explanation layer, which talks to a local
Ollama at `http://localhost:11434` (`OLLAMA_MODEL = "qwen2.5:3b"`, one constant
at the top of `explain.py`). Nothing leaves the machine.

## Measured result on this batch

| | |
|---|---|
| Records | 63 orders + 50 bank lines = 113 |
| Confidently matched | 52 orders (82.5%) — 39 exact, 7 fuzzy-and-verified, 6 via batch decomposition |
| **False positives** | **0** |
| Recall | 0.981 |
| False matches caught by self-verification | 1 planted same-amount trap, rejected |
| Cautious misses | 1 (two orders fit one credit equally well; nothing in the file says which) |
| Exception cause correctly identified | 16/16 |
| Fee leakage found | ₹363.50 across 4 findings (2 illegal MDR, 2 over-contract) |
| Exceptions | 20, ₹120,219 total exposure, each with a cause code and an action |
| Engine time | ~0.002s (≈56,000 records/sec) |

Every one of those numbers is produced by `report.py` comparing the run to
`data/answer_key.csv`, which the engine never reads.

## The design rule: the LLM never does arithmetic

Matching, tolerance windows, fee computation, confidence scoring and ranking are
plain deterministic Python. The LLM in `explain.py` is handed facts that are
already final and asked only for prose and for a category on exceptions the rules
code could not classify.

It is not trusted with that either. Every reply is scanned for digits, and any
number the model wrote that does not already appear in the deterministic facts
causes its text to be discarded and the rules wording kept. A hallucinated figure
in a finance report is a silent, expensive bug, so the guard fails closed. The
report is complete without ever calling the model.

## What self-verification actually does

Two records sharing an amount is close to no evidence — amounts collide
constantly. So every non-referenced match is re-challenged: the engine assumes it
is wrong and sees what survives.

- settled outside the contract window for that instrument → −0.10/day, capped
- credited before the order existed → −0.60
- instrument disagrees → −0.30
- customer reference disagrees → −0.30, absent → −0.05
- **two candidates fit equally well → hard ceiling of 0.55**, not a deduction:
  if nothing in the file says which of them it is, asserting either is a coin flip

Below 0.70 the match is demoted, and *both* sides go back into the exception pool
so a real `MISSING_BANK_CREDIT` is never hidden behind a lookalike the engine
already refused to trust. The demoted pairing is still reported as an advisory,
with zero exposure attached, so the money is counted exactly once.

The two planted traps both get caught: one is an unrelated bank credit that
happens to share an amount (different customer, different instrument), the other
is two orders that genuinely cannot be told apart.

## Fee verification

The statutory 0% MDR check on UPI and RuPay debit runs against **every** bank
line, matched or not — it needs nothing from our side of the file. The
contract-rate check runs only on matches the engine actually believes; pricing a
match already demoted would be arithmetic on a guess.

Platform fee is legitimate revenue and is verified separately from MDR, but is
summed into the same variance so a gateway cannot move an MDR overcharge into it
and hide.

## Exceptions are ranked by money, not by count

`risk = money at risk × days at risk`. An ₹11,366 refund that never reached the
customer outranks four fee variances worth ₹363 between them, even though the fee
findings are the more satisfying catch. Every exception carries a cause code,
a plain explanation, the exposure, and an action a controller can take today.

Manual reconciliation averages 6.1 business days per discrepancy (2025 AFP
Treasury Benchmarking Survey). The 20 discrepancies in this batch would be ~122
analyst-days.

## Live demo mode (`/`)

A virtual clock drives the real engine tick by tick. Merchant orders fire on a
schedule, bank credits arrive after the contract's settlement lag, and the agent
matches, challenges, flags and explains as they land. One virtual day passes
every two real seconds at 1x, so five days of settlement lag play out in about
ten seconds; 20x runs five virtual months in twenty.

`live.py` re-implements nothing. It keeps an in-memory book — open orders, open
bank lines — and each tick hands that book to the same `match_exact`,
`match_fuzzy`, `match_batch`, `verify_fees` and `build_exceptions` functions the
batch report calls. The confidence scores on screen are `engine.challenge()`
output; the `VERIFY` lines in the agent feed are its actual signal list. The
only thing the module invents is *when* records appear.

Four scenario buttons inject prepared cases into the same live stream — no mode
switch, no state reset. Verified end to end through the SSE stream:

| Button | What the engine actually does |
|---|---|
| Lookalike | two orders share an amount, one credit names neither → demoted at **conf 0.55**, "amount is not unique: 2 open orders fit ₹5,652.12 equally well" |
| Illegal MDR | UPI line carrying 0.90% MDR → `ZERO_MDR_VIOLATION`, **₹89.93** overcharge |
| Batch deposit | 6 orders, one lump credit, no breakdown → subset-sum decomposes all 6 at **conf 0.90** |
| Late refund | refund sits as `LATE_REFUND`, then resolves when the bank line lands at T+4 |

A demoted pair is quarantined from further matching — otherwise every tick would
re-match and re-demote it — but stays open for exception building, which is
exactly how `reconcile()` reports it.

**The explanation layer degrades honestly.** Each exception is labelled `LLM,
past the number guard` or `rules engine` in the feed. `explain.enrich()` calls a
local Ollama model, one request per exception, 5s timeout. If Ollama is down,
slow, returns a non-200, or replies with something that is not the JSON we asked
for, the deterministic wording shows and says so. It never claims model output
it did not get.

Measured over ~110s of live run with Ollama up: **15 LLM explanations, 18 rules
fallbacks**. With the endpoint pointed at a dead port: **7/7 rules, zero
tracebacks, engine unaffected** (171 matches over the same window).

## Load a real Razorpay report

`python ingest_razorpay.py` reconciles `samples/razorpay_settlement_sample.csv`
(30 real-shape rows, 3 payouts, planted fee variance + illegal UPI MDR +
adjustment + transfer) with zero pre-processing, or hit **Load Razorpay CSV** on
the dashboard to upload your own export — 28/28 matched, ₹121.14 leakage, in
0.001s.

Three translation decisions worth knowing, all in `ingest_razorpay.py`:

- **Money stays in integer paise.** Razorpay reports paise and the engine's money
  path is integer paise end to end. Converting to rupee floats would introduce
  exactly the drift that must be avoided, so the correct conversion is none.
- **Razorpay's `fee` bundles MDR and platform fee.** Booking it all as MDR would
  fire the statutory 0% check on *every* UPI and RuPay row, since the legitimate
  0.2% platform fee lives inside it. The adapter splits the contracted platform
  fee back out; the residual is the real MDR. They still sum to `fee`, so the
  total-variance check is untouched.
- **The documented schema carries no payment method**, but the method decides the
  contracted MDR and the settlement window. The adapter uses a `method` column
  when the export has one, else sniffs `description`/`notes`, else falls back to
  `DEFAULT_METHOD`. A wrong guess surfaces as a fee variance, so check that
  column mapping before trusting leakage numbers on a real report.

## Receive live Razorpay webhooks

`POST /webhook/razorpay` takes Razorpay's real `settlement.processed` payload,
verifies `X-Razorpay-Signature` (HMAC-SHA256 over the raw body against
`RAZORPAY_WEBHOOK_SECRET`), and pushes it onto the live event bus tagged **RZP**
in the bank column. It verifies, enqueues and returns in ~9ms — Razorpay retries
anything slow or non-2xx. With no secret set it runs in dev mode, skips
verification and warns once on the console.

```bash
python app.py            # one terminal
python test_webhook.py   # another: forged, valid and malformed cases
```

Measured: forged → 400, valid → 200 in 9ms, malformed → 400. Note that
`settlement.processed` means the payout was *initiated*; funds land within about
three hours, so the event is advance notice of a credit the engine must still
match.

## Honest limits

- One refunded order carries only its refund line, not an original credit and a
  later reversal; the matcher handles the refund leg, not the round trip.
- Batch decomposition is subset-sum over the open orders in the window, with a
  second search to check the solution is unique. It is exponential in principle;
  at ~25 candidates with paise-level amounts it is instant. A real book would
  need candidate windowing before this scales.
- The ambiguity ceiling costs recall on purpose. One real match is reported as
  unresolved because the file genuinely does not prove it. That is the correct
  trade in finance and it is counted openly as a miss, not hidden.
- Live mode rescans the whole book every tick, so it is O(n²) in records seen —
  4.4ms at 277 records, irrelevant at demo scale, but a demo left at 20x for an
  hour would need settled records retired into a closed ledger first.
- Ollama's first request for a model pays the load cost — measured at ~40s for
  `qwen2.5:3b` against ~3.1s warm. That is far past the 5s timeout, so `live.py`
  fires `explain.warmup()` on a background thread at startup. Without it a cold
  demo spends its first minute falling back to rules wording for no reason.
- ~3.1s per explanation is close to the 5s ceiling. A larger local model will
  cross it and silently fall back to rules on every call; check the label mix in
  the feed after changing `OLLAMA_MODEL`.
- `report.py --explain` calls the model once per exception, so it takes about a
  minute over the 20-exception batch. The report without `--explain` is complete
  and instant.

## Files

| | |
|---|---|
| `contract.json` | agreed MDR per instrument, platform fee, GST, settlement windows, thresholds |
| `generate.py` | synthetic batch with planted problems + ground-truth answer key |
| `engine.py` | ingest, three matching passes, self-verification, fee verification, exceptions |
| `report.py` | accuracy scoring against the key, ranked exception report, JSON dump |
| `explain.py` | optional LLM prose layer with the invented-number guard |
| `live.py` | virtual clock, event bus, autoplay driver, four scenario injectors |
| `live_page.html` | three-column live UI: merchant, agent reasoning, bank + exceptions |
| `app.py` | Flask: `/` live demo, `/stream` SSE, `/inject/*`, `/speed`, `/batch` report |
| `test_engine.py` | fee math, challenge logic, subset-sum, end-to-end vs the key, cross-process determinism |
| `ingest_razorpay.py` | Razorpay settlement-report adapter + isolated reconcile over an import |
| `webhook_razorpay.py` | signed `settlement.processed` receiver, Flask blueprint |
| `test_webhook.py` | forged / valid / malformed webhook cases against a running server |
| `samples/` | 30-row real-shape Razorpay export with planted issues |

All money is integer paise. There is no float anywhere in the money path.
