from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from funding_bot import (
    CSRNetworkConnector,
    FundingBot,
    GrantsPortalConnector,
    NGODirectoryConnector,
    QueueTaskContext,
    SMTPEmailSender,
)

DEFAULT_QUEUE_NAME = "funding-bot"
DEFAULT_BROKER_URL = "redis://redis:6379/0"
DEFAULT_RESULT_BACKEND = "redis://redis:6379/1"
DEFAULT_RABBITMQ_BROKER_URL = "amqp://" "guest:guest@rabbitmq:5672//"
DEFAULT_DAILY_SUMMARY_HOUR = 9
DEFAULT_DAILY_SUMMARY_MINUTE = 0
BROKER_ROOT = Path(os.environ.get("CELERY_FILESYSTEM_BROKER_DIR", ".celery-broker"))
BROKER_QUEUE_DIR = BROKER_ROOT / "queue"
BROKER_PROCESSED_DIR = BROKER_ROOT / "processed"
BROKER_CONTROL_DIR = BROKER_ROOT / "control"
for _directory in (BROKER_QUEUE_DIR, BROKER_PROCESSED_DIR, BROKER_CONTROL_DIR):
    _directory.mkdir(parents=True, exist_ok=True)


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass(frozen=True)
class QueueConfig:
    enable_task_queue: bool
    enable_legacy_cron: bool
    broker_url: str
    result_backend: str
    task_always_eager: bool
    queue_name: str = DEFAULT_QUEUE_NAME
    inspect_timeout_seconds: float = 1.0

    @property
    def mode(self) -> str:
        if self.enable_task_queue and self.enable_legacy_cron:
            return "hybrid"
        if self.enable_task_queue:
            return "queue"
        return "cron"

    @property
    def active_modes(self) -> list[str]:
        modes: list[str] = []
        if self.enable_legacy_cron:
            modes.append("cron")
        if self.enable_task_queue:
            modes.append("queue")
        return modes or ["cron"]


def load_queue_config() -> QueueConfig:
    return QueueConfig(
        enable_task_queue=_coerce_bool(os.environ.get("ENABLE_TASK_QUEUE"), default=False),
        enable_legacy_cron=_coerce_bool(os.environ.get("ENABLE_LEGACY_CRON"), default=True),
        broker_url=os.environ.get("CELERY_BROKER_URL", DEFAULT_BROKER_URL),
        result_backend=os.environ.get("CELERY_RESULT_BACKEND", DEFAULT_RESULT_BACKEND),
        task_always_eager=_coerce_bool(os.environ.get("CELERY_TASK_ALWAYS_EAGER"), default=False),
        queue_name=os.environ.get("CELERY_QUEUE_NAME", DEFAULT_QUEUE_NAME).strip()
        or DEFAULT_QUEUE_NAME,
        inspect_timeout_seconds=float(os.environ.get("CELERY_INSPECT_TIMEOUT_SECONDS", "1.0")),
    )


def _broker_transport_name(broker_url: str) -> str:
    parsed = urlparse(broker_url)
    return parsed.scheme or "unknown"


