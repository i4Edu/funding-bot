# Dashboard Load Testing

This guide covers the concurrent admin-session load test for the Flask dashboard.

## Tooling

- Python dev dependencies: `pip install -r requirements-dev.txt`
- Load generator: [Locust](https://locust.io/)
- Seed script: `tests/load/seed_dashboard_data.py`
- Scenario: `tests/load/locustfile.py`
- Threshold checker: `tests/load/assert_dashboard_load.py`

## What the scenario does

Each Locust user represents one authenticated admin session:

1. `GET /dashboard` with Basic Auth to establish the Flask session cookie
2. Reuse that cookie across concurrent requests to:
   - `/dashboard`
   - `/dashboard/tasks`
   - `/settings`
   - `/translations`
   - `/tasks`
   - `/api/tasks/export`
   - `/metrics`

This exercises the main dashboard pages and supporting JSON/metrics endpoints under concurrent admin traffic.

## Local run

1. Install dependencies:

   ```bash
   pip install -r requirements-dev.txt
   ```

2. Seed a dedicated load-test database:

   ```bash
   python tests/load/seed_dashboard_data.py --db-path .load-test-dashboard.db
   ```

3. Start the dashboard with load-test credentials:

   ```bash
   export ADMIN_PASSWORD=admin-secret
   export STAFF_PASSWORD=staff-secret
   export AUDITOR_PASSWORD=auditor-secret
   export BOT_DB_PATH=.load-test-dashboard.db
   export SESSION_COOKIE_SECURE=0
   python -m flask --app web.app run --host 127.0.0.1 --port 5001
   ```

   `SESSION_COOKIE_SECURE=0` is only for local/plain-HTTP load-test runs so the authenticated session cookie can be reused by Locust.

4. Run the headless load test from another shell:

   ```bash
   export LOAD_TEST_PASSWORD=admin-secret
   locust \
     -f tests/load/locustfile.py \
     --host http://127.0.0.1:5001 \
     --headless \
     -u 12 \
     -r 3 \
     -t 45s \
     --html dashboard-load-report.html \
     --csv dashboard-load
   ```

5. Validate the captured response-time and throughput thresholds:

   ```bash
   python tests/load/assert_dashboard_load.py \
     --csv-prefix dashboard-load \
     --max-failures 0 \
     --max-p95-ms 750 \
     --min-rps 5
   ```

## Metrics to capture

- total request count
- failures
- median / average / p95 response time
- aggregate throughput (requests per second)

Locust writes:

- `dashboard-load_stats.csv`
- `dashboard-load_stats_history.csv`
- `dashboard-load_failures.csv`
- `dashboard-load-report.html`

## CI coverage

The GitHub Actions workflow includes:

- `signature-property-tests`
- `dashboard-load-test`

The load-test job seeds dashboard data, starts the Flask app, runs headless Locust, validates thresholds, and uploads the HTML/CSV artifacts for review.
