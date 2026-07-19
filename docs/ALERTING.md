# Alerting and query monitoring

This repository includes a Prometheus + Alertmanager starter configuration for database query monitoring.

## Files

- `monitoring/prometheus.yml` — example scrape configuration for `/metrics`
- `monitoring/prometheus-alert-rules.yml` — alert rules for query failures, slow queries, timeouts, queue health, and pool saturation
- `monitoring/alertmanager.yml` — Slack and email notification routing
- `monitoring/grafana-dashboards/database-query-performance.json` — Grafana dashboard for query throughput, p95 latency, slow-query ratio, and timeouts

## Application metrics

The app exports database query metrics at `/metrics`:

- `funding_bot_db_queries_total{statement,status}`
- `funding_bot_db_query_errors_total{statement}`
- `funding_bot_db_query_timeouts_total{statement}`
- `funding_bot_db_slow_queries_total{statement}`
- `funding_bot_db_query_duration_seconds_bucket{statement,le}`
- `funding_bot_db_query_duration_seconds_sum{statement}`
- `funding_bot_db_query_duration_seconds_count{statement}`
- `funding_bot_db_query_duration_seconds_max{statement}`
- `funding_bot_db_query_slow_threshold_seconds`
- `funding_bot_db_queries_in_flight{statement}`

The `statement` label is the normalized SQL verb (`select`, `insert`, `update`, `delete`, `pragma`, and similar). The synthetic `statement="all"` series is emitted for alert formulas that should look at total query volume.

## Slow-query threshold

Set the threshold with:

```bash
export FUNDING_BOT_DB_SLOW_QUERY_THRESHOLD_SECONDS=0.25
```

Tune it per environment:

- **0.10-0.25s** for local development and lightweight test environments
- **0.25-0.50s** for normal production workloads on SQLite
- **0.50s+** when batch imports or heavy reporting jobs are expected

## Alert tuning

Default rules in `monitoring/prometheus-alert-rules.yml` alert when:

- query error/timeout rate exceeds **5%** for 10 minutes
- slow queries exceed **20%** of traffic for 15 minutes
- any timeout occurs within 10 minutes
- p95 query latency exceeds **750ms** for 15 minutes

Adjust the expressions if your workload has expected bursts, migrations, or scheduled batch jobs.

## Slack and email notifications

Update `monitoring/alertmanager.yml` before deployment:

- replace Slack channels with real destinations
- mount a secret containing the Slack webhook at `/etc/alertmanager/secrets/slack-webhook-url`
- mount SMTP credentials at `/etc/alertmanager/secrets/email-password`
- replace sender/recipient addresses and `smarthost`

Example Kubernetes secrets:

```bash
kubectl create secret generic funding-bot-alertmanager \
  --from-literal=slack-webhook-url='https://hooks.slack.com/services/...' \
  --from-literal=email-password='replace-me' \
  -n funding-bot
```

## Dashboard import

Import `monitoring/grafana-dashboards/database-query-performance.json` into Grafana and point it at your Prometheus datasource.

Recommended first panels to watch:

1. **P95 query duration by statement**
2. **Slow query ratio**
3. **Errors and timeouts (15m)**
4. **Queries in flight**

## Health endpoints

- `GET /health/database` now returns pool metrics plus a `queries` section with aggregated query-monitoring data.
- `GET /metrics` exposes the Prometheus series used by the rules and dashboard.