def _generate_idempotency_key(task_name: str, payload: dict[str, Any]) -> str:
    generator = getattr(FundingBot, "generate_idempotency_key", None)
    if callable(generator):
        return str(generator(task_name, payload))
    serialized = json.dumps({"task_name": task_name, "payload": payload}, sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class _FallbackAsyncResult:
    def __init__(self, task_name: str, payload: Any) -> None:
        self.id = f"local-{task_name}"
        self.payload = payload
        self.status = "SUCCESS"

    def ready(self) -> bool:
        return True

    def get(self, propagate: bool = True) -> Any:
        return self.payload


class _FallbackInspect:
    def __init__(self) -> None:
        self._empty: dict[str, Any] = {}

    def ping(self) -> dict[str, Any]:
        return self._empty

    def stats(self) -> dict[str, Any]:
        return self._empty

    def active(self) -> dict[str, Any]:
        return self._empty

    def reserved(self) -> dict[str, Any]:
        return self._empty

    def scheduled(self) -> dict[str, Any]:
        return self._empty


class _FallbackTask:
    def __init__(
        self,
        func: Callable[..., Any],
        *,
        name: str,
        queue: str,
        bind: bool,
    ) -> None:
        self.run = func
        self.name = name
        self.queue = queue
        self.bind = bind
        self.request = type("FallbackRequest", (), {"id": f"local-{name}", "hostname": "local"})()

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self.bind:
            return self.run(self, *args, **kwargs)
        return self.run(*args, **kwargs)

    def update_state(self, *, state: str, meta: dict[str, Any] | None = None) -> None:
        self.request.state = state
        self.request.meta = meta or {}

    def delay(self, *args: Any, **kwargs: Any) -> _FallbackAsyncResult:
        return _FallbackAsyncResult(self.name, self(*args, **kwargs))

    apply_async = delay


class _FallbackControl:
    @staticmethod
    def inspect(timeout: float | None = None) -> _FallbackInspect:
        return _FallbackInspect()


class _FallbackCelery:
    def __init__(self, name: str) -> None:
        self.main = name
        self.conf: dict[str, Any] = {}
        self.tasks: dict[str, Any] = {}
        self.control = _FallbackControl()

    def task(
        self, *decorator_args: Any, **decorator_kwargs: Any
    ) -> Callable[[Callable[..., Any]], Any]:
        name = decorator_kwargs.get("name")
        queue = decorator_kwargs.get("queue", DEFAULT_QUEUE_NAME)
        bind = bool(decorator_kwargs.get("bind", False))

        def decorator(func: Callable[..., Any]) -> _FallbackTask:
            task_name = name or f"{self.main}.{func.__name__}"
            task = _FallbackTask(func, name=task_name, queue=queue, bind=bind)
            self.tasks[task_name] = task
            return task

        if decorator_args and callable(decorator_args[0]):
            return decorator(decorator_args[0])
        return decorator


def create_celery_app(config: QueueConfig | None = None) -> Any:
    queue_config = config or load_queue_config()
    try:
        from celery import Celery
        from celery.schedules import crontab
    except ImportError:
        celery_app = _FallbackCelery("funding_bot")
        celery_app.conf.update(
            broker_url=queue_config.broker_url,
            result_backend=queue_config.result_backend,
            task_always_eager=queue_config.task_always_eager,
            task_default_queue=queue_config.queue_name,
            imports=("tasks.celery_tasks",),
        )
        return celery_app

    celery_app = Celery(
        "funding_bot",
        broker=queue_config.broker_url,
        backend=queue_config.result_backend,
    )
    celery_app.conf.update(
        accept_content=["json"],
        broker_connection_retry_on_startup=True,
        imports=("tasks.celery_tasks",),
        result_extended=True,
        result_serializer="json",
        task_always_eager=queue_config.task_always_eager,
        task_default_queue=queue_config.queue_name,
        task_ignore_result=False,
        task_serializer="json",
        task_track_started=True,
        timezone="UTC",
        enable_utc=True,
    )
    if queue_config.broker_url.startswith("filesystem://"):
        celery_app.conf.broker_transport_options = {
            "data_folder_in": str(BROKER_QUEUE_DIR),
            "data_folder_out": str(BROKER_QUEUE_DIR),
            "data_folder_processed": str(BROKER_PROCESSED_DIR),
            "control_folder": str(BROKER_CONTROL_DIR),
        }
    celery_app.conf.beat_schedule = {
        "daily-summary": {
            "task": "funding_bot.send_daily_summary",
            "schedule": crontab(
                minute=int(
                    os.environ.get(
                        "DAILY_SUMMARY_SCHEDULE_MINUTE", str(DEFAULT_DAILY_SUMMARY_MINUTE)
                    )
                ),
                hour=int(
                    os.environ.get("DAILY_SUMMARY_SCHEDULE_HOUR", str(DEFAULT_DAILY_SUMMARY_HOUR))
                ),
            ),
            "kwargs": {
                "recipient": os.environ.get("DAILY_SUMMARY_RECIPIENT", "lupael@i4e.com.bd"),
                "dry_run": _coerce_bool(os.environ.get("DAILY_SUMMARY_DRY_RUN"), default=False),
                "db_path": os.environ.get("BOT_DB_PATH", "funding_bot.db"),
            },
        }
    }
    return celery_app


celery_app = create_celery_app()


def _with_bot(db_path: str | None, callback: Callable[[FundingBot], Any]) -> Any:
    bot = FundingBot(db_path=db_path or os.environ.get("BOT_DB_PATH", "funding_bot.db"))
    try:
        return callback(bot)
    finally:
        bot.close()


def _queue_result_payload(
    task_run: dict[str, Any], *, mode: str = "queue", **extra: Any
) -> dict[str, Any]:
    payload = dict(task_run.get("result") or {})
    payload.update(extra)
    payload["mode"] = mode
    payload["duplicate"] = bool(task_run.get("duplicate"))
    payload["idempotency_key"] = task_run.get("idempotency_key")
    payload["task_run"] = task_run
    return payload


def _current_worker_hostname() -> str | None:
    try:
        from celery import current_task
    except ImportError:
        return None
    request = getattr(current_task, "request", None)
    return getattr(request, "hostname", None)


@celery_app.task(name="funding_bot.discover", queue=load_queue_config().queue_name)
def discover_opportunities_task(
    *,
    keywords: list[str] | None = None,
    trusted_sources: list[str] | None = None,
    db_path: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    def _run(bot: FundingBot) -> dict[str, Any]:
        payload = {
            "keywords": keywords or [],
            "trusted_sources": trusted_sources or [],
        }

        def _task(context: QueueTaskContext, task_payload: dict[str, Any]) -> dict[str, Any]:
            context.update_progress(20, "Loading discovery configuration.")
            context.checkpoint("Shutdown requested before discovery started.")
            found = context.bot.run_discovery(
                connectors=[
                    GrantsPortalConnector(),
                    CSRNetworkConnector(),
                    NGODirectoryConnector(),
                ],
                keywords=task_payload.get("keywords") or None,
                trusted_sources=task_payload.get("trusted_sources") or None,
            )
            context.update_progress(
                90, "Persisted discovery results.", callback_payload={"count": len(found)}
            )
            context.checkpoint("Shutdown requested after discovery completed.")
            return {
                "count": len(found),
                "new_opportunities": found,
            }

        task_run = bot.execute_queue_task(
            "discover_opportunities",
            payload,
            _task,
            idempotency_key=idempotency_key,
            worker_id=_current_worker_hostname(),
        )
        return _queue_result_payload(task_run)

    return _with_bot(db_path, _run)


@celery_app.task(name="funding_bot.send_outreach", queue=load_queue_config().queue_name)
def send_outreach_task(
    *,
    donor_email: str,
    donor_name: str,
    template_name: str = FundingBot.DEFAULT_OUTREACH_TEMPLATE,
    subject_template: str | None = None,
    body_template: str | None = None,
    locale: str | None = None,
    dry_run: bool = True,
    db_path: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    def _run(bot: FundingBot) -> dict[str, Any]:
        payload = {
            "donor_email": donor_email,
            "donor_name": donor_name,
            "template_name": template_name,
            "subject_template": subject_template,
            "body_template": body_template,
            "locale": locale,
            "dry_run": dry_run,
        }

        def _task(context: QueueTaskContext, task_payload: dict[str, Any]) -> dict[str, Any]:
            context.update_progress(20, "Preparing outreach message.")
            context.checkpoint("Shutdown requested before donor outreach started.")
            sender = None if task_payload.get("dry_run", True) else SMTPEmailSender.from_env()
            resolved_locale = task_payload.get("locale")
            if (
                task_payload.get("subject_template") is None
                and task_payload.get("body_template") is None
            ):
                if resolved_locale is not None:
                    context.bot.upsert_donor(
                        email=str(task_payload["donor_email"]),
                        name=str(task_payload["donor_name"]),
                        locale=str(resolved_locale),
                    )
                result = context.bot.send_outreach_from_template(
                    str(task_payload.get("template_name") or context.bot.DEFAULT_OUTREACH_TEMPLATE),
                    str(task_payload["donor_email"]),
                    str(task_payload["donor_name"]),
                    sender=sender,
                    locale=str(resolved_locale) if resolved_locale is not None else None,
                )
            else:
                fallback = context.bot._resolve_catalog_template(
                    str(task_payload.get("template_name") or context.bot.DEFAULT_OUTREACH_TEMPLATE),
                    segment="unknown",
                    locale=str(resolved_locale or context.bot.DEFAULT_TEMPLATE_LOCALE),
                ) or (
                    "Thank you for supporting {organization_name}",
                    "Dear {donor_name},\n\nThank you for your continued interest in {organization_name}.",
                )
                result = context.bot.send_outreach(
                    donor_email=str(task_payload["donor_email"]),
                    donor_name=str(task_payload["donor_name"]),
                    subject_template=str(task_payload.get("subject_template") or fallback[0]),
                    body_template=str(task_payload.get("body_template") or fallback[1]),
                    sender=sender,
                    locale=str(resolved_locale) if resolved_locale is not None else None,
                )
            context.update_progress(90, "Outreach workflow completed.")
            context.checkpoint("Shutdown requested after donor outreach completed.")
            return result

        task_run = bot.execute_queue_task(
            "send_outreach",
            payload,
            _task,
            idempotency_key=idempotency_key,
            worker_id=_current_worker_hostname(),
        )
        return _queue_result_payload(task_run, dry_run=dry_run)

    return _with_bot(db_path, _run)


@celery_app.task(name="funding_bot.send_daily_summary", queue=load_queue_config().queue_name)
def send_daily_summary_task(
    *,
    recipient: str = "lupael@i4e.com.bd",
    dry_run: bool = False,
    db_path: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    def _run(bot: FundingBot) -> dict[str, Any]:
        payload = {"recipient": recipient, "dry_run": dry_run}

        def _task(context: QueueTaskContext, task_payload: dict[str, Any]) -> dict[str, Any]:
            context.update_progress(25, "Building daily summary.")
            context.checkpoint("Shutdown requested before daily summary started.")
            sender = None if task_payload.get("dry_run", False) else SMTPEmailSender.from_env()
            if sender is not None and not callable(sender):
                sender = lambda *_args, **_kwargs: None
            result = context.bot.send_daily_summary(
                recipient=str(task_payload["recipient"]),
                sender=sender,
            )
            context.update_progress(90, "Daily summary workflow completed.")
            context.checkpoint("Shutdown requested after daily summary completed.")
            return result

        task_run = bot.execute_queue_task(
            "send_daily_summary",
            payload,
            _task,
            idempotency_key=idempotency_key,
            worker_id=_current_worker_hostname(),
        )
        return _queue_result_payload(task_run, recipient=recipient, dry_run=dry_run)

    return _with_bot(db_path, _run)


TASK_DEFINITIONS = {
    "discover": {
        "task_name": discover_opportunities_task.name,
        "queue": load_queue_config().queue_name,
        "legacy_command": "python -m funding_bot discover",
        "task": discover_opportunities_task,
    },
    "outreach": {
        "task_name": send_outreach_task.name,
        "queue": load_queue_config().queue_name,
        "legacy_command": "python -m funding_bot send-outreach",
        "task": send_outreach_task,
    },
    "daily-summary": {
        "task_name": send_daily_summary_task.name,
        "queue": load_queue_config().queue_name,
        "legacy_command": "python -m funding_bot send-daily-summary",
        "task": send_daily_summary_task,
    },
}


def dispatch_discovery(
    *,
    keywords: list[str] | None = None,
    trusted_sources: list[str] | None = None,
    db_path: str | None = None,
) -> tuple[int, dict[str, Any]]:
    config = load_queue_config()
    payload = {
        "keywords": keywords or [],
        "trusted_sources": trusted_sources or [],
    }
    idempotency_key = _generate_idempotency_key("discover_opportunities", payload)
    if config.enable_task_queue:
        result = discover_opportunities_task.delay(
            keywords=keywords,
            trusted_sources=trusted_sources,
            db_path=db_path,
            idempotency_key=idempotency_key,
        )
        return 202, {
            "mode": config.mode,
            "task_id": getattr(result, "id", None),
            "task_name": discover_opportunities_task.name,
            "idempotency_key": idempotency_key,
            "legacy_cron_enabled": config.enable_legacy_cron,
        }

    payload = _run_discovery_inline(
        keywords=keywords,
        trusted_sources=trusted_sources,
        db_path=db_path,
        idempotency_key=idempotency_key,
    )
    payload["mode"] = config.mode
    payload["legacy_cron_enabled"] = config.enable_legacy_cron
    return 200, payload


def _run_discovery_inline(
    *,
    keywords: list[str] | None = None,
    trusted_sources: list[str] | None = None,
    db_path: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    return discover_opportunities_task(
        keywords=keywords,
        trusted_sources=trusted_sources,
        db_path=db_path,
        idempotency_key=idempotency_key,
    )


def _safe_inspect_call(inspector: Any, method_name: str) -> dict[str, Any]:
    if inspector is None:
        return {}
    method = getattr(inspector, method_name, None)
    if method is None:
        return {}
    try:
        payload = method()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _count_entries(worker_payload: dict[str, Any]) -> int:
    total = 0
    for value in worker_payload.values():
        if isinstance(value, list):
            total += len(value)
        elif isinstance(value, dict):
            total += _count_entries(value)
    return total


def get_queue_status(
    *,
    config: QueueConfig | None = None,
    app: Any | None = None,
    inspector: Any | None = None,
) -> dict[str, Any]:
    queue_config = config or load_queue_config()
    status = {
        "queue_enabled": queue_config.enable_task_queue,
        "legacy_cron_enabled": queue_config.enable_legacy_cron,
        "mode": queue_config.mode,
        "active_modes": queue_config.active_modes,
        "broker_transport": _broker_transport_name(queue_config.broker_url),
        "queue_name": queue_config.queue_name,
        "queue_depth": 0,
        "active_tasks": 0,
        "reserved_tasks": 0,
        "scheduled_tasks": 0,
        "worker_count": 0,
        "workers": [],
        "worker_status": "disabled",
    }
    if not queue_config.enable_task_queue:
        return status

    celery_instance = app or celery_app
    inspect_client = inspector
    if inspect_client is None:
        try:
            inspect_client = celery_instance.control.inspect(
                timeout=queue_config.inspect_timeout_seconds
            )
        except Exception:
            inspect_client = None

    ping = _safe_inspect_call(inspect_client, "ping")
    stats = _safe_inspect_call(inspect_client, "stats")
    active = _safe_inspect_call(inspect_client, "active")
    reserved = _safe_inspect_call(inspect_client, "reserved")
    scheduled = _safe_inspect_call(inspect_client, "scheduled")

    worker_names = sorted(set(ping) | set(stats) | set(active) | set(reserved) | set(scheduled))
    active_count = _count_entries(active)
    reserved_count = _count_entries(reserved)
    scheduled_count = _count_entries(scheduled)

    status.update(
        queue_depth=reserved_count + scheduled_count,
        active_tasks=active_count,
        reserved_tasks=reserved_count,
        scheduled_tasks=scheduled_count,
        worker_count=len(worker_names),
        workers=worker_names,
        worker_status="healthy" if worker_names else "degraded",
    )
    return status
