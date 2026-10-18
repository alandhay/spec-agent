"""Real top-ups, through Stripe Checkout. A prototype: Stripe's test mode is the intended use.

How a top-up goes:

1. The page asks for a top-up (POST /api/topup {amount}). With Stripe set up, the server makes a
   Checkout Session for that amount, tagged with the person's id, and the page goes to Stripe's
   own payment page. Card details never reach this server.
2. The person pays (or gives up) and Stripe sends them back to the page (/?paid=...). That return
   is only a courtesy: it proves nothing and grants nothing.
3. Stripe tells the server separately, with a signed webhook (POST /api/stripe/webhook,
   event checkout.session.completed). Only a correctly signed event for a session that is paid
   adds the credit, and each session is credited once, however often Stripe repeats the event.

Settings (hosted, in /etc/specdev.env through deploy.py; never in the repository):
    SPECDEV_STRIPE_SECRET_KEY      sk_test_... (or sk_live_... when it is real)
    SPECDEV_STRIPE_WEBHOOK_SECRET  whsec_..., from the webhook endpoint in Stripe's dashboard
The names start SPECDEV_ so that the sandbox keeps them from the agent's processes.
Without them the old behaviour stays: a top-up is simply granted (your own machine, demos).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.stripe.com/v1"
TOLERANCE_S = 300                       # how old a webhook's signature may be


class PaymentError(Exception):
    """Something went wrong talking to Stripe, or a webhook is not to be trusted; worded for a log."""


class Stripe:
    def __init__(self, secret_key: str, webhook_secret: str, *, currency: str = "usd", send=None) -> None:
        self.secret_key, self.webhook_secret, self.currency = secret_key, webhook_secret, currency
        self._send = send or self._http           # (path, form fields) -> parsed JSON; replaced in tests

    @property
    def test_mode(self) -> bool:
        return self.secret_key.startswith(("sk_test_", "rk_test_"))

    def _http(self, path: str, fields: dict[str, str]) -> dict:
        request = urllib.request.Request(f"{API}{path}", data=urllib.parse.urlencode(fields).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {self.secret_key}"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail)["error"]["message"]
            except (ValueError, KeyError, TypeError):
                pass
            raise PaymentError(f"Stripe said no ({exc.code}): {detail}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise PaymentError(f"could not reach Stripe: {exc}") from None

    def checkout(self, user: str, email: str, amount: float, success_url: str, cancel_url: str) -> str:
        """Start paying ``amount`` (in dollars) of credit for ``user``; return the address of Stripe's payment page."""
        cents = round(amount * 100)
        fields = {
            "mode": "payment",
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": self.currency,
            "line_items[0][price_data][unit_amount]": str(cents),
            "line_items[0][price_data][product_data][name]": "SpecDev workbench credit",
            "line_items[0][price_data][product_data][description]": f"${amount:.2f} of agent time on the ZX Spectrum workbench",
            "client_reference_id": user,
            "metadata[user]": user,
            "metadata[credit_cents]": str(cents),
            "success_url": success_url,
            "cancel_url": cancel_url,
        }
        if email:
            fields["customer_email"] = email
        session = self._send("/checkout/sessions", fields)
        if not session.get("url"):
            raise PaymentError("Stripe made no payment page")
        return session["url"]

    def verify(self, payload: bytes, header: str, now: float | None = None) -> dict:
        """The webhook's event, if Stripe signed it with our secret recently; else PaymentError."""
        parts: dict[str, list[str]] = {}
        for item in (header or "").split(","):
            key, _, value = item.strip().partition("=")
            parts.setdefault(key, []).append(value)
        try:
            stamp = int(parts["t"][0])
        except (KeyError, ValueError):
            raise PaymentError("the webhook has no usable signature") from None
        expected = hmac.new(self.webhook_secret.encode(), f"{stamp}.".encode() + payload, hashlib.sha256).hexdigest()
        if not any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", [])):
            raise PaymentError("the webhook's signature does not match")
        if abs((now or time.time()) - stamp) > TOLERANCE_S:
            raise PaymentError("the webhook's signature is too old")
        try:
            return json.loads(payload)
        except ValueError:
            raise PaymentError("the webhook is not JSON") from None


def credit_from(event: dict) -> tuple[str, float, str] | None:
    """(user, dollars, session id) for a completed, paid Checkout Session event; None for anything else."""
    if event.get("type") not in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        return None
    session = (event.get("data") or {}).get("object") or {}
    if session.get("payment_status") != "paid":
        return None                       # e.g. a bank transfer still on its way: its success event comes later
    metadata = session.get("metadata") or {}
    user = metadata.get("user") or session.get("client_reference_id")
    # The credit is what was sold (kept on the session when it was made), not what was charged,
    # which would also include any tax added at checkout.
    try:
        cents = int(metadata.get("credit_cents") or session.get("amount_total") or 0)
    except (TypeError, ValueError):
        return None
    if not user or cents <= 0 or not session.get("id"):
        return None
    return user, cents / 100, session["id"]
