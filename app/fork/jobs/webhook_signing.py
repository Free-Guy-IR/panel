import hashlib
import hmac
import json


def webhook_request(payload, secret):
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["x-webhook-secret"] = secret
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["x-webhook-signature"] = f"sha256={digest}"
    return body, headers
