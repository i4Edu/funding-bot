# Backup and recovery procedures

This runbook covers backup creation, verification, retention, restore, and disaster recovery for the Funding Bot database.

## Scope and objectives

- **Primary datastore:** SQLite database at `BOT_DB_PATH` (default `funding_bot.db`, containers `/app/data/funding_bot.db`)
- **Backup root:** `BACKUP_DIRECTORY` (recommended `/app/data/backups`)
- **Recovery time objective (RTO):** restore service within **60 minutes** of a declared incident
- **Recovery point objective (RPO):** no more than **15 minutes** of data loss for production when WAL snapshots are healthy

## Backup strategy

### 1. Full backups

Use a nightly full backup as the recovery anchor.

- Tool: `python scripts/backup_sqlite.py --mode full`
- Mechanism: SQLite online backup API creates a transactionally consistent copy without shutting the app down
- Recommended cadence: **daily at 02:00 UTC**
- Recommended use: standard restore, migrations, quarterly recovery drills

Example:

```bash
python scripts/backup_sqlite.py \
  --db /app/data/funding_bot.db \
  --output-dir /app/data/backups \
  --mode full \
  --retention-days 35
```

### 2. Incremental backups

SQLite does not provide native page-level incremental backups like enterprise database engines. For this project, use incremental protection in one of these ways:

1. **Snapshot/object-store incrementals:** sync only changed files from `/app/data/backups` to off-site storage after each full/WAL backup.
2. **Volume snapshots:** use storage-class or CSI snapshots if the Kubernetes platform supports them.
3. **Backup artifact replication:** replicate newly created backup directories to a second region/account.

Recommended cadence:

- replicate changed backup artifacts **after every backup job**
- validate off-site replication **daily**

### 3. WAL backups

When the database runs with `PRAGMA journal_mode=WAL`, capture frequent WAL snapshots between full backups.

- Tool: `python scripts/backup_sqlite.py --mode wal`
- Mechanism: briefly pauses writers with `BEGIN IMMEDIATE`, then copies the database plus `-wal`/`-shm` sidecars
- Recommended cadence: **every 15 minutes**
- Recommended use: reduce RPO between nightly full backups

Example:

```bash
python scripts/backup_sqlite.py \
  --db /app/data/funding_bot.db \
  --output-dir /app/data/backups \
  --mode wal \
  --retention-days 7
```

## Backup scheduling

### Cron schedule for single-node or VM deployments

```cron
# Nightly full backup
0 2 * * * cd /path/to/funding-bot && python scripts/backup_sqlite.py --db "$BOT_DB_PATH" --output-dir ./backups --mode full --retention-days 35

# Frequent WAL snapshot (requires PRAGMA journal_mode=WAL)
*/15 * * * * cd /path/to/funding-bot && python scripts/backup_sqlite.py --db "$BOT_DB_PATH" --output-dir ./backups --mode wal --retention-days 7

# Daily backup verification
20 2 * * * cd /path/to/funding-bot && python scripts/verify_backup.py --latest-in ./backups
```

### Kubernetes schedule

`k8s/cronjob.yaml` includes:

- a nightly full backup job
- a 15-minute WAL backup job
- a daily backup verification job

Use the shared PVC so backups land beside the live database volume, then replicate them to off-cluster storage.

## Verification scripts

### Verify a specific backup

```bash
python scripts/verify_backup.py /app/data/backups/20260719T020000Z-full
```

### Verify the latest backup automatically

```bash
python scripts/verify_backup.py --latest-in /app/data/backups
```

Verification checks:

- every file listed in `manifest.json` exists
- SHA-256 checksums match
- `PRAGMA integrity_check` returns `ok`

## Retention policy

| Backup type | Minimum retention | Purpose |
| --- | --- | --- |
| WAL snapshots | 7 days | short-window point recovery |
| Nightly full backups | 35 days | operational restore baseline |
| Monthly promoted full backups | 12 months | audit/compliance recovery |
| Off-site replicated copies | 12 months | disaster recovery |

