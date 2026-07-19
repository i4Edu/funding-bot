from __future__ import annotations

import os
from typing import Any

from celery import Celery

from funding_bot import FundingBot, QueueTaskContext, SMTPEmailSender


def _broker_url() -> str:
    return os.environ.get("CELERY_BROKER_URL", "redis://redis:6379/0")


celery_app = Celery(
    "funding_bot",
    broker=_broker_url(),
    backend=os.environ.get("CELERY_RESULT_BACKEND", "redis://redis:6379/1"),
)
celery_app.conf.update(
    task_track_started=True,
    worker_send_task_events=True,
    task_send_sent_event=True,
)


def _bot() -> FundingBot:
    return FundingBot(db_path=os.environ.get("BOT_DB_PATH", "funding_bot.db"))


def _worker_id(task_request: Any) -> str:
    hostname = getattr(task_request, "hostname", None) or "unknown-worker"
    return f"celery:{hostname}"


@celery_app.task(bind=True, name="funding_bot.run_discovery")
def run_discovery_task(
    self: Any,
    *,
    keywords: list[str] | None = None,
    trusted_sources: list[str] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    payload = {
        "keywords": keywords or [],
        "trusted_sources": trusted_sources or [],
    }
    bot = _bot()
    try:
        task_run = bot.execute_queue_task(
            "run_discovery",
            payload,
            _run_discovery,
            idempotency_key=idempotency_key,
            worker_id=_worker_id(self.request),
        )
        return task_run
    finally:
        bot.close()


def _run_discovery(context: QueueTaskContext, payload: dict[str, Any]) -> dict[str, Any]:
    context.checkpoint("Shutdown requested before discovery started.")
    found = context.bot.run_discovery(
        keywords=payload.get("keywords") or None,
        trusted_sources=payload.get("trusted_sources") or None,
    )
    context.checkpoint("Shutdown requested after discovery completed.")
    return {"count": len(found), "new_opportunities": found}


@celery_app.task(bind=True, name="funding_bot.send_daily_summary")
def send_daily_summary_task(
    self: Any,
    *,
    recipient: str | None = None,
    dry_run: bool = True,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    payload = {"recipient": recipient, "dry_run": dry_run}
    bot = _bot()
    try:
        task_run = bot.execute_queue_task(
            "send_daily_summary",
            payload,
            _send_daily_summary,
            idempotency_key=idempotency_key,
            worker_id=_worker_id(self.request),
        )
        return task_run
    finally:
        bot.close()


def _send_daily_summary(context: QueueTaskContext, payload: dict[str, Any]) -> dict[str, Any]:
    context.checkpoint("Shutdown requested before building the daily summary.")
    sender = None if payload.get("dry_run", True) else SMTPEmailSender.from_env()
    summary = context.bot.send_daily_summary(recipient=payload.get("recipient"), sender=sender)
    context.checkpoint("Shutdown requested after the daily summary task finished.")
    return summary


@celery_app.task(bind=True, name="funding_bot.send_outreach")
def send_outreach_task(
    self: Any,
    *,
    donor_email: str,
    donor_name: str,
    subject_template: str,
    body_template: str,
    dry_run: bool = True,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    payload = {
        "donor_email": donor_email,
        "donor_name": donor_name,
        "subject_template": subject_template,
        "body_template": body_template,
        "dry_run": dry_run,
    }
    bot = _bot()
    try:
        task_run = bot.execute_queue_task(
            "send_outreach",
            payload,
            _send_outreach,
            idempotency_key=idempotency_key,
            worker_id=_worker_id(self.request),
        )
        return task_run
    finally:
        bot.close()


def _send_outreach(context: QueueTaskContext, payload: dict[str, Any]) -> dict[str, Any]:
    context.checkpoint("Shutdown requested before donor outreach started.")
    sender = None if payload.get("dry_run", True) else SMTPEmailSender.from_env()
    result = context.bot.send_outreach(
        donor_email=str(payload["donor_email"]),
        donor_name=str(payload["donor_name"]),
        subject_template=str(payload["subject_template"]),
        body_template=str(payload["body_template"]),
        sender=sender,
    )
    context.checkpoint("Shutdown requested after donor outreach completed.")
    return result
