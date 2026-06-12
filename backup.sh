#!/usr/bin/env bash
# Daily Postgres backup → local + optional offsite (Backblaze B2 via rclone).
# Cron example (server):  0 3 * * *  /opt/gubernator/backup.sh >> /var/log/gubernator-backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")"

STAMP=$(date +%Y%m%d_%H%M%S)
DIR=./backups
mkdir -p "$DIR"
FILE="$DIR/gubernator_$STAMP.sql.gz"

# Dump from the running db container
docker compose exec -T db pg_dump -U "${POSTGRES_USER:-gubernator}" "${POSTGRES_DB:-gubernator}" | gzip > "$FILE"
echo "[$(date)] wrote $FILE"

# Keep last 14 local backups
ls -1t "$DIR"/gubernator_*.sql.gz | tail -n +15 | xargs -r rm -f

# Optional offsite via rclone (configure an 'b2' remote first: rclone config)
if command -v rclone >/dev/null 2>&1 && [ -n "${B2_BUCKET:-}" ]; then
  rclone copy "$FILE" "b2:${B2_BUCKET}/" && echo "[$(date)] uploaded to b2:${B2_BUCKET}"
fi
