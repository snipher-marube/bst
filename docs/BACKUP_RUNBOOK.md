# AnalyticsMeta — Database Backup & Restoration Runbook

> Last updated: 2026-04-15
> Addresses Gap 17 of the Gap Analysis

---

## Overview

| Component | Backup method | Frequency | Retention |
|-----------|---------------|-----------|-----------|
| PostgreSQL (primary) | `pg_dump` + WAL archiving | Daily full + continuous WAL | 7 daily, 4 weekly |
| Media files (PDF reports) | `rsync` to object store | Daily | 30 days |
| Redis (Celery broker) | Not backed up — volatile only | — | — |

---

## 1. PostgreSQL Daily Backup (pg_dump)

### Cron job (run as `postgres` or app user)

```bash
# /etc/cron.d/analyticsmeta-backup
0 2 * * * postgres /opt/analyticsmeta/scripts/pg_backup.sh >> /var/log/pg_backup.log 2>&1
```

### Backup script `/opt/analyticsmeta/scripts/pg_backup.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail

DB_NAME="${POSTGRES_DB:-analyticsmeta}"
DB_USER="${POSTGRES_USER:-postgres}"
BACKUP_DIR="${BACKUP_DIR:-/backups/postgres}"
RETENTION_DAYS=7
RETENTION_WEEKS=4
DATE=$(date +%Y-%m-%d)

mkdir -p "${BACKUP_DIR}/daily" "${BACKUP_DIR}/weekly"

# Daily dump (custom format — supports parallel restore)
DUMP_FILE="${BACKUP_DIR}/daily/${DB_NAME}_${DATE}.dump"
pg_dump -U "${DB_USER}" -Fc "${DB_NAME}" -f "${DUMP_FILE}"
echo "[$(date -u)] Backup written: ${DUMP_FILE} ($(du -sh ${DUMP_FILE} | cut -f1))"

# Weekly copy (every Sunday)
if [ "$(date +%u)" = "7" ]; then
    cp "${DUMP_FILE}" "${BACKUP_DIR}/weekly/${DB_NAME}_week$(date +%V).dump"
fi

# Prune old daily backups
find "${BACKUP_DIR}/daily" -name "*.dump" -mtime "+${RETENTION_DAYS}" -delete

# Prune old weekly backups (keep 4 weeks = 28 days)
WEEKLY_RETENTION=$(( RETENTION_WEEKS * 7 ))
find "${BACKUP_DIR}/weekly" -name "*.dump" -mtime "+${WEEKLY_RETENTION}" -delete

echo "[$(date -u)] Backup rotation complete."
```

---

## 2. WAL Archiving (Point-in-Time Recovery)

Add to `postgresql.conf`:

```
wal_level = replica
archive_mode = on
archive_command = 'rsync -a %p /backups/wal/%f'
restore_command = 'cp /backups/wal/%f %p'
```

For production, replace `rsync` target with an S3 path using `aws s3 cp` or
`wal-g archive-push`.

---

## 3. Render.com Managed Backups

In `render.yaml`, the PostgreSQL service should include:

```yaml
databases:
  - name: analyticsmeta-db
    plan: standard
    backupRetentionDays: 7
```

Render retains daily snapshots automatically. To restore:
1. Open the Render Dashboard → Your PostgreSQL service
2. Click **Backups** tab
3. Select the snapshot and click **Restore**
4. A new database instance is provisioned — update `DATABASE_URL` in the
   Environment settings of your web service and redeploy.

---

## 4. Media Files (PDF Reports)

PDF reports are stored in `MEDIA_ROOT/reports/`. Back up to object storage daily:

```bash
# /etc/cron.d/analyticsmeta-media-backup
30 2 * * * app rsync -a --delete /srv/media/reports/ s3://your-bucket/media-backup/reports/
```

Or using `rclone`:

```bash
rclone sync /srv/media/reports/ remote:analyticsmeta-media/reports/ --log-file=/var/log/media_backup.log
```

---

## 5. Restoration Procedure

### 5.1 Full restore from pg_dump

```bash
# 1. Stop application servers (prevent writes during restore)
systemctl stop gunicorn celery-worker celery-beat

# 2. Drop and recreate the target database
psql -U postgres -c "DROP DATABASE IF EXISTS analyticsmeta_restore;"
psql -U postgres -c "CREATE DATABASE analyticsmeta_restore OWNER analyticsmeta;"

# 3. Restore
pg_restore -U postgres -d analyticsmeta_restore --jobs 4 \
    /backups/postgres/daily/analyticsmeta_2026-04-15.dump

# 4. Verify row counts
psql -U postgres analyticsmeta_restore -c "
SELECT relname, reltuples::bigint AS rows
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
ORDER BY rows DESC LIMIT 20;"

# 5. Swap to restored DB (update DATABASE_URL or rename DBs)
# 6. Restart services
systemctl start gunicorn celery-worker celery-beat
```

### 5.2 Point-in-time recovery from WAL

```bash
# 1. Stop PostgreSQL
systemctl stop postgresql

# 2. Copy base backup to data directory
cp -a /backups/postgres/base_backup /var/lib/postgresql/14/main

# 3. Create recovery.conf (or postgresql.conf entries for PG 12+):
cat > /var/lib/postgresql/14/main/recovery.signal << EOF
EOF
# In postgresql.conf:
echo "restore_command = 'cp /backups/wal/%f %p'"      >> postgresql.conf
echo "recovery_target_time = '2026-04-15 01:30:00'"   >> postgresql.conf

# 4. Start PostgreSQL — it will replay WAL up to the target time
systemctl start postgresql
```

---

## 6. Backup Verification Checklist

Run weekly (or after any major deployment):

- [ ] Latest daily dump exists and is non-zero bytes
- [ ] `pg_restore --list` on the dump returns expected table list
- [ ] Restore to a test DB succeeds without errors
- [ ] Row counts in test DB match production
- [ ] Media backup directory is recent (`find /backups/media -mtime -1`)
- [ ] Render backups show a snapshot from the last 24 hours

---

## 7. Escalation

| Severity | Action |
|----------|--------|
| Backup job fails | Alert via Sentry (SENTRY_DSN env var) + PagerDuty |
| Last backup > 25 hours old | Page on-call engineer |
| Data loss confirmed | Follow incident runbook + notify affected workspace owners |

---

*This runbook should be reviewed after every major infrastructure change.*
