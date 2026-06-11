#!/usr/bin/env python3
"""
One-time: create the PayPal Product + $29/mo Plan (with 14-day free trial).
Prints the plan ID — copy it into .env as PAYPAL_PLAN_ID, then restart.

Run:  .venv/bin/python setup_paypal_plan.py
"""
import asyncio, sys
sys.path.insert(0, ".")
from backend import paypal
from backend.config import PAYPAL_ENV, PLAN_PRICE_USD, TRIAL_DAYS


async def main():
    print(f"PayPal env: {PAYPAL_ENV}  |  price: ${PLAN_PRICE_USD}/mo  |  trial: {TRIAL_DAYS} days")
    # Reuse an existing product if its ID is passed (avoids creating duplicates on retry)
    if len(sys.argv) > 1 and sys.argv[1].startswith("PROD-"):
        product_id = sys.argv[1]
        print("Reusing product:", product_id)
    else:
        print("Creating product…")
        product_id = await paypal.create_product()
        print("  product:", product_id)
    print("Creating plan…")
    plan_id = await paypal.create_plan(product_id)
    print("  plan:", plan_id)
    print("\n✓ Done. Put this in .env then restart:")
    print(f"\n  PAYPAL_PLAN_ID={plan_id}\n")


if __name__ == "__main__":
    asyncio.run(main())
