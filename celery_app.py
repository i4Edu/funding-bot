from __future__ import annotations

from typing import Any

from task_queue import (
    DEFAULT_BROKER_URL,
    DEFAULT_QUEUE_NAME,
    DEFAULT_RABBITMQ_BROKER_URL,
    DEFAULT_RESULT_BACKEND,
    celery_app,
    create_celery_app,
    load_queue_config,
)

DEFAULT_CELERY_BROKER_URL = DEFAULT_BROKER_URL
DEFAULT_CELERY_RESULT_BACKEND = DEFAULT_RESULT_BACKEND
DEFAULT_CELERY_QUEUE = DEFAULT_QUEUE_NAME


def get_celery_config() -> dict[str, Any]:
    config = load_queue_config()
    return {
        "broker_url": config.broker_url,
        "result_backend": config.result_backend,
        "task_always_eager": config.task_always_eager,
        "task_default_queue": config.queue_name,
        "imports": ("tasks.celery_tasks",),
        "beat_schedule": celery_app.conf.beat_schedule,
    }


def get_broker_url() -> str:
    return get_celery_config()["broker_url"]


def get_result_backend() -> str:
    return get_celery_config()["result_backend"]


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
