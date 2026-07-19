# Frequently Asked Questions

This FAQ covers setup, connectors, deduplication, exports, API usage, and common operations. For step-by-step recovery advice, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Setup and local development

### 1. What do I need to install before running Funding Bot?
Install the Python/web dependencies first:

```bash
pip install -r web/requirements.txt
```

If you use the accessibility test runner, also run `npm install`.

### 2. How do I run the test suite?
Use the built-in unittest commands from the README:

```bash
python -m unittest discover -s tests
python -m unittest discover -s tests -p 'test_integration.py'
```

### 3. How do I start the CLI?
Run commands through the module entry point, for example:

```bash
python -m funding_bot list-opportunities
python -m funding_bot --verbose discover --keywords education
```

### 4. How do I start the web dashboard?
Run:

```bash
python -m flask --app web.app run
```

### 5. Which environment variables matter most for first-time setup?
At minimum, set dashboard passwords and a stable app secret for web usage:

- `ADMIN_PASSWORD`
- `STAFF_PASSWORD`
- `AUDITOR_PASSWORD`
- `FLASK_SECRET_KEY`

Queue deployments also need `ENABLE_TASK_QUEUE`, `CELERY_BROKER_URL`, and `CELERY_RESULT_BACKEND`.

### 6. How do I point the bot at a different SQLite database?
Use `--db <path>` for CLI commands or set `BOT_DB_PATH` for the web app and workers.

### 7. How can I inspect the currently saved organization profile and connector settings?
Run:

```bash
python -m funding_bot show-settings
```

## Connectors and discovery

### 8. How do I validate a connector without running full discovery?
Use the connector test command:

```bash
python -m funding_bot test-connector --connector grants-portal --keywords learning --limit 2
```

### 9. Why does `test-connector` return demo data instead of live results?
Built-in connectors use demo data unless a remote HTTP client/configuration is active. The output includes a `mode` field so you can confirm whether the connector ran in `demo` or `remote` mode.

### 10. How do keyword mappings work?
Connectors expand a requested keyword into canonical keywords, synonyms, and category names before matching. See [CONNECTORS.md](CONNECTORS.md) for the built-in mapping tables.

### 11. Why are HTTP connector URLs rejected?
Funding Bot requires `https://` for connector and OAuth token URLs. Insecure `http://` endpoints are rejected before any request is made.

### 12. What does connector fallback mode do?
`PORTAL_FALLBACK_MODE` controls what happens when a remote connector fails:

- `cache-first`
- `cache-only`
- `default-only`
- `disabled`

See [CONNECTORS.md](CONNECTORS.md) for details.

### 13. How do I debug connector auth or OAuth2 issues?
Check the `error` field from `test-connector`, confirm the stored secret has `token_url`, `client_id`, and `client_secret`, and verify the upstream token endpoint returns JSON with `access_token`.

### 14. Why am I seeing connector rate-limit warnings?
Remote connectors use per-connector token-bucket rate limiting. Reduce polling frequency, wait for the retry window, or tune the rate-limit environment variables documented in the README.

## Deduplication and opportunity handling

### 15. How does Funding Bot avoid duplicate opportunities?
Each normalized opportunity gets a stable signature. Discovery inserts only new signatures, so repeated runs do not create duplicate rows for unchanged records.

### 16. Why didn’t discovery create a new row for an opportunity I expected?
The opportunity may already exist under the same signature, or the connector fallback returned cached/default data that did not include a new record.

### 17. Why does application submission fail with `An application already exists for opportunity ...`?
The bot already has an application row for that opportunity signature. Review the existing record instead of resubmitting it.

### 18. How do I look up an opportunity by signature?
Use the API:

```bash
curl -u staff:$STAFF_PASSWORD http://localhost:5000/opportunities/<signature>
```

Or list current opportunities from the CLI first.

## Task management, imports, and exports

### 19. Which task statuses are supported?
The workflow uses `todo`, `in-progress`, `blocked`, and `done`. Some older inputs like `pending` and `in_progress` are normalized for compatibility.

### 20. Why can’t I move a task directly from `todo` to `done`?
Task transitions follow a state machine. Move it through `in-progress` first. See [COLLABORATION.md](COLLABORATION.md).

### 21. How do I export tasks for another system?
Use the export API:

```bash
curl -u auditor:$AUDITOR_PASSWORD \
  "http://localhost:5000/api/tasks/export?sort=due_date&source=external_sync"
```

### 22. How do I sync tasks from another tool?
Send a JSON payload with a top-level `tasks` array to `/api/tasks/sync`.

### 23. How do I bulk import tasks from CSV?
Send raw `text/csv` or upload a multipart file to `/api/tasks/import`. The import is transactional: if one row fails validation, the whole import is rolled back.

### 24. What date format should I use for task due dates?
Use ISO-8601, such as `2026-07-31` or `2026-07-31T00:00:00+00:00`.

### 25. Why does a staff user get `403 Forbidden` on `/tasks`?
Staff users are limited to their own assignment lane. Use an admin or auditor account for broader views.

## Dashboard, API, and auth

### 26. How do I authenticate API requests?
Use HTTP Basic Auth with one of the built-in role names as the username:

- `admin`
- `staff`
- `auditor`

Example:

```bash
curl -u admin:$ADMIN_PASSWORD http://localhost:5000/tasks
```

### 27. Which routes are public?
`/`, `/health`, and `/health/queue` are public. Most dashboard, task, donor, metrics, and settings routes require authentication.

### 28. Why do I get `Invalid authentication credentials`?
The username is not one of the supported roles, the password is wrong, or the matching environment variable is unset.

### 29. How do I monitor application and queue health?
Check:

```bash
curl http://localhost:5000/health
curl http://localhost:5000/health/queue
```

Use `/metrics` for Prometheus-style monitoring.

### 30. What should I look at in `/metrics` first?
Start with:

- `funding_bot_queue_health_status`
- `funding_bot_queue_depth`
- `funding_bot_queue_workers`
- `funding_bot_queue_duplicate_preventions_total`

### 31. How do I prove discovery and outreach from the dashboard?
Open `/settings` as `admin`, then use **Run discovery now** and **Send test outreach**. The dry-run outreach option composes and logs the message without sending it.

## Exports, compliance, and auditability

### 32. How do GDPR exports work?
`gdpr_export(donor_email)` collects the donor profile, consent records, communications, outreach events, and related audit log entries for that donor.

### 33. How do GDPR deletions work?
`gdpr_delete(donor_email)` anonymizes donor and communication data while keeping a deletion audit trail.

### 34. How do I gather evidence for troubleshooting or audits?
Review:

```bash
python -m funding_bot audit-log --limit 20
curl -u auditor:$AUDITOR_PASSWORD http://localhost:5000/audit-log
curl -u auditor:$AUDITOR_PASSWORD http://localhost:5000/metrics
```

### 35. Where should I look for deployment-specific guidance?
Use:

- [Deployment and Scaling Guide](DEPLOYMENT.md)
- [Connector Guide](CONNECTORS.md)
- [Collaboration Guide](COLLABORATION.md)
- [Compliance Procedures](COMPLIANCE.md)
- [Troubleshooting Guide](TROUBLESHOOTING.md)