Retention rules:

- the backup script prunes old snapshot directories based on `--retention-days`
- promote the first successful full backup of each month to long-term storage before local pruning
- never delete the most recent verified full backup until a newer verified full backup exists in both local and off-site storage

## Restore procedures

### Before you restore

1. Declare the incident and pause scheduled jobs.
2. Stop write traffic to the application (`Deployment`, workers, or cron-based CLI runs).
3. Preserve the failed database files for forensics by copying them to an incident directory.
4. Identify the target recovery point: latest verified full backup or latest verified WAL snapshot.

### Restore from a full backup

1. Scale application writers down to zero or stop the service.
2. Verify the selected backup:
   ```bash
   python scripts/verify_backup.py /app/data/backups/<timestamp>-full
   ```
3. Copy the backup database into place:
   ```bash
   cp /app/data/backups/<timestamp>-full/funding_bot.sqlite3 /app/data/funding_bot.db
   ```
4. Run a post-restore integrity check:
   ```bash
   python scripts/verify_backup.py /app/data/backups/<timestamp>-full
   ```
5. Start the application.
6. Validate `/health`, `/health/queue`, and a representative dashboard/API workflow.
7. Announce recovery completion and record the incident timeline.

### Restore from a WAL snapshot

1. Scale writers down to zero or stop the service.
2. Verify the selected WAL snapshot:
   ```bash
   python scripts/verify_backup.py /app/data/backups/<timestamp>-wal
   ```
3. Copy the snapshot files into place (always copy `funding_bot.db`, then copy `funding_bot.db-wal` and `funding_bot.db-shm` when they are present in the snapshot):
   ```bash
   cp /app/data/backups/<timestamp>-wal/funding_bot.db /app/data/funding_bot.db
   cp /app/data/backups/<timestamp>-wal/funding_bot.db-wal /app/data/funding_bot.db-wal
   cp /app/data/backups/<timestamp>-wal/funding_bot.db-shm /app/data/funding_bot.db-shm
   ```
4. Open the application or a maintenance shell and run `PRAGMA integrity_check`.
5. Start the service and confirm the expected records are present.
6. Immediately create a new full backup after recovery stabilizes.

### Recovery validation checklist

- `/health` returns `200` and `status=ok`
- the dashboard loads
- recent opportunities, donors, and tasks are present
- scheduled jobs resume only after a successful manual smoke test
- a fresh verified full backup is created after the incident

## Disaster recovery plan

1. **Detect and declare:** classify the incident (disk loss, corruption, cluster loss, accidental deletion).
2. **Contain:** stop writers, revoke broken automation, and preserve evidence.
3. **Recover infrastructure:** restore PVC/volume access, redeploy manifests, and rehydrate secrets/config.
4. **Restore data:** recover the latest verified full backup plus the newest acceptable WAL snapshot.
5. **Validate service:** run health checks, smoke tests, and backup verification.
6. **Resume operations:** re-enable scheduled jobs gradually.
7. **Review:** document timeline, actual RTO/RPO, root cause, and corrective actions.

Minimum DR readiness requirements:

- one verified full backup stored off-cluster/off-host
- backup manifests and checksums retained with the data
- Kubernetes manifests and secrets recovery process documented
- quarterly restore drill proving the 60-minute RTO target

## Backup testing process

Run this process at least **monthly** and after any storage or scheduling change:

1. Pick the latest verified full backup.
2. Restore it into a non-production environment.
3. Run `python scripts/verify_backup.py` against the restored artifact.
4. Start the web app against the restored database.
5. Execute smoke checks for health, dashboard access, and one read/write workflow.
6. Record restore duration, issues found, and corrective actions.
7. Promote the drill result to the operations log.

## Operational notes

- Prefer WAL mode in production if the 15-minute RPO matters.
- Replicate `/app/data/backups` to object storage or a second volume; local-only backups are not sufficient for disaster recovery.
- Review retention settings whenever compliance or donor-data requirements change.
