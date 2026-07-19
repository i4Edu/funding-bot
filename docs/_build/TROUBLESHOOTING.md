# Troubleshooting Guide

Use this guide for the most common Funding Bot setup and runtime problems. For connector implementation details, see [CONNECTORS.md](CONNECTORS.md). For queue rollout guidance, see [DEPLOYMENT.md](DEPLOYMENT.md). For task workflow rules, see [COLLABORATION.md](COLLABORATION.md).

## Quick diagnostics

Run these commands before changing configuration:

```bash
# Verify the Python/web dependencies used by the bot
pip install -r web/requirements.txt

# Run the Python test suite
python -m unittest discover -s tests

# Inspect saved profile, keywords, and credential aliases
python -m funding_bot show-settings

# Validate a connector in isolation
python -m funding_bot test-connector --connector grants-portal --keywords learning --limit 2

# Run discovery with extra logs
python -m funding_bot --verbose discover --keywords education

# Review recent audit activity
python -m funding_bot audit-log --limit 20

# Check web and queue health
curl http://localhost:5000/health
curl http://localhost:5000/health/queue

# Inspect queue metrics (admin or auditor role required)
curl -u auditor:$AUDITOR_PASSWORD http://localhost:5000/metrics | grep funding_bot_queue
```

If you are running Docker Compose, also check service logs:

```bash
docker compose logs web worker redis
```

## What to check first

1. Confirm the database path with `BOT_DB_PATH` or `--db`.
2. Confirm dashboard credentials: `ADMIN_PASSWORD`, `STAFF_PASSWORD`, `AUDITOR_PASSWORD`.
3. If queue mode is enabled, verify `ENABLE_TASK_QUEUE=1`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, and `CELERY_QUEUE_NAME`.
4. Re-run the failing command with `--verbose` when available.
5. Review `/health`, `/health/queue`, `/metrics`, and `audit-log` before restarting services.

## Installation and startup issues

| Symptom or message | Likely cause | Resolution |
| --- | --- | --- |
| `ModuleNotFoundError: No module named 'flask'` | Web dependencies were not installed. | Run `pip install -r web/requirements.txt`, then retry `python -m flask --app web.app run`. |
| `ModuleNotFoundError: No module named 'celery'` | Queue dependencies are missing. | Reinstall dependencies from `web/requirements.txt`; queue mode depends on Celery. |
| `python -m unittest discover -s tests` fails immediately with import errors | The environment is missing required Python packages. | Install requirements first, then re-run the test command. |
| The dashboard starts but sessions keep resetting | `FLASK_SECRET_KEY` is unset or inconsistent across restarts. | Set a stable `FLASK_SECRET_KEY` in the deployment environment. |
| Login succeeds locally but cookies are not kept in HTTP development mode | `SESSION_COOKIE_SECURE=1` blocks cookies over plain HTTP. | For local HTTP-only development, set `SESSION_COOKIE_SECURE=0`. Keep it enabled in production. |

### Debug steps

```bash
python --version
python -m flask --app web.app run
python -m unittest discover -s tests
```

## Connector setup and discovery issues

| Symptom or message | Likely cause | Resolution |
| --- | --- | --- |
| `status: "degraded"` from `test-connector` | The remote connector failed and fallback mode returned cached/default data. | Review the `metadata.last_error` field, verify endpoint reachability, and confirm `PORTAL_FALLBACK_MODE` is set as intended. |
| `status: "error"` from `test-connector` | Connector validation raised an exception before fallback could complete. | Re-run with the same connector and inspect the `error` field in the JSON response. |
| `OAuth2 client-credentials configuration requires token_url, client_id, and client_secret.` | OAuth2 connector credentials are incomplete. | Update the stored secret or environment-backed secret to include all required values. |
| `OAuth2 token endpoint for secret '...' returned invalid JSON.` | The token endpoint returned HTML, text, or malformed JSON. | Verify the token URL, credentials, and upstream auth server response. |
| `OAuth2 token endpoint for secret '...' did not return access_token.` | The auth server responded without a usable token. | Confirm scopes, audience, and client permissions for that connector. |
| `... must use an https:// URL` | Connector or OAuth token URLs are configured with `http://`. | Switch connector and token endpoints to `https://`. Funding Bot rejects insecure connector transport. |
| Repeated log lines like `connector offline` or `request short-circuited because the circuit breaker is open` | The connector is failing repeatedly and the circuit breaker opened. | Check upstream availability, lower error rates, then retry discovery after the cooldown window. |
| `GlobalGiving rate limit exceeded; retry in ... seconds.` | The connector exceeded its token-bucket quota. | Retry later, reduce polling frequency, or tune the per-connector rate-limit settings in the environment. |

### Connector diagnostics

```bash
python -m funding_bot test-connector --connector grants-portal --keywords learning --limit 2
python -m funding_bot --verbose discover --keywords "education,csr"
curl http://localhost:5000/health
```

Check:

- `requested_keywords` vs `expanded_keywords` in `test-connector` output
- `metadata.source_status`, `metadata.last_error`, and `metadata.retry_after_seconds`
- `PORTAL_FALLBACK_MODE` (`cache-first`, `cache-only`, `default-only`, `disabled`)

## Authentication and authorization issues

| Symptom or message | Likely cause | Resolution |
| --- | --- | --- |
| `Authentication required` | The request did not include valid Basic Auth or an active dashboard session. | Send `curl -u <role>:<password> ...` or sign in again through the dashboard. |
| `Invalid authentication credentials` | Wrong username/password pair or missing environment variable for that role. | Use `admin`, `staff`, or `auditor` as the username and verify the matching password environment variable. |
| `Forbidden` | The authenticated role lacks access to the route or record. | Retry with the correct role; for example, `staff` users cannot manage admin-only routes or view another assignee's tasks. |
| Session expires sooner than expected | Session timeout is too low. | Increase `DASHBOARD_SESSION_TIMEOUT_MINUTES` and redeploy. |

