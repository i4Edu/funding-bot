from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from celery import Celery, Task
from celery.schedules import crontab

from funding_bot import FundingBot, SMTPEmailSender

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = os.environ.get("BOT_DB_PATH", "funding_bot.db")
BROKER_DIR = Path(os.environ.get("CELERY_FILESYSTEM_BROKER_DIR", PROJECT_ROOT / ".celery-broker"))
QUEUE_DIR = BROKER_DIR / "queue"
PROCESSED_DIR = BROKER_DIR / "processed"
CONTROL_DIR = BROKER_DIR / "control"
for directory in (QUEUE_DIR, PROCESSED_DIR, CONTROL_DIR):
    directory.mkdir(parents=True, exist_ok=True)


def _env_flag(name: str, default: bool = False) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _create_celery_app() -> Celery:
    broker_url = os.environ.get("CELERY_BROKER_URL", "filesystem://")
    result_backend = os.environ.get("CELERY_RESULT_BACKEND", "cache+memory://")
    app = Celery("funding_bot", broker=broker_url, backend=result_backend)
    app.conf.update(
        accept_content=["json"],
        result_serializer="json",
        task_serializer="json",
        enable_utc=True,
        timezone="UTC",
        task_track_started=True,
        task_send_sent_event=True,
        task_always_eager=_env_flag("CELERY_TASK_ALWAYS_EAGER"),
        task_store_eager_result=_env_flag("CELERY_TASK_STORE_EAGER_RESULT", True),
        broker_transport_options={
            "data_folder_in": str(QUEUE_DIR),
            "data_folder_out": str(QUEUE_DIR),
            "data_folder_processed": str(PROCESSED_DIR),
            "control_folder": str(CONTROL_DIR),
        },
        beat_schedule={
            "daily-summary": {
                "task": "funding_bot.send_daily_summary",
                "schedule": crontab(
                    minute=int(os.environ.get("DAILY_SUMMARY_SCHEDULE_MINUTE", "0")),
                    hour=int(os.environ.get("DAILY_SUMMARY_SCHEDULE_HOUR", "9")),
                ),
                "kwargs": {
                    "db_path": DEFAULT_DB_PATH,
                    "recipient": os.environ.get("DAILY_SUMMARY_RECIPIENT", "lupael@i4e.com.bd"),
                    "dry_run": _env_flag("DAILY_SUMMARY_DRY_RUN"),
                },
            }
        },
    )
    return app


app = _create_celery_app()


