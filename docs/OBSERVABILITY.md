# Observability

The funding bot now exposes distributed tracing and service-level objective (SLO) reporting for the dashboard, connector workflows, and Celery-backed task queue.

## Distributed tracing

- OpenTelemetry is configured through `observability.py`.
- Incoming dashboard requests create server spans and return both `traceparent` and `X-Trace-Id` headers.
- Connector fetches create child spans and propagate span context to outbound HTTP headers.
- Task queue dispatch captures the current trace context and forwards it to Celery jobs so worker spans join the originating request trace.

### Environment variables

```env
FUNDING_BOT_TRACE_EXPORTER=otlp
OTEL_SERVICE_NAME=funding-bot
OTEL_SERVICE_NAMESPACE=funding-bot
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://jaeger:4318/v1/traces
```

Set `FUNDING_BOT_TRACE_EXPORTER=console` to emit spans to stdout, or `none` to disable exporting.

### Jaeger

`docker-compose.yml` includes a `jaegertracing/all-in-one` service:

- Jaeger UI: `http://127.0.0.1:16686`
- OTLP/HTTP collector: `http://127.0.0.1:4318/v1/traces`

When running in Kubernetes, configure the collector endpoint with the `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` config map value.

## Service-level objectives

SLO samples are stored in the application database (`slo_events`) and summarized over a rolling 24-hour window.

| SLO | Target |
| --- | --- |
| Connector latency | p95 latency <= 2.0s, error rate <= 5% |
| Task queue throughput | p95 latency <= 60s, error rate <= 2%, throughput >= 5 jobs/hour |
| Dashboard response time | p95 latency <= 750ms, error rate <= 1%, throughput >= 5 requests/hour |

## Reporting

- Dashboard UI: `/dashboard` includes an SLO summary card.
- JSON API: `/api/slo`
- Prometheus: `/metrics`
- Grafana: `monitoring/grafana-dashboards/slo-overview.json`

Prometheus exports include:

- `funding_bot_slo_latency_p95_seconds{operation=...}`
- `funding_bot_slo_error_rate{operation=...}`
- `funding_bot_slo_success_rate{operation=...}`
- `funding_bot_slo_throughput_per_hour{operation=...}`
- `funding_bot_slo_compliance{operation=...}`

## Operational flow

1. Start the app stack plus Jaeger.
2. Exercise `/dashboard`, `/settings/discover`, or any Celery-backed job.
3. Inspect traces in Jaeger by filtering on the `funding-bot` service.
4. Scrape `/metrics` with Prometheus and import the Grafana SLO dashboard.