### Auth diagnostics

```bash
curl -u admin:$ADMIN_PASSWORD http://localhost:5000/dashboard
curl -u staff:$STAFF_PASSWORD "http://localhost:5000/tasks?assignee=staff"
curl -u auditor:$AUDITOR_PASSWORD http://localhost:5000/audit-log
```

## Queue and worker issues

| Symptom or message | Likely cause | Resolution |
| --- | --- | --- |
| `/health/queue` returns `status: "disabled"` | Queue mode is not enabled. | Set `ENABLE_TASK_QUEUE=1` if you expect Celery-backed execution. |
| `Queue monitoring is disabled because ENABLE_TASK_QUEUE is not enabled.` | Queue health was requested while cron-only mode is active. | This is expected in legacy mode; enable the queue only when you are ready to run workers. |
| `CELERY_BROKER_URL is not configured.` | Queue mode is enabled but no broker URL is set. | Set `CELERY_BROKER_URL` and `CELERY_RESULT_BACKEND`, then restart web and worker processes. |
| `Timed out while contacting the Celery broker: ...` | The broker is down, unreachable, or too slow for the configured timeout. | Verify Redis/RabbitMQ reachability and increase `CELERY_HEALTH_TIMEOUT_SECONDS` if the broker is healthy but slow. |
| `Unable to query Celery queue health: ...` | Broker introspection or worker inspection failed. | Check worker logs, broker credentials, and network connectivity between the web app and the broker. |
| Queue depth keeps growing | Workers are down, underprovisioned, or listening on the wrong queue. | Confirm worker count in `/health/queue`, verify `CELERY_QUEUE_NAME`, and scale workers as needed. |

### Queue diagnostics

```bash
curl http://localhost:5000/health/queue
curl -u auditor:$AUDITOR_PASSWORD http://localhost:5000/metrics | grep funding_bot_queue
celery -A celery_app:celery_app inspect ping
celery -A celery_app:celery_app flower --port=5555
```

Check:

- `worker_count`
- `queue_depth`
- `pending_tasks`
- `active_tasks`
- queue mode: `cron`, `hybrid`, or `queue`

## Opportunities, deduplication, and application issues

| Symptom or message | Likely cause | Resolution |
| --- | --- | --- |
| `An application already exists for opportunity '...'` | The bot already recorded an application for that opportunity signature. | Open `/opportunities/<signature>` or list opportunities to review the existing application instead of submitting a duplicate. |
| `Unknown opportunity '...'` | The signature is wrong, stale, or points to another database. | Re-run discovery, confirm the correct `--db`/`BOT_DB_PATH`, and fetch the signature again from `list-opportunities` or `/opportunities`. |
| Discovery finds fewer rows than expected | Existing rows were deduplicated by stable signature. | Compare discovered signatures with existing records; unchanged signatures are skipped by design. |

### Diagnostic commands

```bash
python -m funding_bot list-opportunities --limit 20
curl -u staff:$STAFF_PASSWORD http://localhost:5000/opportunities
```

## Task, import, and export issues

| Symptom or message | Likely cause | Resolution |
| --- | --- | --- |
| `Task due_date must be an ISO-8601 date or datetime string.` | The task API or CSV import used an unsupported date format. | Use values like `2026-07-31` or `2026-07-31T00:00:00+00:00`. |
| `Task status cannot transition from 'todo' to 'done'.` | The requested state change violates the workflow rules. | Move tasks through allowed transitions such as `todo -> in-progress -> done`. |
| `Field 'tasks' must be a list of task objects.` | `/api/tasks/sync` received the wrong JSON shape. | Send a JSON object containing a top-level `tasks` array. |
| `CSV import body is empty.` | `/api/tasks/import` received no CSV data. | Send raw `text/csv` or a multipart upload named `file`. |
| `Unsupported CSV columns: ...` | The import file contains headers the API does not allow. | Restrict the file to the documented columns in the README import section. |
| `CSV import did not contain any task rows.` | The CSV file had only a header row or blank lines. | Add at least one valid task row and retry. |
| `Forbidden` when a staff user reads `/tasks` | Staff users are limited to their own lane. | Remove another user's `assignee` filter or use an admin/auditor account. |

### Diagnostic commands

```bash
curl -u admin:$ADMIN_PASSWORD "http://localhost:5000/api/tasks/export?sort=due_date"
curl -u admin:$ADMIN_PASSWORD -X POST http://localhost:5000/api/tasks/sync -H "Content-Type: application/json" -d '{"tasks":[]}'
python -m funding_bot audit-log --action task_status_changed --limit 20
```

## Logs and evidence to review

- CLI output from `python -m funding_bot --verbose ...`
- `python -m funding_bot audit-log --limit 20`
- `/audit-log` for dashboard-visible history
- `/health` and `/health/queue` for runtime status
- `/metrics` for queue depth, worker count, and duplicate queue prevention
- `docker compose logs web worker redis` when running the Compose stack
- Flower at `http://localhost:5555` if queue monitoring is enabled

## Related docs

- [Connector Guide](CONNECTORS.md)
- [Deployment and Scaling Guide](DEPLOYMENT.md)
- [Collaboration Guide](COLLABORATION.md)
- [Compliance Procedures](COMPLIANCE.md)
- [FAQ](FAQ.md)
