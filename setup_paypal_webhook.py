#!/usr/bin/env python3
"""
Create (or reuse) the PayPal webhook for this deployment and print its ID.
Run after the site is live at a public HTTPS URL:
    docker compose exec app python setup_paypal_webhook.py
Then put the printed PAYPAL_WEBHOOK_ID into .env and reload.
"""
import asyncio, sys
sys.path.insert(0, ".")
from backend import paypal
from backend.config import APP_BASE_URL, PAYPAL_ENV


async def main():
    url = APP_BASE_URL.rstrip("/") + "/api/webhook/paypal"
    print(f"PayPal env: {PAYPAL_ENV}  |  webhook URL: {url}")
    wid = await paypal.ensure_webhook(url)
    print("\n✓ Webhook ready. Put this in .env then reload:\n")
    print(f"  PAYPAL_WEBHOOK_ID={wid}\n")


if __name__ == "__main__":
    asyncio.run(main())
