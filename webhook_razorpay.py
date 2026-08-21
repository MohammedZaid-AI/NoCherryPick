"""Razorpay webhook receiver.

Takes Razorpay's real `settlement.processed` payload, verifies it, and drops it
onto the live event bus the dashboard is already listening to. The handler does
no reconciliation work: Razorpay retries anything that is not a 2xx, so it
verifies, enqueues and returns.

`settlement.processed` means Razorpay has initiated the payout, not that the
money has landed. Funds reach the merchant bank within about three hours over
NEFT/RTGS/IMPS, so this event is an advance notice -- the bank credit it
promises is what the engine will later have to match.
"""
import hashlib
import hmac
import json
import os

from flask import Blueprint, request

from live import BUS

bp = Blueprint("razorpay_webhook", __name__)
SECRET_ENV = "RAZORPAY_WEBHOOK_SECRET"
_warned = False


def verify(raw_body, signature, secret):
    """Razorpay signs the raw request body with HMAC-SHA256, hex digest."""
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, (signature or "").strip())


@bp.post("/webhook/razorpay")
def receive():
    global _warned
    raw = request.get_data()
    secret = os.environ.get(SECRET_ENV)

    if secret:
        if not verify(raw, request.headers.get("X-Razorpay-Signature"), secret):
            return {"ok": False, "error": "signature mismatch"}, 400
    elif not _warned:
        _warned = True
        print("WARNING: {} is not set -- webhook signatures are NOT being "
              "verified. Dev mode only; never run this way against a real "
              "Razorpay account.".format(SECRET_ENV))

    try:
        body = json.loads(raw)
        entity = body["payload"]["settlement"]["entity"]
    except (ValueError, KeyError, TypeError):
        return {"ok": False, "error": "unrecognised payload"}, 400

    # Amounts stay in integer paise, like every other event on this bus -- the
    # page's money() helper does the rupee formatting at render time.
    BUS.emit("rzp_settlement",
             type="RZP_SETTLEMENT_PROCESSED",
             event=body.get("event", "settlement.processed"),
             settlement_id=entity.get("id", ""),
             utr=entity.get("utr", ""),
             amount=int(entity.get("amount", 0)),
             fees=int(entity.get("fees", 0)),
             tax=int(entity.get("tax", 0)),
             status=entity.get("status", ""),
             created_at=int(entity.get("created_at", 0)))
    return {"ok": True}, 200
