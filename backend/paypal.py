"""PayPal REST client — OAuth, subscriptions, plan setup, webhook verification."""
from __future__ import annotations
import httpx
from backend.config import (
    PAYPAL_API_BASE, PAYPAL_CLIENT_ID, PAYPAL_SECRET, PAYPAL_WEBHOOK_ID,
    PLAN_PRICE_USD, TRIAL_DAYS, APP_BASE_URL,
)


async def _token() -> str:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            f"{PAYPAL_API_BASE}/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth=(PAYPAL_CLIENT_ID, PAYPAL_SECRET),
        )
        r.raise_for_status()
        return r.json()["access_token"]


async def _api(method: str, path: str, json=None, headers=None) -> dict:
    tok = await _token()
    h = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    if headers:
        h.update(headers)
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.request(method, f"{PAYPAL_API_BASE}{path}", json=json, headers=h)
        if r.status_code >= 400:
            raise RuntimeError(f"PayPal {method} {path} → {r.status_code}: {r.text}")
        return r.json() if r.text else {}


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
    data = await _api("POST", "/v1/billing/plans", json={
        "product_id": product_id,
        "name": "Gubernator Monthly",
        "description": f"Gubernator monthly subscription — ${PLAN_PRICE_USD}/mo after a {TRIAL_DAYS}-day free trial",
        "status": "ACTIVE",
        "billing_cycles": [
            {   # Free trial cycle
                "frequency": {"interval_unit": "DAY", "interval_count": TRIAL_DAYS},
                "tenure_type": "TRIAL", "sequence": 1, "total_cycles": 1,
                "pricing_scheme": {"fixed_price": {"value": "0", "currency_code": "USD"}},
            },
            {   # Regular monthly billing, indefinite
                "frequency": {"interval_unit": "MONTH", "interval_count": 1},
                "tenure_type": "REGULAR", "sequence": 2, "total_cycles": 0,
                "pricing_scheme": {"fixed_price": {"value": PLAN_PRICE_USD, "currency_code": "USD"}},
            },
        ],
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
