# funding-bot

Nonprofit Funding Automation Bot

This repository now includes a small Python MVP that covers the core workflow
described in the project brief:

- store nonprofit profile data and credential references
- discover funding opportunities from trusted sources with keyword filters
- prevent duplicate applications with SQLite-backed tracking
- record browser-driven submission retries and statuses
- send throttled, opt-out-aware donor outreach
- generate PDF and Word-compatible application documents
- build a daily summary email for `lupael@i4e.com.bd`

## Files

- `funding_bot.py` – bot logic
- `tests/test_funding_bot.py` – focused unit tests

## Run tests

```bash
python -m unittest discover -s tests
```
