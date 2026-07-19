from __future__ import annotations

from task_queue import discover_opportunities_task as discover_task
from task_queue import discover_opportunities_task as run_discovery_task
from task_queue import send_daily_summary_task, send_outreach_task

__all__ = [
    "discover_task",
    "run_discovery_task",
    "send_daily_summary_task",
    "send_outreach_task",
]
