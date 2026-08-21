"""LLM explanation layer -- strictly outside the arithmetic path.

Runs against a local Ollama instance. The model is handed facts that are already
computed and is asked for two things only: prose, and a category for exceptions
the rules code labelled UNKNOWN. It cannot change a code, an amount, a
confidence score or a ranking.

Then it is not trusted with that either: every reply is scanned for digits, and
any number the model wrote that does not already appear in the deterministic
facts causes its text to be thrown away and the rules-generated wording kept.
A hallucinated figure in a finance report is a silent, expensive bug, so the
guard fails closed.

Entirely optional, and deliberately unreliable-tolerant. If Ollama is not
running, is slow, returns a non-200, or replies with something that is not the
JSON we asked for, every exception silently keeps its deterministic wording --
which was already complete. The engine's correctness never depends on a model
being reachable.
"""
import json
import re

import requests

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen2.5:3b"
TIMEOUT_SECONDS = 5

CATEGORIES = ["LATE_SETTLEMENT", "MISSING_BANK_CREDIT", "LATE_REFUND", "DUPLICATE_RETRY",
              "FEE_VARIANCE", "ZERO_MDR_VIOLATION", "NET_DEPOSIT_UNRESOLVED",
              "LOW_CONFIDENCE_MATCH", "RAZORPAY_ADJUSTMENT", "RAZORPAY_TRANSFER",
              "UNKNOWN"]

SYSTEM = """You are writing exception notes for a payments reconciliation report \
that a finance controller will act on.

For the exception you are given, return a one or two sentence plain-English \
explanation a non-technical finance manager would understand.

Absolute rules:
- Never state a number, amount, percentage, date or count that is not already \
present verbatim in the facts you were given. Do not compute anything. Do not \
round, convert, total or estimate. If you are unsure, write prose with no digits.
- Never contradict the given reason code.
- For an exception whose code is UNKNOWN, also pick the best fitting category \
from the provided list, or leave it UNKNOWN.

Reply with JSON only, no other text: {"explanation": "...", "category": "..."}"""


def _digits(s):
    return set(re.findall(r"\d+", s))


def _ask(prompt, model):
    """One Ollama completion, or None. Never raises, never logs a traceback.

    Connection refused, timeout, non-200, malformed JSON and a reply that is not
    the object we asked for all land in the same place: None, and the caller
    keeps the rules wording.
    """
    try:
        r = requests.post(OLLAMA_URL, timeout=TIMEOUT_SECONDS, json={
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": 120},
        })
        if r.status_code != 200:
            return None
        text = r.json().get("response", "")
        return json.loads(text[text.index("{"):text.rindex("}") + 1])
    except Exception:                          # noqa: BLE001 - fallback is the feature
        return None


def warmup(model=OLLAMA_MODEL, timeout=90):
    """Load the model into memory once, off the critical path.

    Ollama's first request for a model pays the load cost -- measured at ~40s
    for qwen2.5:3b against ~3s warm. That is far past TIMEOUT_SECONDS, so a cold
    demo would spend its first minute falling back to rules wording for no
    reason other than startup. Call this from a background thread. Failure here
    means nothing: enrich() still falls back on its own.
    """
    try:
        requests.post(OLLAMA_URL, timeout=timeout, json={
            "model": model, "prompt": "ok", "stream": False,
            "options": {"num_predict": 1}})
        return True
    except Exception:                          # noqa: BLE001
        return False


def enrich(run, model=OLLAMA_MODEL):
    """Attach model-written prose where it survives the number guard."""
    kept = rejected = 0
    # One call per exception: num_predict is 120 tokens, which is ample for a
    # single note and nowhere near enough for a batch of them. A truncated batch
    # reply is unparseable JSON, which would silently demote every explanation
    # in the batch to rules wording.
    for e in run["exceptions"]:
        facts = json.dumps(dict(record=e.record_id, code=e.code,
                                amount_rupees=e.amount_paise / 100,
                                age_days=e.age_days, rules_finding=e.explanation,
                                action=e.action, categories=CATEGORIES), indent=1)
        item = _ask(SYSTEM + "\n\nFacts:\n" + facts, model)
        if not isinstance(item, dict):
            continue                           # unreachable, slow, or off-format

        prose = str(item.get("explanation", "")).strip()
        if not prose:
            continue
        allowed = _digits(e.explanation + e.action + e.record_id + str(e.amount_paise / 100)
                          + str(e.age_days))
        if _digits(prose) - allowed:
            rejected += 1                      # invented a figure -- discard it
            continue
        e.llm_explanation = prose
        kept += 1
        cat = item.get("category")
        if e.code == "UNKNOWN" and cat in CATEGORIES and cat != "UNKNOWN":
            e.llm_category = cat

    run["llm_note"] = "{} explanations accepted, {} rejected for inventing figures".format(
        kept, rejected)
    return run


if __name__ == "__main__":
    # the guard is the part worth checking, and it needs no model at all
    class E:
        record_id, code, age_days, amount_paise = "ORD-1", "X", 3, 45600
        explanation, action = "Captured 3d ago against a T+2 window.", "Escalate."
        llm_explanation = llm_category = ""

    e = E()
    allowed = _digits(e.explanation + e.action + e.record_id + str(e.amount_paise / 100)
                      + str(e.age_days))
    assert not (_digits("The payment is 3 days old and still unpaid.") - allowed)
    assert _digits("The gateway owes you 91,200 rupees.") - allowed
    print("number guard ok")

    # an unreachable endpoint must be silent and must return None, not raise
    _url, globals()["OLLAMA_URL"] = OLLAMA_URL, "http://localhost:1/api/generate"
    assert _ask("hello", OLLAMA_MODEL) is None
    globals()["OLLAMA_URL"] = _url
    print("unreachable-endpoint fallback ok")

    run = {"exceptions": [e]}
    enrich(run)
    print(run["llm_note"], "| explanation:", e.llm_explanation or "(rules wording kept)")
