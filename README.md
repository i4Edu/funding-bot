# Nonprofit Funding Bot

[![Build Status](https://img.shields.io/badge/build-placeholder-lightgrey)](#)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](#installation)
[![License](https://img.shields.io/badge/license-TBD-lightgrey)](#license)

The Nonprofit Funding Bot helps staff discover funding opportunities, prevent duplicate applications, track submissions, manage donor outreach, generate application documents, and prepare daily operational summaries. It combines a Python core, a Flask web dashboard, and deployment paths for Docker today and Kubernetes as the scaling roadmap matures.

For planned milestones and release scope, see [roadmap.md](roadmap.md).
For connector implementation and keyword-mapping guidance, see [docs/CONNECTORS.md](docs/CONNECTORS.md).
For vulnerability reporting, disclosure timelines, incident response, and the penetration-testing checklist, see [docs/SECURITY.md](docs/SECURITY.md).

## Overview

The project is designed for nonprofit operations teams that need a lightweight workflow for:

- discovering grants, CSR opportunities, and NGO funding programs from trusted sources
- storing organizational profile data and credential references
- tracking applications in SQLite with duplicate protection
- logging outreach with opt-out safeguards and throttling
- generating PDF and DOCX-ready documents from templates
- emailing a daily summary report to staff
- expanding into dashboards, compliance tooling, and production deployment over time

## Architecture

| Component | Purpose |
| --- | --- |
| `funding_bot.py` | Core service and CLI entry point. Manages discovery, donor records, audit logs, document generation, outreach, status polling, and daily summaries. |
| `web/app.py` | Flask dashboard and JSON API for staff, admins, and auditors. Uses Basic Auth backed by role-specific environment variables. |
| SQLite database | Default operational store for opportunities, applications, donors, communications, documents, and audit logs. |
| `Dockerfile` / `docker-compose.yml` | Container packaging for the CLI and web dashboard, suitable for local and small-team deployments. |
| `k8s/` (roadmap target) | Planned Kubernetes manifests for horizontal scaling, CronJobs, secrets, and production orchestration in v0.5.0+. |

## Features by version

| Version | Status | Scope |
| --- | --- | --- |
| `v0.1.0` | ✅ Done | MVP: opportunity discovery, deduplication, SQLite tracking, document generation, outreach logging, daily summaries, and CLI-based scheduling. |
| `v0.2.0` | ✅ Done | Portal connectors, donor segmentation, GDPR-oriented compliance workflows, and engagement metrics. |
| `v0.3.0` | ✅ Done | Admin CLI extensions, credential vault integration, AI proposal drafting, and richer outreach analytics. |
| `v0.4.0` | ✅ Done | Web dashboard, role-based access, collaboration workflows, and monthly audit reports. |
| `v0.5.0` | ✅ Done | Docker and Kubernetes operations, retry/backoff resilience, and multi-language outreach templates. |
| `v1.0.0` | ✅ Done | Mature donor CRM behavior, full portal ecosystem, advanced compliance, and production release readiness. |

### Version details

#### v0.1.0 — MVP
- opportunity discovery from trusted sources
- duplicate prevention via stable signatures
- SQLite-backed application tracking
- PDF and DOCX document generation
- outreach logging with weekly throttling and opt-out protection
- daily summary email generation and SMTP delivery support

#### v0.2.0 — Multi-portal + engagement
- government, CSR, and NGO portal connectors
- donor segmentation (`corporate`, `institutional`, `individual`)
- donor locale preferences for outreach templates (`en`, `bn`)
- GDPR-oriented auditability and encrypted credential handling
- personalized outreach templates with engagement metrics

#### v0.3.0 — Automation + intelligence
- admin CLI extensions: `list-opportunities`, `audit-log`, `list-donors`
- credential vault support for managed secrets
- AI-assisted proposal drafting from stored nonprofit profile data
- outreach analytics for opens, clicks, and donor response tracking

#### v0.4.0 — Dashboard + collaboration
- Flask web dashboard for operations visibility
- role-based access for admin, staff, and auditor personas
- monthly audit report generation
- collaboration workflows for shared review, follow-up, and personal task tracking
- self-service `/settings` panel for the organization profile, search keywords, and
  credential aliases, plus one-click actions to prove donation search and donor
  outreach without leaving the browser

#### v0.5.0 — Scaling + resilience
- Docker Compose deployment for local/shared hosting
- Kubernetes rollout for multi-instance operations
- retry/backoff handling for browser and portal failures
- multi-language outreach templates, including English and Bengali

#### v1.0.0 — Production release
- mature CRM-like donor and application history
- hardened compliance and accessibility processes
- automated daily, weekly, and monthly reporting
- onboarding-friendly staff documentation and production operations

## Installation

The core bot uses the Python standard library plus Babel for locale-aware document formatting, and the web/task-queue stack uses Flask, Celery, and the Redis client:

```bash
pip install -r web/requirements.txt
```

## Document localization

`FundingBot.generate_document(..., locale=...)` supports these document locales:

| Locale | Purpose | Date format | Number format |
| --- | --- | --- | --- |
| `en` | English documents | `MM/DD/YYYY` | Western grouping, e.g. `1,250,000.5` |
| `bn` | Bengali documents | `DD/MM/YYYY` | Bengali locale grouping, e.g. `12,50,000` |

Formatting uses Babel. Template placeholders automatically localize `date`, `datetime`, `int`, `float`, and `Decimal` values from the merged profile/context.

For translated copy inside templates, provide a `translations` mapping and reference it with `{t[key]}`:

```python
documents = bot.generate_document(
    kind="cover_letter",
    template="{t[greeting]}\nDate: {report_date}\nBudget: {budget}",
    output_dir="generated-docs",
    locale="bn",
    context={
        "report_date": datetime(2026, 7, 19, 9, 30, tzinfo=timezone.utc),
        "budget": 1250000,
        "translations": {
            "en": {"greeting": "Dear Review Committee"},
            "bn": {},
        },
    },
)
```

If a translation is missing for the requested locale, document generation falls back to the English (`en`) value.

## Quick Start

### Run tests

```bash
python -m unittest discover -s tests
```

```bash
npm install
npm run test:a11y
```

### Run the CLI

```bash
# Print the daily summary without sending it
python -m funding_bot send-daily-summary --dry-run

# Send the daily summary via SMTP
python -m funding_bot send-daily-summary --recipient lupael@i4e.com.bd

# List discovered opportunities (optionally filter by status)
python -m funding_bot list-opportunities
python -m funding_bot list-opportunities --status pending --limit 20

# Validate a single connector and print sample results
python -m funding_bot test-connector --connector grants-portal --keywords learning

# View recent audit log entries
python -m funding_bot audit-log
python -m funding_bot audit-log --action application_recorded --limit 50

# List donors (optionally filter by segment)
python -m funding_bot list-donors
python -m funding_bot list-donors --segment corporate
```

### Run the web dashboard

```bash
python -m flask --app web.app run
```

## CLI Reference

Global option:

| Option | Description |
| --- | --- |
| `--db PATH` | Path to the SQLite database file. Default: `funding_bot.db`. |

Command reference:

| Command | Version | Key options | Purpose | Status |
| --- | --- | --- | --- | --- |
| `send-daily-summary` | `v0.1.0` | `--recipient EMAIL`, `--dry-run` | Build the daily funding report and either print it or send it through SMTP. | Available |
| `list-opportunities` | `v0.3.0` | `--status STATUS` | List discovered opportunities, optionally filtered by status. | Available |
| `audit-log` | `v0.3.0` | `--limit N`, `--action ACTION` | Review recent audit events for compliance and operational troubleshooting. | Available |
| `list-donors` | `v0.3.0` | `--segment {corporate,institutional,individual,unknown}` | List donor records and segment membership. | Available |
| `monthly-audit-report` | `v1.0.0` | `--year YEAR`, `--month MONTH`, `--output FILE` | Generate a monthly GDPR/ISO compliance audit report as JSON. | Available |
| `discover` | `v0.3.0` | `--keywords KEYWORDS`, `--trusted-sources SOURCES` | Query every configured portal connector and persist new opportunities (proves donation search). | Available |
| `test-connector` | `v1.0.0` | `--connector NAME`, `--keywords KEYWORDS`, `--limit N` | Validate one connector in isolation and print sample results plus connector-specific keyword mappings. | Available |
| `send-outreach` | `v0.3.0` | `--email EMAIL`, `--name NAME`, `--subject TEMPLATE`, `--body TEMPLATE`, `--dry-run` | Compose and send (or preview) a personalized donor outreach email (proves donor communication). | Available |
| `set-organization-profile` | `v0.4.0` | `--file FILE` | Store the nonprofit's organization profile from a JSON file (or stdin). | Available |
| `register-credential` | `v0.4.0` | `--alias ALIAS`, `--env-var ENV_VAR` | Register a credential alias that resolves to an environment variable. | Available |
| `show-settings` | `v0.4.0` | *(none)* | Print the organization profile, search settings, and credential aliases. | Available |

## SMTP Configuration

Set the following environment variables before running the `send-daily-summary`
command (or before calling `SMTPEmailSender.from_env()` programmatically):

| Variable        | Default       | Description                                |
|-----------------|---------------|--------------------------------------------|
| `SMTP_HOST`     | `localhost`   | Mail server hostname                       |
| `SMTP_PORT`     | `587`         | Mail server port                           |
| `SMTP_USERNAME` | *(empty)*     | Login username                             |
| `SMTP_PASSWORD` | *(empty)*     | Login password                             |
| `SMTP_USE_TLS`  | `1`           | Set to `0` to disable STARTTLS             |
| `SMTP_FROM`     | username      | Envelope `From` address                    |

## Web Dashboard

The dashboard is intended for v0.4.0+ operations and is already scaffolded in `web/app.py`.

### Run locally

```bash
pip install -r web/requirements.txt
python -m flask --app web.app run
```

### Accessibility checks

The dashboard templates now share a keyboard-visible skip link through
`web/templates/base.html`. To run automated accessibility checks locally against the
template fixture app:

```bash
pip install -r web/requirements.txt
npm install
npx playwright install chromium
python -m flask --app tests.accessibility.app run --host 127.0.0.1 --port 5001
```

Then, in a second terminal:

```bash
npm run test:a11y
```

The accessibility runner uses `@axe-core/playwright` to scan `/dashboard`,
`/dashboard/tasks`, and `/settings` rendered by the fixture app, and exits non-zero
when any axe violation is found. The same command runs in GitHub Actions CI.

### Dashboard screenshot

![Funding Bot dashboard](docs/images/dashboard-screenshot.png)

### Role-based authentication

The dashboard uses HTTP Basic Auth. Use one of these usernames as the role name:

| Username | Environment variable | Access |
| --- | --- | --- |
| `admin` | `ADMIN_PASSWORD` | Full control, including submissions and donor updates |
| `staff` | `STAFF_PASSWORD` | Operational read access to dashboard and opportunity views |
| `auditor` | `AUDITOR_PASSWORD` | Read access to dashboard, donors, analytics, and audit logs |

### Available routes

| Route | Method | Roles | Purpose |
| --- | --- | --- | --- |
| `/` | `GET` | Public | Redirect to `/dashboard`. |
| `/dashboard` | `GET` | `staff`, `admin`, `auditor` | HTML operations dashboard (WCAG 2.1 accessible). |
| `/dashboard/tasks` | `GET` | `staff`, `admin`, `auditor` | HTML task dashboard with assignee, status, due-date filters and assignee/status/due-date sorting. |
| `/tasks` | `GET` | `staff`, `admin`, `auditor` | List tasks as JSON with assignee, status, due-date filtering and assignee/status/due-date sorting. |
| `/opportunities` | `GET` | `staff`, `admin`, `auditor` | List opportunities as JSON. |
| `/opportunities/<signature>` | `GET` | `staff`, `admin`, `auditor` | Show one opportunity, linked application, and submission attempts. |
| `/opportunities/<signature>/submit` | `POST` | `admin` | Record a submission result for an opportunity. |
| `/donors` | `GET` / `POST` | `admin`, `auditor` for `GET`; `admin` for `POST` | List or upsert donor records, including preferred outreach locale. |
| `/donors/<email>/opt-out` | `POST` | `admin` | Mark a donor as opted out. |
| `/analytics` | `GET` | `admin`, `auditor` | Return outreach analytics data. |
| `/audit-log` | `GET` | `admin`, `auditor` | Return the latest audit log entries. |
| `/settings` | `GET` | `staff`, `admin`, `auditor` | Self-service settings panel: organization profile, search keywords, credential aliases, and proof-of-capability actions. |
| `/settings/organization` | `POST` | `admin` | Update the organization profile. |
| `/settings/search` | `POST` | `admin` | Update donation-search keyword filters and trusted sources. |
| `/settings/credentials` | `POST` | `admin` | Register a credential alias (never exposes secret values). |
| `/settings/discover` | `POST` | `admin` | Run discovery immediately in cron mode, or enqueue it as a Celery task when `ENABLE_TASK_QUEUE=1`. |
| `/settings/test-outreach` | `POST` | `admin` | Compose (dry-run) or send a donor outreach email — proves the bot can communicate with donors. |
| `/tasks/<id>/status` | `POST` | `staff`, `admin`, `auditor` | Transition a task through `todo`, `in-progress`, `blocked`, and `done` with state-machine validation. |
| `/feedback` | `POST` | `staff`, `admin` | Submit partner feature-request or bug-report feedback. |
| `/metrics` | `GET` | `admin`, `auditor` | Prometheus-compatible text metrics for Grafana scraping, including task totals and status counts. |
| `/health` | `GET` | Public | Health-check endpoint with embedded queue mode and queue-health snapshot. |
| `/health/queue` | `GET` | Public | Queue health snapshot including queue depth, worker status, and cron/queue migration mode. |

### Keyboard navigation and screen reader checks

The dashboard pages keep a predictable tab order based on the visible layout and add shortcut help directly in the UI.

- `Tab` / `Shift+Tab` move through links, forms, and action buttons in page order.
- `Enter` and `Space` activate dashboard action buttons, including the settings proof actions.
- Global shortcuts: `Alt+Shift+D` (dashboard), `Alt+Shift+S` (settings), `Alt+Shift+M` (main content), `Alt+Shift+K` (keyboard shortcut help).
- Dashboard shortcuts: `Alt+Shift+O` focuses recent opportunities and `Alt+Shift+A` focuses recent applications.
- Settings shortcuts: `Alt+Shift+O` focuses organization profile, `Alt+Shift+F` focuses donation search settings, `Alt+Shift+C` focuses credential aliases, `Alt+Shift+R` runs discovery, and `Alt+Shift+T` focuses donor outreach.
- Screen reader QA should confirm landmarks, headings, live-region status messages, and the keyboard shortcut help card on both `/dashboard` and `/settings`.

Automated accessibility coverage lives in `tests/test_web_app.py` and checks ARIA labels, live regions, keyboard bindings, and shortcut documentation.

### Prometheus metrics

The `/metrics` endpoint exposes the following gauges and counters in the Prometheus text exposition format:

| Metric | Type | Description |
| --- | --- | --- |
| `funding_bot_opportunities_total` | counter | Total opportunities discovered |
| `funding_bot_applications_total` | counter | Total grant applications recorded |
| `funding_bot_pending_applications` | gauge | Applications awaiting a decision |
| `funding_bot_donors_total` | gauge | Total donor records |
| `funding_bot_opted_out_donors` | gauge | Donors who have opted out |
| `funding_bot_audit_log_entries_total` | counter | Total audit log entries |
| `funding_bot_communications_total` | counter | Total outreach emails logged |
| `funding_bot_uptime_seconds` | gauge | Seconds since the web process started |
| `funding_bot_queue_health_status` | gauge | Queue health state (`1` = broker reachable and metrics collected; `0` = disabled or degraded) |
| `funding_bot_queue_broker_up` | gauge | Whether the Celery broker is reachable |
| `funding_bot_queue_active_tasks` | gauge | Active Celery tasks currently executing |
| `funding_bot_queue_pending_tasks` | gauge | Tasks waiting in the monitored queue |
| `funding_bot_queue_depth` | gauge | Broker queue depth for the monitored Celery queue |
| `funding_bot_queue_workers` | gauge | Online Celery workers detected |

Add a scrape target pointing to `http://<host>:5000/metrics` in your Prometheus configuration or Grafana Agent config, and authenticate with an `admin` or `auditor` dashboard role.

### Task filter API

`GET /tasks` and `GET /dashboard/tasks` accept the same query parameters:

| Parameter | Example | Description |
| --- | --- | --- |
| `assignee` | `staff` | Filter to an exact assignee. Staff users are restricted to their own role. |
| `status` | `in-progress` | Filter by task status. Accepted values: `todo`, `in-progress`, `done`, `blocked`. |
| `due_date_before` | `2026-07-31` | Include only tasks due on or before the given UTC date. |
| `due_date_after` | `2026-07-01` | Include only tasks due on or after the given UTC date. |
| `sort` | `due_date` | Sort results by `assignee`, `status`, or `due_date`. Prefix with `-` for descending order (for example `-due_date` or `-assignee`). Default: `updated_at`. |

Example:

```bash
curl -u admin:$ADMIN_PASSWORD \
  "http://localhost:5000/tasks?assignee=staff&status=todo&due_date_after=2026-07-01&due_date_before=2026-07-31&sort=due_date"
```

## Outreach template translations

- Store built-in outreach template catalogs in `i18n/outreach_templates/` as UTF-8 JSON files.
- Keep matching template keys in `en.json` and `bn.json` so every locale can render the same outreach flows.
- Save each donor's preferred `locale` on the donor profile; outreach composition uses that preference to pick the built-in template automatically.
- One-off subject/body overrides still work and keep the locale-aware Bengali or English opt-out notice.

### Queue health monitoring

Set the optional queue-monitoring environment variables when Celery is enabled:

```bash
export ENABLE_TASK_QUEUE=1
export CELERY_BROKER_URL=redis://redis:6379/0
export CELERY_RESULT_BACKEND=redis://redis:6379/1
export CELERY_QUEUE_NAME=funding-bot
export CELERY_HEALTH_TIMEOUT_SECONDS=2.0
```

`GET /health/queue` returns JSON like:

```json
{
  "status": "ok",
  "queue_name": "funding-bot",
  "broker_reachable": true,
  "timeout_seconds": 2.0,
  "active_tasks": 2,
  "pending_tasks": 4,
  "queue_depth": 4,
  "worker_count": 2,
  "workers": [
    {
      "name": "celery@worker-1",
      "status": "online",
      "active_tasks": 1,
      "reserved_tasks": 2,
      "scheduled_tasks": 0
    }
  ]
}
```

Possible `status` values:

- `ok`: broker reachable and worker/task metrics collected
- `disabled`: queue mode is not enabled (`ENABLE_TASK_QUEUE=0`)
- `degraded`: broker unreachable, Celery unavailable, or the health probe timed out

When `status` is `degraded`, the endpoint responds with HTTP `503` and includes an `error` field describing the timeout or broker failure.

### Partner feedback

Staff and admin users can submit feedback for the feature backlog:

```bash
curl -u staff:$STAFF_PASSWORD \
  -X POST http://localhost:5000/feedback \
  -H "Content-Type: application/json" \
  -d '{"category": "feature_request", "message": "Add CSV export for audit logs.", "contact": "partner@ngo.org"}'
```

Allowed categories: `feature_request`, `bug_report`, `general`.
The `message` field must be non-empty and at most 2000 characters.

## Proof: Search and Donor Communication

Two independent ways to demonstrate the bot searching for donation opportunities and
communicating with a donor — from the CLI or from the `/settings` admin panel, without
touching code or environment variables.

### From the CLI

```bash
# Search every configured portal connector and store any new opportunities.
python funding_bot.py discover --keywords "education,csr"

# Compose (and, unless --dry-run, send via SMTP) a personalized donor email.
python funding_bot.py send-outreach --email donor@example.org --name "Jane Donor" --dry-run
```

### From the web Settings panel

1. Sign in to `/settings` as `admin`.
2. Click **Run discovery now** under "Prove: Donation Search" to query every portal
   connector and see newly discovered opportunities rendered as JSON.
3. Fill in a donor email/name under "Prove: Donor Communication" and click
   **Send test outreach**. With "Dry run" checked, the email is composed and logged
   without being delivered; uncheck it (with SMTP credentials configured) to deliver
   a real message.

Both actions are logged to the audit trail (`audit-log` / `/audit-log`) for
compliance review.

For contributor guidance on English/Bengali outreach copy and locale conventions, see [docs/TRANSLATIONS.md](docs/TRANSLATIONS.md).

## Docker Deployment

The repository includes a `Dockerfile` and `docker-compose.yml`.

1. Copy environment settings:

   ```bash
   cp .env.example .env
   ```

2. Update values in `.env` for SMTP credentials, database path, dashboard passwords, and queue flags.
3. Start the stack:

   ```bash
   docker compose up
   ```

The Compose stack runs:
- a CLI container for bot jobs
- a Flask web container on `http://localhost:5000`
- optional `redis`, `worker`, and `flower` services when started with the `queue` profile
- a shared volume for SQLite data at `/app/data`

## Kubernetes Deployment

Kubernetes is the v0.5.0+ deployment target.

```bash
kubectl apply -f k8s/
```

Recommended secret/config inputs:

- SMTP settings: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_USE_TLS`, `SMTP_FROM`
- dashboard auth: `ADMIN_PASSWORD`, `STAFF_PASSWORD`, `AUDITOR_PASSWORD`
- persistence/runtime: `BOT_DB_PATH`, `ENABLE_TASK_QUEUE`, `ENABLE_LEGACY_CRON`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`

Use a `CronJob` for scheduled summary delivery and a `Deployment`/`Service` pair for the dashboard. If the `k8s/` manifests are not yet present in your branch, treat this as the target structure for the scaling release.

## GDPR / Compliance

Compliance is a cross-version concern:

- audit activity is stored in the `audit_logs` table
- donor opt-out state is enforced during outreach
- donor segmentation supports controlled communications
- roadmap compliance helpers should expose `gdpr_export()` and `gdpr_delete()` workflows for subject-access and erasure requests

In practice, `gdpr_export()` should bundle all donor/application data tied to a subject, while `gdpr_delete()` should remove or anonymize personal data while preserving required audit history.

## Scheduling

Celery is the preferred replacement for cron for new asynchronous work in this repository. See [docs/celery-vs-rq.md](docs/celery-vs-rq.md) for the Celery vs RQ evaluation and recommendation.

### Celery configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `CELERY_BROKER_URL` | `redis://redis:6379/0` | Primary broker URL. Use the RabbitMQ example below to switch brokers. |
| `CELERY_RESULT_BACKEND` | `redis://redis:6379/1` | Result backend for task metadata and task return values. |
| `ENABLE_TASK_QUEUE` | `0` in code / `1` in `.env.example` | Enable Celery-backed async task execution. |
| `ENABLE_LEGACY_CRON` | `1` | Keep legacy cron scheduling active during queue migration. |
| `CELERY_QUEUE_NAME` | `funding-bot` | Default queue name for funding bot workers. |
| `CELERY_HEALTH_TIMEOUT_SECONDS` | `2.0` in `.env.example` | Timeout for `/health/queue` broker and worker checks. |
| `CELERY_TASK_ALWAYS_EAGER` | `0` | Execute queued work inline for tests and local debugging. |

RabbitMQ broker example:

```bash
export CELERY_BROKER_URL=amqp://<user>:<password>@rabbitmq:5672//
```

### Running the worker

```bash
celery -A celery_app:celery_app worker --loglevel=info --queues funding-bot
```

### Docker Compose brokers

`docker-compose.yml` now includes:

- `redis` as the default Celery broker and result backend
- `rabbitmq` as an alternate broker option
- `worker` running `celery_app:celery_app`

Start the stack with:

```bash
docker compose up --build
```

### Legacy cron fallback

Cron can remain as a migration fallback while queue-backed workers are introduced:

- `ENABLE_TASK_QUEUE=0`, `ENABLE_LEGACY_CRON=1` → legacy cron only
- `ENABLE_TASK_QUEUE=1`, `ENABLE_LEGACY_CRON=1` → hybrid migration mode
- `ENABLE_TASK_QUEUE=1`, `ENABLE_LEGACY_CRON=0` → queue-first mode

```cron
0 9 * * * cd /path/to/funding-bot && python -m funding_bot send-daily-summary
```

For Kubernetes deployments, mirror either the legacy CLI schedule with a `CronJob` or the new worker model with a Celery-compatible broker deployment.

## Partner Onboarding

Use the included onboarding script to set up a new NGO partner environment in a single step:

```bash
bash scripts/onboard.sh
```

The script:
1. Verifies Python 3.11+ and Docker prerequisites.
2. Copies `.env.example` to `.env` and prompts for SMTP credentials and dashboard passwords (passwords are not echoed).
3. Installs Python dependencies.
4. Runs the test suite.
5. Builds and starts the Docker Compose stack (pass `--skip-docker` to skip).
6. Smoke-tests the `/health` endpoint.

Options:

| Option | Description |
| --- | --- |
| `--env-file PATH` | Path to write the `.env` file (default: `.env`). When Docker is enabled, the script links `.env` to this file so Compose uses the same values. |
| `--db-path PATH` | SQLite database path written into `.env` (default: `/app/data/funding_bot.db`). |
| `--skip-docker` | Set up the Python environment only; do not start Docker. |

## Compliance Reports

Generate a monthly audit report for any period:

```bash
# Print to stdout (JSON)
python -m funding_bot monthly-audit-report

# Save to a file for a specific month
python -m funding_bot monthly-audit-report --year 2025 --month 6 --output reports/2025-06-audit.json
```

The report includes:
- Audit log entries grouped by action type
- GDPR operations (exports, deletions, opt-outs)
- Application outcome counts by status
- Outreach analytics (sent, opened, clicked, bounce rate)
- New donor registrations and total opted-out count

## Roadmap

Release planning lives in [roadmap.md](roadmap.md). Use it alongside this README when onboarding new staff, planning environment changes, or sequencing upcoming feature work.

## License

No project license is published in this repository yet. Update this section and the badge above when a license is chosen.
