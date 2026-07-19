# Health and readiness checks

The dashboard now exposes two public probe endpoints:

- `GET /health` — lightweight liveness probe for the Flask process and SQLite connection
- `GET /ready` — readiness probe for traffic serving and Kubernetes load-balancing decisions

## Strategy

### `/health`

Use `/health` for liveness-style checks. It performs only low-cost checks:

1. confirms the web process is running
2. runs `SELECT 1` against the configured database
3. returns `200` when both pass, otherwise `503`

### `/ready`

Use `/ready` for readiness-style checks. It validates the dependencies required to serve production traffic:

1. database connectivity
2. Redis availability for cache and/or Celery Redis targets when configured
3. Celery broker/worker availability when queue mode is enabled
4. connector availability by instantiating the active connector set and checking each connector health state

Checks that are intentionally disabled by configuration (for example Redis not configured or Celery queue mode turned off) report `status: "disabled"` and do not fail readiness.

## Response model

Both endpoints return:

- `status`: `ok` or `degraded`
- `checks`: per-component status payloads
- `failing_checks`: component names that caused degradation
- `metrics`: cumulative probe counters for endpoints and components

`/ready` also returns top-level `database`, `redis`, `celery`, and `connectors` sections for easy operator inspection.

## Metrics

The authenticated `GET /metrics` endpoint exports:

- `funding_bot_health_checks_total{endpoint="..."}`
- `funding_bot_health_failures_total{endpoint="..."}`
- `funding_bot_health_component_checks_total{component="..."}`
- `funding_bot_health_component_failures_total{component="..."}`

## Kubernetes guidance

- use `/health` for `startupProbe` and `livenessProbe`
- use `/ready` for `readinessProbe`
- alert on repeated `/ready` failures before pods cycle on `/health`
