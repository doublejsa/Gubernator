"""PayPal REST client — OAuth, subscriptions, plan setup, webhook verification."""
from __future__ import annotations
import asyncio
import httpx
from backend.config import (
    PAYPAL_API_BASE, PAYPAL_CLIENT_ID, PAYPAL_SECRET, PAYPAL_WEBHOOK_ID,
    PLAN_PRICE_USD, TRIAL_DAYS, APP_BASE_URL,
)

_RETRIES = 4   # retry transient connect/timeout errors


async def _token() -> str:
    last = None
    for attempt in range(_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=45.0)) as c:
                r = await c.post(
                    f"{PAYPAL_API_BASE}/v1/oauth2/token",
                    data={"grant_type": "client_credentials"},
                    auth=(PAYPAL_CLIENT_ID, PAYPAL_SECRET),
                )
                r.raise_for_status()
                return r.json()["access_token"]
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            last = e
            await asyncio.sleep(2 * (attempt + 1))
    raise RuntimeError(f"PayPal token failed after {_RETRIES} attempts: {last}")


async def _api(method: str, path: str, json=None, headers=None, token: str | None = None) -> dict:
    tok = token or await _token()
    h = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    if headers:
        h.update(headers)
    last = None
    for attempt in range(_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=45.0)) as c:
                r = await c.request(method, f"{PAYPAL_API_BASE}{path}", json=json, headers=h)
                if r.status_code >= 400:
                    raise RuntimeError(f"PayPal {method} {path} → {r.status_code}: {r.text}")
                return r.json() if r.text else {}
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            last = e
            await asyncio.sleep(2 * (attempt + 1))
    raise RuntimeError(f"PayPal {method} {path} failed after {_RETRIES} attempts: {last}")


# ── One-time setup: product + plan (run via setup_paypal_plan.py) ─────────────
async def create_product() -> str:
    data = await _api("POST", "/v1/catalogs/products", json={
        "name": "Gubernator",
        "description": "Gubernator — AI Agent Control Panel subscription",
        "type": "SERVICE",
        "category": "SOFTWARE",
    })
    return data["id"]

async def create_plan(product_id: str) -> str:
    cycles = []
    seq = 1
    if TRIAL_DAYS > 0:
        cycles.append({   # Free trial cycle
            "frequency": {"interval_unit": "DAY", "interval_count": TRIAL_DAYS},
            "tenure_type": "TRIAL", "sequence": seq, "total_cycles": 1,
            "pricing_scheme": {"fixed_price": {"value": "0", "currency_code": "USD"}},
        })
        seq += 1
    cycles.append({   # Regular monthly billing, indefinite
        "frequency": {"interval_unit": "MONTH", "interval_count": 1},
        "tenure_type": "REGULAR", "sequence": seq, "total_cycles": 0,
        "pricing_scheme": {"fixed_price": {"value": PLAN_PRICE_USD, "currency_code": "USD"}},
    })
    trial_txt = f" after a {TRIAL_DAYS}-day free trial" if TRIAL_DAYS > 0 else ""
    data = await _api("POST", "/v1/billing/plans", json={
        "product_id": product_id,
        "name": f"Gubernator Monthly (${PLAN_PRICE_USD})",
        "description": f"Gubernator monthly subscription — ${PLAN_PRICE_USD}/mo{trial_txt}",
        "status": "ACTIVE",
        "billing_cycles": cycles,
        "payment_preferences": {
            "auto_bill_outstanding": True,
            "setup_fee": {"value": "0", "currency_code": "USD"},
            "setup_fee_failure_action": "CONTINUE",
            "payment_failure_threshold": 2,
        },
    })
    return data["id"]


# ── Runtime ───────────────────────────────────────────────────────────────────
async def get_subscription(sub_id: str) -> dict:
    return await _api("GET", f"/v1/billing/subscriptions/{sub_id}")

async def cancel_subscription(sub_id: str, reason: str = "User requested cancellation") -> None:
    await _api("POST", f"/v1/billing/subscriptions/{sub_id}/cancel", json={"reason": reason})


WEBHOOK_EVENTS = [
    "BILLING.SUBSCRIPTION.ACTIVATED",
    "BILLING.SUBSCRIPTION.CANCELLED",
    "BILLING.SUBSCRIPTION.SUSPENDED",
    "BILLING.SUBSCRIPTION.EXPIRED",
    "BILLING.SUBSCRIPTION.PAYMENT.FAILED",
    "PAYMENT.SALE.COMPLETED",
    "PAYMENT.SALE.DENIED",
]

async def ensure_webhook(url: str) -> str:
    """Create (or reuse) a webhook for `url`, return its id. Idempotent."""
    existing = await _api("GET", "/v1/notifications/webhooks")
    for wh in existing.get("webhooks", []):
        if wh.get("url") == url:
            return wh["id"]
    data = await _api("POST", "/v1/notifications/webhooks", json={
        "url": url,
        "event_types": [{"name": e} for e in WEBHOOK_EVENTS],
    })
    return data["id"]


async def verify_webhook(headers: dict, body: dict) -> bool:
    """Verify a webhook came from PayPal. Returns True if SUCCESS (or if no
    webhook ID configured yet — dev fallback)."""
    if not PAYPAL_WEBHOOK_ID:
        return True   # dev: simulator / not yet configured
    try:
        result = await _api("POST", "/v1/notifications/verify-webhook-signature", json={
            "auth_algo":         headers.get("paypal-auth-algo"),
            "cert_url":          headers.get("paypal-cert-url"),
            "transmission_id":   headers.get("paypal-transmission-id"),
            "transmission_sig":  headers.get("paypal-transmission-sig"),
            "transmission_time": headers.get("paypal-transmission-time"),
            "webhook_id":        PAYPAL_WEBHOOK_ID,
            "webhook_event":     body,
        })
        return result.get("verification_status") == "SUCCESS"
    except Exception:
        return False
