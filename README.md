# funding-bot

Nonprofit Funding Automation Bot

This repository includes a Python implementation that covers the full core
workflow described in the project brief:

- store nonprofit profile data and credential references
- discover funding opportunities from trusted sources with keyword filters
- prevent duplicate applications with SQLite-backed tracking
- record browser-driven submission retries and statuses
- send throttled, opt-out-aware donor outreach
- generate PDF and Word-compatible application documents
- build and email a daily summary report to `lupael@i4e.com.bd`

## Files

- `funding_bot.py` – bot logic and CLI entry point
- `tests/test_funding_bot.py` – unit tests

## Run tests

```bash
python -m unittest discover -s tests
```

## CLI usage

```bash
# Print the daily summary without sending it
python -m funding_bot send-daily-summary --dry-run

# Send the daily summary via SMTP (reads settings from environment variables)
python -m funding_bot send-daily-summary --recipient lupael@i4e.com.bd
```

## SMTP configuration

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

## Scheduling the daily report

Use a system cron job to send the report every day at 9 AM:

```cron
0 9 * * * cd /path/to/funding-bot && python -m funding_bot send-daily-summary
```
