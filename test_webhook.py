"""Exercise the Razorpay webhook endpoint without Razorpay.

    python app.py                 # in one terminal
    python test_webhook.py        # in another

Covers both signature cases. With RAZORPAY_WEBHOOK_SECRET set in the server's
environment, the invalid case must be rejected with 400. Without it the server
is in dev mode and accepts everything, which this script reports rather than
fails on -- an unset secret is a configuration state, not a bug.
"""
import hashlib
import hmac
import json
import os
import sys
import time

import requests

URL = "http://127.0.0.1:5000/webhook/razorpay"
SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "whsec_demo_secret")

PAYLOAD = {
    "entity": "event",
    "account_id": "acc_OyQ1mLpKd83Xz",
    "event": "settlement.processed",
    "contains": ["settlement"],
    "payload": {
        "settlement": {
            "entity": {
                "id": "setl_OyRk4TzE1Wq7Ab",
                "entity": "settlement",
                "amount": 200000,
                "status": "processed",
                "fees": 400,
                "tax": 72,
                "utr": "HDFC24101800391",
                "created_at": 1729675800,
            }
        }
    },
    "created_at": 1729675800,
}


def post(body, signature):
    return requests.post(URL, data=body, timeout=10, headers={
        "Content-Type": "application/json", "X-Razorpay-Signature": signature})


def main():
    body = json.dumps(PAYLOAD).encode()
    good = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()

    # Whether signatures are verified is a property of the SERVER's environment,
    # not this script's. Probe it with a forged signature instead of reading our
    # own env, which says nothing about how the server was started.
    try:
        r = post(body, "0" * 64)
    except requests.exceptions.ConnectionError:
        sys.exit("no server on {} -- start it with `python app.py` first".format(URL))
    verifying = r.status_code == 400
    print("invalid signature -> {} {}".format(r.status_code, r.text.strip()))

    t0 = time.perf_counter()
    r = post(body, good)
    ms = (time.perf_counter() - t0) * 1000
    print("valid signature   -> {} {}  ({:.0f}ms)".format(r.status_code, r.text.strip(), ms))
    assert ms < 1000, "handler must return fast; Razorpay retries slow endpoints"

    if verifying:
        print("server IS verifying signatures")
        assert r.status_code == 200, (
            "a correctly signed webhook was rejected -- this script signs with "
            "{!r}; it must match the server's RAZORPAY_WEBHOOK_SECRET".format(SECRET))
        print("forged signature correctly rejected, valid one accepted")
    else:
        assert r.status_code == 200, "dev mode must accept anything well-formed"
        print("\nNOTE: the SERVER has no RAZORPAY_WEBHOOK_SECRET set, so it is in dev\n"
              "mode and does not verify signatures. To prove rejection, restart the\n"
              "server with the secret set and run this again:\n"
              '  $env:RAZORPAY_WEBHOOK_SECRET = "whsec_demo_secret"   # PowerShell\n'
              "  export RAZORPAY_WEBHOOK_SECRET=whsec_demo_secret     # bash")

    r = post(b'{"entity":"event","payload":{}}',
             hmac.new(SECRET.encode(), b'{"entity":"event","payload":{}}',
                      hashlib.sha256).hexdigest())
    print("malformed payload -> {} {}".format(r.status_code, r.text.strip()))
    assert r.status_code == 400, "a payload with no settlement entity must be rejected"

    print("\nwebhook checks passed")


if __name__ == "__main__":
    main()
