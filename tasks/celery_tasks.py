from __future__ import annotations

from typing import Any

from celery_app import celery_app
from funding_bot import FundingBot, SMTPEmailSender


def _resolve_db_path(db_path: str | None) -> str | None:
    return db_path if db_path else None


@celery_app.task(name="funding_bot.discover_opportunities")
def run_discovery_task(
    *,
    db_path: str | None = None,
    keywords: list[str] | None = None,
    trusted_sources: list[str] | None = None,
) -> dict[str, Any]:
    bot = FundingBot(db_path=_resolve_db_path(db_path))
    try:
        opportunities = bot.run_discovery(
            keywords=keywords,
            trusted_sources=trusted_sources,
        )
        return {"count": len(opportunities), "opportunities": opportunities}
    finally:
        bot.close()


@celery_app.task(name="funding_bot.send_daily_summary")
def send_daily_summary_task(
    *,
    recipient: str,
    db_path: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    bot = FundingBot(db_path=_resolve_db_path(db_path))
    try:
        sender = None if dry_run else SMTPEmailSender.from_env()
        summary = bot.send_daily_summary(recipient=recipient, sender=sender)
        return {"recipient": recipient, "dry_run": dry_run, **summary}
    finally:
        bot.close()


@celery_app.task(name="funding_bot.send_outreach")
def send_outreach_task(
    *,
    donor_email: str,
    donor_name: str,
    subject_template: str,
    body_template: str,
    db_path: str | None = None,
    dry_run: bool = False,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bot = FundingBot(db_path=_resolve_db_path(db_path))
    try:
        sender = None if dry_run else SMTPEmailSender.from_env()
        result = bot.send_outreach(
            donor_email=donor_email,
            donor_name=donor_name,
            subject_template=subject_template,
            body_template=body_template,
            context=context,
            sender=sender,
        )
        return {"dry_run": dry_run, **result}
    finally:
        bot.close()
