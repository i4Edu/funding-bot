# Deployment and Scaling Guide

This document covers production deployment for the funding bot web app, legacy cron jobs, and the new Celery worker fleet used during the cron-to-queue migration. Contributor setup and PR expectations for deployment-related changes live in [../CONTRIBUTING.md](../CONTRIBUTING.md).

## Deployment modes

The application supports three runtime modes:

| Mode | `ENABLE_TASK_QUEUE` | `ENABLE_LEGACY_CRON` | Use when |
| --- | --- | --- | --- |
| Legacy cron | `0` | `1` | Existing single-node installs that still rely on CLI/CronJob scheduling. |
| Hybrid migration | `1` | `1` | Rolling out Celery workers while keeping the old cron path available. |
| Queue-first | `1` | `0` | Celery workers are fully adopted and cron has been retired. |

Hybrid mode is the recommended migration step because it lets operators verify queue health, worker capacity, and Flower monitoring before disabling cron.

## Required environment variables

| Variable | Example | Purpose |
| --- | --- | --- |
| `BOT_DB_PATH` | `/app/data/funding_bot.db` | Shared SQLite data path. |
| `ENABLE_TASK_QUEUE` | `1` | Enables Celery dispatch for queue-backed workflows. |
| `ENABLE_LEGACY_CRON` | `1` | Keeps legacy cron/CLI scheduling active. |
| `CELERY_BROKER_URL` | `redis://redis:6379/0` | Broker connection for worker/task transport. |
| `CELERY_RESULT_BACKEND` | `redis://redis:6379/1` | Result backend for task metadata. |
| `CELERY_QUEUE_NAME` | `funding-bot` | Queue consumed by workers. |
| `CELERY_TASK_ALWAYS_EAGER` | `0` | Local/test-only inline execution toggle. |
| `FUNDING_BOT_DB_POOL_SIZE` | `5` | Base SQLAlchemy connection pool size for the SQLite engine. |
| `FUNDING_BOT_DB_MAX_OVERFLOW` | `10` | Extra burst connections allowed beyond the base pool size. |
| `FUNDING_BOT_DB_POOL_TIMEOUT_SECONDS` | `30` | Wait time before pool checkout fails. |
| `FUNDING_BOT_DB_POOL_RECYCLE_SECONDS` | `1800` | Connection recycle interval for long-lived workers/web processes. |
| `FUNDING_BOT_CACHE_BACKEND` | `redis` | Cache backend (`redis` or `memory`). |
| `FUNDING_BOT_CACHE_URL` | `redis://redis:6379/2` | Shared Redis DB for donor/connector/profile caches. |
| `FUNDING_BOT_DONOR_CACHE_TTL_SECONDS` | `300` | TTL for donor-record cache entries. |
| `FUNDING_BOT_CONNECTOR_CACHE_TTL_SECONDS` | `60` | TTL for connector-state cache entries. |
| `FUNDING_BOT_DEDUPED_PROFILE_CACHE_TTL_SECONDS` | `600` | TTL for deduplicated profile cache entries. |
| `ADMIN_PASSWORD` / `STAFF_PASSWORD` / `AUDITOR_PASSWORD` | `...` | Dashboard authentication. |
| SMTP settings | `SMTP_HOST`, `SMTP_PORT`, etc. | Required for real email delivery. |

## Docker Compose

### Legacy cron only

```bash
cp .env.example .env
docker compose up --build
```

### Hybrid or queue-first

1. Set:

   ```bash
   ENABLE_TASK_QUEUE=1
   ENABLE_LEGACY_CRON=1   # set to 0 after migration
   CELERY_BROKER_URL=redis://redis:6379/0
   CELERY_RESULT_BACKEND=redis://redis:6379/1
   CELERY_QUEUE_NAME=funding-bot
   ```

2. Start the queue profile:

   ```bash
   docker compose --profile queue up --build
   ```

This starts:

- `web` for the Flask dashboard/API
- `bot` for legacy CLI/cron execution
- `redis` as the default broker
- `worker` for Celery task execution
- `flower` for queue monitoring on `http://localhost:5555`

## Kubernetes

The repository ships dashboard manifests for `Deployment`, `Service`, `Ingress`, `HPA`, and `VPA` under `k8s/`. Apply the namespace first, then the remaining resources. For a step-by-step rollout, see [KUBERNETES.md](KUBERNETES.md).

For queue mode, add:

1. A broker deployment/service (Redis or RabbitMQ)
2. A Celery worker deployment
3. An optional Flower deployment/service

### Recommended worker deployment shape

Use a dedicated deployment with the same container image as the web app:

```yaml
command:
  - celery
  - -A
  - celery_app:celery_app
  - worker
  - --loglevel=info
  - --queues
  - funding-bot
```

Recommended baseline resources per worker pod:

- **requests**: `100m` CPU / `128Mi` memory
- **limits**: `500m` CPU / `512Mi` memory

Adjust based on connector latency, document generation volume, and SMTP throughput.

## Pre-scale load testing

Before increasing dashboard replicas or worker counts, run the concurrent admin-session dashboard load test documented in [LOAD_TESTING.md](LOAD_TESTING.md). Capture p95 latency and throughput from the generated Locust CSV/HTML artifacts so you can compare results before and after scaling changes.

## Health checks and monitoring

### Application health endpoints

