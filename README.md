# Nonprofit Funding Bot

[![Build Status](https://img.shields.io/badge/build-placeholder-lightgrey)](#)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](#installation)
[![License](https://img.shields.io/badge/license-TBD-lightgrey)](#license)

The Nonprofit Funding Bot helps staff discover funding opportunities, prevent duplicate applications, track submissions, manage donor outreach, generate application documents, and prepare daily operational summaries. It combines a Python core, a Flask web dashboard, and deployment paths for Docker today and Kubernetes as the scaling roadmap matures.

For planned milestones and release scope, see [roadmap.md](roadmap.md).
For connector implementation and keyword-mapping guidance, see [docs/CONNECTORS.md](docs/CONNECTORS.md).
For collaboration workflow, permissions, and task API examples, see [docs/COLLABORATION.md](docs/COLLABORATION.md).
For deployment and scaling guidance, see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
For contributor setup, pull request expectations, and code review standards, see [CONTRIBUTING.md](CONTRIBUTING.md).
For vulnerability reporting, disclosure timelines, incident response, and the penetration-testing checklist, see [docs/SECURITY.md](docs/SECURITY.md).

## Overview

The project is designed for nonprofit operations teams that need a lightweight workflow for:

- discovering grants, CSR opportunities, NGO funding programs, and crowdfunding campaigns from trusted sources
- storing organizational profile data and credential references
- tracking applications in SQLite with duplicate protection
- logging outreach with opt-out safeguards and throttling
- storing donor communication consent and opt-out history
- generating PDF and DOCX-ready documents from templates
- enforcing self-hosted data residency settings (`US`, `EU`, `ASIA`)
- generating jurisdiction-aware privacy policies in HTML and PDF
- emailing a daily summary report to staff
- coordinating shared task assignment, re-assignment, and status tracking
- generating weekly/monthly GDPR self-check reports for retention and data-subject activity
- expanding into dashboards, compliance tooling, and production deployment over time

## Architecture

### System diagram

```text
                                    External systems
+--------------------+   +---------------------+   +----------------------+
| Grants.gov / grant |   | CSR APIs / partner  |   | NGO directories /    |
| portals            |   | funding networks    |   | crowdfunding feeds   |
+----------+---------+   +----------+----------+   +-----------+----------+
           |                        |                          |
           +------------------------+--------------------------+
                                    |
                                    v
                         +-------------------------+
                         | Connector layer         |
                         | funding_bot.py          |
                         | - discovery connectors  |
                         | - dedupe + normalization|
                         | - outreach/doc helpers  |
                         +-----------+-------------+
                                     |
                    sync CLI/admin   | enqueue async discovery,
                    actions          | outreach, reports
                                     v
                         +-------------------------+
                         | Task queue              |
                         | Celery + Redis/RabbitMQ |
                         +-----------+-------------+
                                     |
                                     v
                         +-------------------------+
                         | Worker processes        |
                         | celery_tasks.py         |
                         +-----------+-------------+
                                     |
          +--------------------------+---------------------------+
          |                          |                           |
          v                          v                           v
+------------------+       +----------------------+   +----------------------+
| Web dashboard    |<----->| SQLite database      |<->| SMTP / email provider|
| Flask + JSON API |       | opportunities, tasks,|   | donor outreach +     |
| /dashboard,      |       | donors, audit logs,  |   | daily summaries      |
| /settings, /tasks|       | docs, queue metadata |   +----------------------+
+---------+--------+       +----------+-----------+
          |                           ^
          | staff/admin/auditor UI    |
          +---------------------------+
```

### Component responsibilities

| Component | Purpose |
| --- | --- |
| `funding_bot.py` | Core service and CLI entry point. Manages discovery, donor records, audit logs, document generation, outreach, status polling, and daily summaries. |
| Connector layer | Pulls and normalizes opportunity data from Grants.gov-style portals, CSR APIs, NGO directories, and crowdfunding sources before deduplication/storage. |
| `task_queue.py`, `celery_app.py`, `celery_tasks.py` | Dispatch, broker configuration, retries, and background execution for discovery, outreach, and report generation. |
| `web/app.py` | Flask dashboard and JSON API for staff, admins, and auditors. Uses Basic Auth backed by role-specific environment variables and secure Flask sessions for dashboard access. |
| SQLite database | Default operational store for opportunities, applications, donors, communications, documents, collaboration tasks, and audit logs. |
| `Dockerfile` / `docker-compose.yml` | Container packaging for the CLI, web dashboard, broker, worker, and optional Flower monitor in local/shared deployments. |
| `k8s/` manifests | Kubernetes deployment option for the web pod, worker pods, services, secrets/config, persistent storage, and scheduled jobs. |

### Data flow

1. Connectors query external grant and partner systems, then normalize results into shared opportunity records.
2. The CLI or web dashboard either executes work inline or enqueues background jobs onto Celery for slower discovery/reporting flows.
3. Workers persist results to SQLite so the dashboard, CLI, and audit/reporting paths all read the same operational state.
4. Outreach and summary jobs call SMTP/email providers after checking consent, throttling, and audit requirements.
5. Staff use the dashboard and task API to review queue state, assign work, and inspect audit history generated by both synchronous and async paths.

### Deployment topology

```text
Docker Compose (local/shared)
+--------+   +-----+   +--------+   +-------------------+
|  web   |   | bot |   | worker |   | redis / rabbitmq  |
+---+----+   +--+--+   +---+----+   +---------+---------+
    \______________ shared image + env __________________/
                     |
                     v
              +--------------+
              | SQLite volume|
              +--------------+

Kubernetes (scaled)
+-------------------+      +----------------------+
| web Deployment    |----->| Service / Ingress    |
+---------+---------+      +----------------------+
          |
          +-----> PersistentVolumeClaim -----> SQLite data path
          |
+---------v---------+      +----------------------+
| worker Deployment |<---->| Redis/RabbitMQ svc   |
+-------------------+      +----------------------+
          |
          +-----> optional CronJob for scheduled summaries/discovery
```

- **Docker / Docker Compose**: run `web`, `bot`, `worker`, broker (`redis` by default, optional `rabbitmq`), and optional `flower` with a shared volume for SQLite data.
- **Kubernetes**: deploy the Flask dashboard as a `Deployment` + `Service`, run Celery workers as a separate `Deployment`, keep scheduled summaries/discovery in a `CronJob` where needed, and mount persistent storage for the SQLite data path.
- **Migration mode**: support legacy cron, hybrid queue mode, or queue-first mode by toggling `ENABLE_TASK_QUEUE` and `ENABLE_LEGACY_CRON`. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for rollout details.

## Features by version

| Version | Status | Scope |
| --- | --- | --- |
| `v0.1.0` | ✅ Done | MVP: opportunity discovery, deduplication, SQLite tracking, document generation, outreach logging, daily summaries, and CLI-based scheduling. |
| `v0.2.0` | ✅ Done | Portal connectors, donor segmentation, GDPR-oriented compliance workflows, and engagement metrics. |
| `v0.3.0` | ✅ Done | Admin CLI extensions, credential vault integration, AI proposal drafting, and richer outreach analytics. |
| `v0.4.0` | ✅ Done | Web dashboard, role-based access, collaboration workflows, and monthly audit reports. |
| `v0.5.0` | ✅ Done | Docker and Kubernetes operations, retry/backoff resilience, multi-language outreach templates, and translation review tooling. |
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
- government, CSR, NGO, and crowdfunding connectors
- donor segmentation (`corporate`, `institutional`, `individual`)
- donor locale preferences for outreach templates (`en`, `bn`)
- GDPR-oriented auditability and encrypted credential handling
- consent records for donor communication history and opt-outs
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
- translation review workflow with pending / approved / rejected status tracking
- RTL-aware locale metadata and preview checks for future Arabic / Urdu support

#### v1.0.0 — Production release
- mature CRM-like donor and application history
- hardened compliance and accessibility processes
- automated daily, weekly, and monthly reporting
- periodic GDPR self-check reports covering consent, retention, exports, and deletions
- onboarding-friendly staff documentation and production operations
- configurable data residency enforcement and privacy policy generation

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
# Prompt for missing required arguments
python -m funding_bot send-outreach --dry-run

# Disable prompts in automation
python -m funding_bot --non-interactive test-connector --connector grants-portal --keywords learning

# Show informational CLI logs
python -m funding_bot --verbose discover --keywords education

# Silence non-error logs
python -m funding_bot --quiet list-opportunities

# Print the daily summary without sending it
python -m funding_bot send-daily-summary --dry-run

# Send the daily summary via SMTP
python -m funding_bot send-daily-summary --recipient lupael@i4e.com.bd

# Generate a weekly GDPR self-check report
python -m funding_bot gdpr-self-check-report --cadence weekly

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
| `--verbose` | Increase CLI logging to `INFO` for operational progress messages. |
| `--quiet` | Reduce CLI logging to `ERROR` so only failures are emitted. |
| `--non-interactive` | Disable `input()` prompts and fail fast when required command options are missing. |

## CLI logging and interactive prompts

The CLI configures Python logging when it starts:

| Setting | Logging level | Behavior |
| --- | --- | --- |
| default | `WARNING` | Show warnings and errors only. |
| `--verbose` | `INFO` | Show additional operational log messages. |
| `--quiet` | `ERROR` | Suppress warnings and only show errors. |

Commands with required options (`send-outreach`, `test-connector`, and
`register-credential`) prompt for missing values by default. Use
`--non-interactive` in scripts or CI so missing required flags fail immediately
instead of waiting for input.

Examples:

```bash
# Interactive prompt for a missing connector slug
python -m funding_bot test-connector --limit 1

# Interactive prompt for outreach recipient details
python -m funding_bot send-outreach --dry-run

# Script-safe execution that fails if a required flag is omitted
python -m funding_bot --non-interactive send-outreach --email donor@example.org --name "Donor" --dry-run
```

Command reference:

| Command | Version | Key options | Purpose | Status |
| --- | --- | --- | --- | --- |
| `send-daily-summary` | `v0.1.0` | `--recipient EMAIL`, `--dry-run` | Build the daily funding report and either print it or send it through SMTP. | Available |
| `list-opportunities` | `v0.3.0` | `--status STATUS` | List discovered opportunities, optionally filtered by status. | Available |
| `audit-log` | `v0.3.0` | `--limit N`, `--action ACTION` | Review recent audit events for compliance and operational troubleshooting. | Available |
| `list-donors` | `v0.3.0` | `--segment {corporate,institutional,individual,unknown}` | List donor records and segment membership. | Available |
| `monthly-audit-report` | `v1.0.0` | `--year YEAR`, `--month MONTH`, `--output FILE` | Generate a monthly GDPR/ISO compliance audit report as JSON. | Available |
| `gdpr-self-check-report` | `v1.0.0` | `--cadence {weekly,monthly}`, `--output FILE` | Generate a GDPR self-check report covering consent coverage, retention, exports, and deletions. | Available |
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

## Self-hosted data residency and privacy policies

Set these environment variables in self-hosted deployments:

| Variable | Default | Description |
| --- | --- | --- |
| `DATA_RESIDENCY` | `US` | Required residency zone: `US`, `EU`, or `ASIA`. |
| `DATA_STORAGE_REGION` | `DATA_RESIDENCY` | Runtime-observed storage location. Startup fails if it does not match `DATA_RESIDENCY`. |
| `PRIVACY_POLICY_OUTPUT_DIR` | `generated/privacy_policies` | Directory for generated HTML/PDF privacy policies. Use `/app/data/privacy_policies` in containers for persistence. |

Example:

```bash
export DATA_RESIDENCY=EU
export DATA_STORAGE_REGION=EU
export PRIVACY_POLICY_OUTPUT_DIR=/app/data/privacy_policies
```

The Settings panel can generate versioned privacy policies for one or more jurisdictions using the stored organization profile. Supported jurisdictions currently align with the residency zones: `US`, `EU`, and `ASIA`.

## Queue task retry and audit configuration

Queue task runs now persist into SQLite for auditability. Each run stores its
final result in `task_runs`, every retry attempt in `task_history`, and terminal
failures in `dead_letter_queue`.

| Variable | Default | Description |
| --- | --- | --- |
| `FUNDING_BOT_TASK_RETRY_LIMIT` | `3` | Maximum retry attempts after the initial queue-task failure. |
| `FUNDING_BOT_TASK_RETRY_BACKOFF_SECONDS` | `5` | Base retry delay in seconds before exponential backoff is applied. |
| `FUNDING_BOT_TASK_RETRY_BACKOFF_MAX_SECONDS` | `300` | Maximum retry delay cap in seconds. |

Queue-facing helpers (`run_discovery_task`, `send_outreach_task`, and
`send_daily_summary_task`) use exponential backoff, emit audit-log events such
as `queue_task_retry_scheduled` / `queue_task_completed` / `queue_task_failed`,
and move exhausted runs into the dead-letter queue for later review.

## Connector pagination and caching

Portal connectors now fetch remote results page-by-page and cache successful
results for a configurable polling window.

- cache keys include the connector ID, normalized keywords, and page size
- repeated discovery calls within the TTL reuse cached connector results
- cache invalidation is available programmatically with `connector.invalidate_cache()`
  or `connector.invalidate_cache(["keyword"])` for a single query
- connector cache metrics expose hits, misses, cache size, page size, and TTL
  through `connector.cache_metrics()` and the `/metrics` endpoint

Environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `PORTAL_PAGE_SIZE` | `100` | Global default page size for paginated connector requests. |
| `PORTAL_CACHE_TTL` | `300` | Global connector cache TTL in seconds. |
| `GRANTS_PORTAL_PAGE_SIZE` | inherits `PORTAL_PAGE_SIZE` | Page size override for the Grants Portal connector. |
| `GRANTS_PORTAL_CACHE_TTL` | inherits `PORTAL_CACHE_TTL` | Cache TTL override for the Grants Portal connector. |
| `CSR_NETWORK_PAGE_SIZE` | inherits `PORTAL_PAGE_SIZE` | Page size override for the CSR Network connector. |
| `CSR_NETWORK_CACHE_TTL` | inherits `PORTAL_CACHE_TTL` | Cache TTL override for the CSR Network connector. |
| `NGO_DIRECTORY_PAGE_SIZE` | inherits `PORTAL_PAGE_SIZE` | Page size override for the NGO Directory connector. |
| `NGO_DIRECTORY_CACHE_TTL` | inherits `PORTAL_CACHE_TTL` | Cache TTL override for the NGO Directory connector. |
| `GLOBALGIVING_PAGE_SIZE` | inherits `PORTAL_PAGE_SIZE` | Page size override for the GlobalGiving connector. |
| `GLOBALGIVING_CACHE_TTL` | inherits `PORTAL_CACHE_TTL` | Cache TTL override for the GlobalGiving connector. |
| `KICKSTARTER_FOR_GOOD_PAGE_SIZE` | inherits `PORTAL_PAGE_SIZE` | Page size override for the Kickstarter for Good connector. |
| `KICKSTARTER_FOR_GOOD_CACHE_TTL` | inherits `PORTAL_CACHE_TTL` | Cache TTL override for the Kickstarter for Good connector. |

## Connector rate limiting

Remote connector calls also use per-connector token-bucket rate limiting so
GlobalGiving, Kickstarter for Good, and other upstream APIs can be queried
without exhausting their quotas.

| Variable | Default | Description |
| --- | --- | --- |
| `PORTAL_RATE_LIMIT_DEFAULT_CAPACITY` | `5` | Global burst size per connector. |
| `PORTAL_RATE_LIMIT_DEFAULT_REFILL_RATE` | `1` | Global refill rate in tokens per second. |
| `GLOBALGIVING_RATE_LIMIT_CAPACITY` | inherits global default | GlobalGiving-specific burst size. |
| `GLOBALGIVING_RATE_LIMIT_REFILL_RATE` | inherits global default | GlobalGiving-specific refill rate. |
| `KICKSTARTER_FOR_GOOD_RATE_LIMIT_CAPACITY` | inherits global default | Kickstarter-specific burst size. |
| `KICKSTARTER_FOR_GOOD_RATE_LIMIT_REFILL_RATE` | inherits global default | Kickstarter-specific refill rate. |

Connector definitions supplied through `FUNDING_BOT_CONNECTORS` may also include a
`rate_limit` object:

```json
{
  "connectors": [
    {
      "type": "globalgiving",
      "transport": "http",
      "rate_limit": { "capacity": 2, "refill_rate": 0.5 }
    }
  ]
}
```

When a connector exceeds its quota, discovery degrades gracefully for that
connector: it returns no rows for that request and exposes `retry_after_seconds`
metadata instead of raising an exception.

## Connector TLS requirements

Outbound connector requests are HTTPS-only. The bot rejects insecure `http://`
connector URLs before making a request, uses a `requests` session with a minimum
TLS version of 1.2, and leaves certificate verification enabled.

## Web Dashboard

The dashboard is intended for v0.4.0+ operations and is already scaffolded in `web/app.py`.

### Run locally

```bash
pip install -r web/requirements.txt
python -m flask --app web.app run
```

### Accessibility checks

The dashboard templates now share a keyboard-visible skip link through
`web/templates/base.html`, and `web/static/dashboard.css` provides local
contrast-safe theme tokens so the WCAG audit does not depend on the Bootstrap
CDN. To run automated accessibility checks locally against the template fixture
app:

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
`/dashboard/tasks`, `/settings`, and `/translations` in both light and dark
mode, then performs explicit color-contrast assertions for the role chip, muted
helper copy, and status badges. The initial contrast audit found two Bootstrap
combinations below WCAG 2.1 AA for normal text: `text-white-50` on `bg-success`
(~2.22:1) and `text-muted` on `bg-light` (~4.45:1). The shared dashboard theme
replaces those with AA-compliant colors, and the same audit command runs in
GitHub Actions CI.

### Dashboard screenshot

![Funding Bot dashboard](docs/images/dashboard-screenshot.png)

### Role-based authentication

The dashboard uses HTTP Basic Auth to establish a signed Flask session. Session
cookies are issued with `Secure` and `HttpOnly` enabled, and idle sessions
expire after `DASHBOARD_SESSION_TIMEOUT_MINUTES` (default: `30`). Set
`FLASK_SECRET_KEY` in deployed environments before serving the dashboard.

Use one of these usernames as the role name:

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
| `/tasks` | `POST` | `admin` | Create a task with an assignee, optional due date, and initial workflow status. |
| `/tasks/<id>` | `GET` | `staff`, `admin`, `auditor` | Fetch one task as JSON. Staff users are limited to their own lane. |
| `/tasks/<id>/assign` | `POST` | `admin` | Assign or reassign a task to another dashboard role. |
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
| `/settings/privacy-policy` | `POST` | `admin` | Generate versioned privacy policies from the organization profile in HTML/PDF for one or more jurisdictions. |
| `/settings/test-outreach` | `POST` | `admin` | Compose (dry-run) or send a donor outreach email — proves the bot can communicate with donors. |
| `/translations` | `GET` | `staff`, `admin`, `auditor` | HTML translation review dashboard with locale preview and RTL rendering checks. |
| `/translations/locales` | `GET` | `staff`, `admin`, `auditor` | List supported locale metadata, including direction and RTL flags. |
| `/translations/reviews` | `GET` / `POST` | `staff`, `admin`, `auditor` for `GET`; `staff`, `admin` for `POST` | List review items or queue new locale content for approval. |
| `/translations/reviews/<id>/decision` | `POST` | `staff`, `admin` | Approve or reject a queued translation review item. |
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

### Translation review workflow

Use the `/translations` dashboard to stage and approve locale copy before it is used in outreach templates or future localized UI work.

1. Sign in as `staff` or `admin`.
2. Submit a locale change with the locale code, translation key, source text, translated text, and optional notes.
3. Each submission is stored with a `pending` review state.
4. A staff reviewer can approve (`approved`) or reject (`rejected`) the queued item from the same dashboard.
5. Review actions capture reviewer role, timestamp, and notes for auditability.

### RTL preview checks

Arabic (`ar`) and Urdu (`ur`) locale definitions are marked as right-to-left. The dashboard templates now use `dir`, logical text alignment, and shared RTL-safe CSS utilities so reviewers can preview future RTL rendering with a URL such as:

```bash
curl -u staff:$STAFF_PASSWORD "http://localhost:5000/translations?locale=ar"
```

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
| `funding_bot_queue_task_runs_running` | gauge | Queue task runs currently marked running in SQLite |
| `funding_bot_queue_task_runs_completed` | counter | Queue task runs completed successfully |
| `funding_bot_queue_task_runs_failed` | counter | Queue task runs that exhausted retries and failed |
| `funding_bot_queue_task_runs_cancelled` | counter | Queue task runs cancelled before completion |
| `funding_bot_queue_task_retries_total` | counter | Retry attempts scheduled with exponential backoff |
| `funding_bot_dead_letter_queue_total` | gauge | Queue task runs currently stored in the dead-letter queue |
| `funding_bot_queue_duplicate_preventions_total` | counter | Duplicate queue executions prevented by idempotency keys |

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
# Search every configured portal connector, including crowdfunding sources, and store any new opportunities.
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
For pull request workflow, review standards, setup steps, and contributor etiquette, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Docker Deployment

The repository includes a `Dockerfile` and `docker-compose.yml`.

1. Copy environment settings:

   ```bash
   cp .env.example .env
   ```

2. Update values in `.env` for SMTP credentials, database path, dashboard passwords, and queue flags.
3. Start the stack:

   ```bash
   docker compose --profile queue up --build
   ```

The Compose stack runs:
- a CLI container for bot jobs
- a Flask web container on `http://localhost:5000`
- `redis` and `rabbitmq` broker services for Celery
- a Celery `worker` and optional `flower` monitoring UI on `http://localhost:5555`
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

Use a `CronJob` for scheduled summary delivery, a second `CronJob` for retention cleanup (`python -m funding_bot enforce-data-retention`), and a `Deployment`/`Service` pair for the dashboard. If the `k8s/` manifests are not yet present in your branch, treat this as the target structure for the scaling release.

## Compliance Documentation

- [Accessibility conformance status](docs/ACCESSIBILITY.md)
- [Compliance procedures and checklists](docs/COMPLIANCE.md)
- [Translation contributor guidance](docs/TRANSLATIONS.md)

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
- `flower` for queue monitoring on port `5555`

Start the stack with:

```bash
docker compose --profile queue up --build
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