class FundingBotTask(Task):
    abstract = True

    def _db_path(self, kwargs: dict[str, Any]) -> str:
        return str(kwargs.get("db_path") or DEFAULT_DB_PATH)

    def _record(
        self,
        task_id: str,
        task_name: str,
        *,
        status: str,
        progress: int,
        message: str,
        kwargs: dict[str, Any],
        result: dict[str, Any] | None = None,
        error_message: str | None = None,
        callback_name: str | None = None,
        callback_payload: dict[str, Any] | None = None,
        completed: bool = False,
    ) -> None:
        bot = FundingBot(db_path=self._db_path(kwargs))
        try:
            bot.record_task_run(
                task_id,
                task_name,
                status=status,
                progress=progress,
                message=message,
                payload={key: value for key, value in kwargs.items() if key != "db_path"},
                result=result,
                error_message=error_message,
                callback_name=callback_name,
                callback_payload=callback_payload,
                completed_at=bot._utcnow() if completed else None,
            )
        finally:
            bot.close()

    def before_start(self, task_id: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        self._record(
            task_id,
            self.name,
            status="STARTED",
            progress=0,
            message="Task accepted by worker.",
            kwargs=kwargs,
        )

    def update_progress(
        self,
        progress: int,
        message: str,
        *,
        kwargs: dict[str, Any],
        meta: dict[str, Any] | None = None,
    ) -> None:
        self._record(
            self.request.id,
            self.name,
            status="PROGRESS",
            progress=progress,
            message=message,
            kwargs=kwargs,
            callback_name="progress",
            callback_payload=meta,
        )
        self.update_state(
            state="PROGRESS",
            meta={"progress": progress, "message": message, **(meta or {})},
        )

    def on_success(
        self,
        retval: Any,
        task_id: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        self._record(
            task_id,
            self.name,
            status="SUCCESS",
            progress=100,
            message="Task completed successfully.",
            kwargs=kwargs,
            result=retval if isinstance(retval, dict) else {"result": retval},
            callback_name="on_success",
            callback_payload={"state": "SUCCESS"},
            completed=True,
        )

    def on_failure(
        self,
        exc: BaseException,
        task_id: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        einfo: Any,
    ) -> None:
        self._record(
            task_id,
            self.name,
            status="FAILURE",
            progress=100,
            message="Task failed.",
            kwargs=kwargs,
            error_message=str(exc),
            callback_name="on_failure",
            callback_payload={"state": "FAILURE"},
            completed=True,
        )


@app.task(bind=True, base=FundingBotTask, name="funding_bot.discover")
def discover_task(
    self: FundingBotTask,
    *,
    db_path: str | None = None,
    keywords: list[str] | None = None,
    trusted_sources: list[str] | None = None,
) -> dict[str, Any]:
    task_kwargs = {
        "db_path": db_path,
        "keywords": keywords,
        "trusted_sources": trusted_sources,
    }
    self.update_progress(15, "Loading search settings.", kwargs=task_kwargs)
    bot = FundingBot(db_path=db_path or DEFAULT_DB_PATH)
    try:
        self.update_progress(55, "Running connector discovery.", kwargs=task_kwargs)
        found = bot.run_discovery(keywords=keywords, trusted_sources=trusted_sources)
        result = {
            "count": len(found),
            "new_opportunities": found,
            "keywords": keywords or [],
            "trusted_sources": trusted_sources or [],
        }
        self.update_progress(90, "Persisted discovery results.", kwargs=task_kwargs, meta={"count": len(found)})
        return result
    finally:
        bot.close()


@app.task(bind=True, base=FundingBotTask, name="funding_bot.send_outreach")
def send_outreach_task(
    self: FundingBotTask,
    *,
    db_path: str | None = None,
    donor_email: str,
    donor_name: str,
    subject_template: str | None = None,
    body_template: str | None = None,
    locale: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    task_kwargs = {
        "db_path": db_path,
        "donor_email": donor_email,
        "donor_name": donor_name,
        "subject_template": subject_template,
        "body_template": body_template,
        "locale": locale,
        "dry_run": dry_run,
    }
    self.update_progress(20, "Preparing outreach message.", kwargs=task_kwargs)
    bot = FundingBot(db_path=db_path or DEFAULT_DB_PATH)
    try:
        sender = None if dry_run else SMTPEmailSender.from_env()
        if subject_template is None and body_template is None:
            if locale is not None:
                bot.upsert_donor(email=donor_email, name=donor_name, locale=locale)
            result = bot.send_outreach_from_template(
                bot.DEFAULT_OUTREACH_TEMPLATE,
                donor_email,
                donor_name,
                sender=sender,
            )
        else:
            resolved_subject = subject_template
            resolved_body = body_template
            if resolved_subject is None or resolved_body is None:
                fallback = bot._resolve_catalog_template(
                    bot.DEFAULT_OUTREACH_TEMPLATE,
                    segment="unknown",
                    locale=locale or bot.DEFAULT_TEMPLATE_LOCALE,
                ) or (
                    "Thank you for supporting {organization_name}",
                    "Dear {donor_name},\n\nThank you for your continued interest in {organization_name}.",
                )
                resolved_subject = resolved_subject or fallback[0]
                resolved_body = resolved_body or fallback[1]
            result = bot.send_outreach(
                donor_email=donor_email,
                donor_name=donor_name,
                subject_template=resolved_subject,
                body_template=resolved_body,
                sender=sender,
                locale=locale,
            )
        self.update_progress(90, "Outreach workflow completed.", kwargs=task_kwargs)
        return {**result, "dry_run": dry_run}
    finally:
        bot.close()


@app.task(bind=True, base=FundingBotTask, name="funding_bot.send_daily_summary")
def send_daily_summary_task(
    self: FundingBotTask,
    *,
    db_path: str | None = None,
    recipient: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    task_kwargs = {
        "db_path": db_path,
        "recipient": recipient,
        "dry_run": dry_run,
    }
    self.update_progress(25, "Building daily summary.", kwargs=task_kwargs)
    bot = FundingBot(db_path=db_path or DEFAULT_DB_PATH)
    try:
        sender = None if dry_run else SMTPEmailSender.from_env()
        summary = bot.send_daily_summary(recipient=recipient, sender=sender)
        self.update_progress(90, "Daily summary processed.", kwargs=task_kwargs)
        return {
            **summary,
            "recipient": recipient or bot.load_organization_profile().get("summary_recipient", "lupael@i4e.com.bd"),
            "dry_run": dry_run,
        }
    finally:
        bot.close()
