#!/usr/bin/env python3
"""
Send "your trial ends in 2 days" emails. Run daily via cron, e.g.:
    0 9 * * *  cd /path/to/Gubernator && .venv/bin/python send_trial_reminders.py

Finds trialing users whose trial ends within ~2 days and who haven't been
reminded yet, emails them, and marks trial_reminder_sent so it sends once.
"""
import asyncio, sys
sys.path.insert(0, ".")
from datetime import datetime, timedelta
from sqlalchemy import select
from backend.db import SessionLocal
from backend.models import User
from backend.email_sender import send_trial_reminder


async def main():
    now    = datetime.utcnow()
    cutoff = now + timedelta(days=2)
    sent = 0
    async with SessionLocal() as db:
        users = (await db.execute(
            select(User).where(
                User.subscription_status == "trialing",
                User.trial_reminder_sent == False,                 # noqa: E712
                User.trial_ends_at.isnot(None),
                User.trial_ends_at <= cutoff,
                User.trial_ends_at >= now,
            )
        )).scalars().all()
        for u in users:
            ends = u.trial_ends_at.strftime("%d %B %Y")
            if await send_trial_reminder(u.email, ends):
                u.trial_reminder_sent = True
                sent += 1
                print(f"  reminded {u.email} (ends {ends})")
        await db.commit()
    print(f"✓ Sent {sent} trial reminder(s).")


if __name__ == "__main__":
    asyncio.run(main())