- `GET /health` is the lightweight liveness endpoint for the Flask process and database
- `GET /ready` is the readiness endpoint for dependency-aware traffic admission
- `GET /health/queue` returns queue-only diagnostics:
  - queue mode (`cron`, `queue`, or `hybrid`)
  - worker count and worker names
  - active task count
  - queue depth / pending tasks
  - whether legacy cron is still enabled
- `GET /health/database` returns SQLAlchemy pool status, configured sizing, lifecycle counters, and aggregated query-monitoring data
- `GET /health/cache` returns cache backend/reachability details

`/ready` checks database, Redis, Celery, and connector health. Integrations that are intentionally disabled by configuration report `status: "disabled"` and do not fail readiness. See [HEALTH_CHECKS.md](HEALTH_CHECKS.md) for the detailed strategy.

Use `/health/queue` for worker-specific alerts, `/health` for liveness, and `/ready` for readiness.

### Flower dashboard

Run Flower with:

```bash
celery -A celery_app:celery_app flower --port=5555
```

Flower should be protected behind authentication or a private network boundary. Recommended uses:

- inspect worker availability
- confirm tasks are routed to `funding-bot`
- monitor retry spikes or stuck tasks
- watch queue depth during traffic bursts

When running via Docker Compose, set `FLOWER_BASIC_AUTH=user:password` in `.env`
to require login for the Flower UI.

### Graceful shutdown and idempotency

- Workers receive `SIGTERM` with a 45-second Docker stop grace period.
- In-flight queue tasks persist `shutdown_requested=1` in `task_runs` before
  exiting, so operators can distinguish clean drains from crashes.
- Every queued workflow stores an `idempotency_key` plus `duplicate_requests`
  in SQLite to prevent duplicate execution during retries, restarts, or repeat
  enqueue requests.
- Monitor `funding_bot_queue_duplicate_preventions_total`,
  `funding_bot_queue_task_runs_cancelled`, and `funding_bot_dead_letter_queue_total`
  to confirm shutdowns and retries are behaving as expected.

### Metrics and alerts

The `/metrics` endpoint exports queue metrics alongside app metrics, SQLAlchemy pool counters, database query histograms/counters, cache hit/miss/set/invalidation metrics, and health probe counters. Alert on:

- readiness failures increasing over time
- queue health status dropping to `0`
- worker count dropping below the expected replica count
- queue depth growing continuously over multiple scrape intervals
- active tasks staying high with no drop in pending depth
- database pool checked-out connections remaining near the configured size
- database query error rate exceeding your SLO
- database query timeouts or lock timeouts appearing at all
- slow query ratio or p95 latency staying above the tuned threshold
- cache miss ratios jumping unexpectedly for donor/profile lookups

Example assets are checked into `monitoring/`:

- `prometheus.yml` scrape config
- `prometheus-alert-rules.yml` alert rules
- `alertmanager.yml` Slack/email notification routing
- `grafana-dashboards/database-query-performance.json` query dashboard

See `docs/ALERTING.md` for threshold tuning and secret wiring.

## Scaling strategy

### Horizontal scaling

The dashboard HPA in `k8s/hpa.yaml` targets 70% CPU and 75% memory utilization with a 2-6 pod range. Scale workers horizontally before scaling the web app:

- **1 worker**: low traffic, manual operations, small cron migration
- **2-3 workers**: normal office-hour operation with concurrent discovery/outreach tasks
- **4+ workers**: heavy partner usage, multiple organizations, or bursty scheduled jobs

Examples:

```bash
docker compose --profile queue up --scale worker=3
kubectl scale deployment/funding-bot-worker --replicas=4 -n funding-bot
```

### Queue partitioning

Start with a single queue named `funding-bot`. Split queues when one workload dominates others:

- `funding-bot-discovery` for portal scans
- `funding-bot-outreach` for email composition/delivery
- `funding-bot-reporting` for daily summaries and reports

Only introduce queue partitioning after confirming a real throughput bottleneck.

### Worker sizing guidance

- Increase **replica count** for I/O-bound tasks or backlogs
- Increase **CPU/memory limits** for document generation or heavier processing
- Use the dashboard VPA in `k8s/vpa.yaml` as a recommendation baseline for new pod sizing
- Keep cron enabled during the first scale-out window so scheduled reporting still runs if workers are temporarily unavailable

## Migration path from cron to Celery

1. **Baseline**: `ENABLE_TASK_QUEUE=0`, `ENABLE_LEGACY_CRON=1`
2. **Enable hybrid mode**: turn on `ENABLE_TASK_QUEUE=1` and deploy broker + workers
3. **Verify**:
   - `/ready` returns `200`
   - `/health/queue` reports healthy workers
   - Flower shows the expected queue and task names
   - web-triggered discovery returns queued task metadata
4. **Scale workers** until queue depth remains stable during peak usage
5. **Disable cron** by setting `ENABLE_LEGACY_CRON=0`
6. **Remove legacy CronJobs** only after multiple healthy release cycles

## Operational checklist

- [ ] Broker is reachable from web and worker pods
- [ ] `ENABLE_TASK_QUEUE` and `ENABLE_LEGACY_CRON` reflect the intended mode
- [ ] `/health` returns `200`
- [ ] `/ready` returns `200`
- [ ] `/health/queue` returns healthy workers
- [ ] Flower is reachable for operators
- [ ] Worker replicas match observed queue depth
- [ ] Legacy cron is left enabled until queue mode is proven stable
