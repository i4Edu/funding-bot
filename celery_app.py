from __future__ import annotations

import os
from typing import Any

from celery import Celery

DEFAULT_CELERY_BROKER_URL = "redis://redis:6379/0"
DEFAULT_CELERY_RESULT_BACKEND = "redis://redis:6379/1"
DEFAULT_CELERY_QUEUE = "funding-bot"
DEFAULT_RABBITMQ_BROKER_URL = "******rabbitmq:5672//"


def _env_flag(name: str, *, default: bool) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def get_broker_url() -> str:
    return (
        os.environ.get("CELERY_BROKER_URL")
        or os.environ.get("BROKER_URL")
        or DEFAULT_CELERY_BROKER_URL
    )


def get_result_backend() -> str:
    return os.environ.get("CELERY_RESULT_BACKEND") or DEFAULT_CELERY_RESULT_BACKEND


def get_celery_config() -> dict[str, Any]:
    return {
        "broker_url": get_broker_url(),
        "result_backend": get_result_backend(),
        "task_default_queue": os.environ.get("CELERY_DEFAULT_QUEUE", DEFAULT_CELERY_QUEUE),
        "accept_content": ["json"],
        "task_serializer": "json",
        "result_serializer": "json",
        "timezone": os.environ.get("CELERY_TIMEZONE", "UTC"),
        "enable_utc": True,
        "task_track_started": True,
        "result_extended": True,
        "broker_connection_retry_on_startup": True,
        "task_always_eager": _env_flag("CELERY_TASK_ALWAYS_EAGER", default=False),
        "task_eager_propagates": _env_flag("CELERY_TASK_EAGER_PROPAGATES", default=True),
        "imports": ("tasks.celery_tasks",),
    }


def create_celery_app() -> Celery:
    app = Celery("funding-bot")
    app.conf.update(get_celery_config())
    return app


celery_app = create_celery_app()

__all__ = [
    "DEFAULT_CELERY_BROKER_URL",
    "DEFAULT_CELERY_RESULT_BACKEND",
    "DEFAULT_CELERY_QUEUE",
    "DEFAULT_RABBITMQ_BROKER_URL",
    "celery_app",
    "create_celery_app",
    "get_broker_url",
    "get_celery_config",
    "get_result_backend",
]
