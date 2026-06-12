# Deploying Gubernator (Hetzner Cloud CAX21 / ARM)

Stack: Docker Compose → Caddy (auto-HTTPS) → FastAPI app → Postgres (pgvector).
Everything below runs the same on amd64 or arm64.

## 1. Create the server
- Hetzner Cloud → **Add Server** → Location (EU or Ashburn) → Image **Ubuntu 24.04**
  → Type **CAX21** (Arm, 4 vCPU / 8 GB) → add your SSH key → Create.
- Note the public IP.

## 2. DNS
At your DNS host, add an **A record**: `app.gubernator.co` → `<server-ip>`.
(Wait for it to resolve before step 6 so Caddy can get a cert.)

## 3. Install Docker on the server
```bash
ssh root@<server-ip>
curl -fsSL https://get.docker.com | sh
```

## 4. Get the code + config
```bash
mkdir -p /opt/gubernator && cd /opt/gubernator
git clone -b prod https://github.com/doublejsa/Gubernator.git .   # deploy from the prod branch
cp .env.production.example .env
nano .env        # fill EVERYTHING in (see below), then: chmod 600 .env
```
Fill in `.env`:
- `POSTGRES_PASSWORD` + the same value inside `DATABASE_URL` (host stays `db`)
- `SECRET_KEY`, `VAULT_MASTER_KEY` — `python3 -c "import secrets; print(secrets.token_hex(32))"` each
- `RESEND_API_KEY`, `EMAIL_FROM`
- PayPal **live** keys (`PAYPAL_ENV=live`, client id + secret) — leave `PAYPAL_PLAN_ID` empty for now

Edit `Caddyfile` — set your domain + email.

## 5. Build & start
```bash
docker compose up -d --build
docker compose logs -f app      # watch it boot; Ctrl-C to stop tailing
```
The app refuses to start if secrets are weak (GUBERNATOR_ENV=production guard).

## 6. Create the live PayPal plan
```bash
docker compose exec app python setup_paypal_plan.py
```
Copy the printed `PAYPAL_PLAN_ID=...` into `.env`, then `docker compose up -d` to reload.

## 7. PayPal webhook (now that you have a public HTTPS URL)
- PayPal dashboard → your **live** app → **Webhooks** → Add
  `https://app.gubernator.co/api/webhook/paypal`
- Subscribe to BILLING.SUBSCRIPTION.* and PAYMENT.SALE.* events
- Copy the **Webhook ID** into `.env` as `PAYPAL_WEBHOOK_ID`, then `docker compose up -d`.

## 8. Cron jobs (on the host)
```bash
crontab -e
```
```
# Daily trial-ending reminder emails
0 9 * * *  cd /opt/gubernator && docker compose exec -T app python send_trial_reminders.py
# Daily DB backup
0 3 * * *  cd /opt/gubernator && ./backup.sh >> /var/log/gubernator-backup.log 2>&1
```
For offsite backups: `apt install rclone`, `rclone config` a Backblaze B2 remote named `b2`,
set `B2_BUCKET` in `.env`.

## 9. Verify
- Visit `https://app.gubernator.co` — valid HTTPS, login page loads.
- Register → confirm the verification email arrives (Resend).
- Subscribe with a real (or sandbox→live) PayPal account → app unlocks.

## Release workflow (main → prod)
Develop on `main`. When a change is tested and ready to ship:
```bash
# on your machine
git checkout prod && git merge main && git push origin prod
git checkout main
```
Then update the server:
```bash
cd /opt/gubernator && git pull && docker compose up -d --build
```
The server only ever tracks `prod`, so live never picks up half-finished `main` work.

## Security reminders (see SECURITY.md)
- `.env` is `chmod 600`, never committed.
- Consider a firewall: allow 22/80/443 only (`ufw allow 22,80,443/tcp && ufw enable`).
- Back up off-box (step 8) — you hold users' encrypted credentials.
