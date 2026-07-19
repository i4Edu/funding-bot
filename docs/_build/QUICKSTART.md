# Quickstart

Use this guide to get the funding bot running locally for the first time.

## Prerequisites

- Git
- Python 3.11+
- `pip`
- Node.js 20+ and `npm` (for accessibility checks)
- Docker + Docker Compose (optional, for containerized setup)

## 1. Clone the repository

```bash
git clone https://github.com/<your-org>/funding-bot.git
cd funding-bot
```

## 2. Install dependencies

### Python app dependencies

```bash
python3 -m pip install -r web/requirements.txt
```

### Accessibility test dependencies (optional)

```bash
npm install
npx playwright install chromium
```

## 3. Create your local configuration

Copy the sample environment file:

```bash
cp .env.example .env
```

Then update the values you need in `.env`.

### Minimum local-development changes

- Set `ADMIN_PASSWORD`, `STAFF_PASSWORD`, and `AUDITOR_PASSWORD`
- Set `FLASK_SECRET_KEY` to any long random string
- Keep `SESSION_COOKIE_SECURE=0` for local `http://localhost` development
- If you are **not** using Docker, change `BOT_DB_PATH` from `/app/data/funding_bot.db` to `funding_bot.db`
- If you plan to send real email, fill in the SMTP settings

For the full variable reference, see [ENV_VARS.md](ENV_VARS.md).

### Optional assisted setup

The repository includes a helper script that can generate and populate `.env`:

```bash
./scripts/onboard.sh --skip-docker
```

## 4. First run

### Option A: Run locally with Python

Start the dashboard:

```bash
python3 -m flask --app web.app run --host 127.0.0.1 --port 5000
```

Open:

- Dashboard: <http://127.0.0.1:5000/dashboard>
- Health check: <http://127.0.0.1:5000/health>
- Metrics: <http://127.0.0.1:5000/metrics>

You can also smoke-test the CLI:

```bash
python3 -m funding_bot send-daily-summary --dry-run
```

### Option B: Run with Docker Compose

Keep `BOT_DB_PATH=/app/data/funding_bot.db` and start the containers:

```bash
docker compose up --build
```

To start the queue worker, Redis, RabbitMQ, and Flower too:

```bash
docker compose --profile queue up --build
```

Services:

- Web UI: <http://127.0.0.1:5000/dashboard>
- Health: <http://127.0.0.1:5000/health>
- Flower (queue profile): <http://127.0.0.1:5555>

## 5. Useful validation commands

```bash
python3 -m unittest discover -s tests
pytest tests/test_smoke.py -m quick -q
pytest tests/test_smoke.py -m smoke -q
python3 -m funding_bot list-opportunities
python3 -m funding_bot send-daily-summary --dry-run
```

To capture flaky smoke-test tracking artifacts locally:

```bash
mkdir -p test-results
pytest tests/test_smoke.py -m smoke -q --reruns 2 --reruns-delay 1 \
  --flaky-report=test-results/flaky-report.json \
  --flaky-report-markdown=test-results/flaky-report.md \
  --test-reliability-metrics=test-results/test-reliability.prom
```

Optional accessibility run:

```bash
python3 -m flask --app tests.accessibility.app run --host 127.0.0.1 --port 5001
npm run test:a11y
```

## Troubleshooting

### Login keeps failing locally

Set `SESSION_COOKIE_SECURE=0` in `.env` when running over plain HTTP on localhost.

### `ModuleNotFoundError` or missing Flask/Celery packages

Reinstall Python dependencies:

```bash
python3 -m pip install -r web/requirements.txt
```

### Database path errors

- Local Python run: use `BOT_DB_PATH=funding_bot.db` or another writable local path
- Docker run: use `BOT_DB_PATH=/app/data/funding_bot.db`

### Email sending fails

- Verify `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, and `SMTP_FROM`
- Use `python3 -m funding_bot send-daily-summary --dry-run` first to validate templates without sending mail

### Queue health is degraded

- Confirm `ENABLE_TASK_QUEUE=1` only when Redis/RabbitMQ and the worker are running
- Re-run with `docker compose --profile queue up --build`
- Check `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, and `CELERY_QUEUE_NAME`

### Flower is unavailable

Start the queue profile and optionally set `FLOWER_BASIC_AUTH` in `.env` before launching it.

## Related docs

- [Environment variable reference](ENV_VARS.md)
- [Glossary of key terms](GLOSSARY.md)
- [Video walkthroughs](VIDEOS.md)
- [Deployment guide](DEPLOYMENT.md)
- [Security guide](SECURITY.md)
