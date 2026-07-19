from __future__ import annotations

import base64
import binascii
import hmac
import importlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from flask import Flask, Response, g, jsonify, redirect, render_template, request, session, url_for

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from funding_bot import (  # noqa: E402
    DEFAULT_LOCALE_CODE,
    DuplicateSubmissionError,
    FundingBot,
    FundingBotError,
    OpportunityNotFoundError,
    SMTPEmailSender,
    TaskCommentNotFoundError,
    TaskNotFoundError,
    TaskTransitionError,
    _validate_email,
    default_connectors,
)
from task_queue import dispatch_discovery, get_queue_status, load_queue_config  # noqa: E402

app = Flask(__name__, template_folder=str(Path(__file__).resolve().parent / "templates"))
app.config["JSON_SORT_KEYS"] = False
MAX_FEEDBACK_MESSAGE_LENGTH = 2000
DEFAULT_CELERY_HEALTH_TIMEOUT_SECONDS = 2.0
TASK_SORT_OPTIONS = (
    ("updated_at", "Recently updated"),
    ("-updated_at", "Least recently updated"),
    ("assignee", "Assignee (A-Z)"),
    ("-assignee", "Assignee (Z-A)"),
    ("status", "Status (A-Z)"),
    ("-status", "Status (Z-A)"),
    ("due_date", "Due date (earliest first)"),
    ("-due_date", "Due date (latest first)"),
)

ROLE_PASSWORD_ENV_VARS = {
    "admin": "ADMIN_PASSWORD",
    "staff": "STAFF_PASSWORD",
    "auditor": "AUDITOR_PASSWORD",
}
DEFAULT_SESSION_TIMEOUT_MINUTES = 30
SESSION_ROLE_KEY = "authenticated_role"
SESSION_AUTHENTICATED_AT_KEY = "authenticated_at"
SESSION_LAST_SEEN_AT_KEY = "last_seen_at"

# Track server start time for the uptime metric.
_APP_START_TIME = time.time()


def _env_flag(name: str, *, default: bool) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _read_session_timeout_minutes() -> int:
    raw_value = os.environ.get("DASHBOARD_SESSION_TIMEOUT_MINUTES", str(DEFAULT_SESSION_TIMEOUT_MINUTES))
    try:
        parsed = int(raw_value)
    except ValueError:
        return DEFAULT_SESSION_TIMEOUT_MINUTES
    return max(1, parsed)


def _configure_session_security(flask_app: Flask) -> None:
    flask_app.config["SECRET_KEY"] = os.environ.get(
        "FLASK_SECRET_KEY",
        os.environ.get("SECRET_KEY", "development-only-change-me"),
    )
    flask_app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(
        minutes=_read_session_timeout_minutes()
    )
    flask_app.config["SESSION_COOKIE_HTTPONLY"] = True
    flask_app.config["SESSION_COOKIE_SECURE"] = _env_flag(
        "SESSION_COOKIE_SECURE",
        default=True,
    )
    flask_app.config["SESSION_COOKIE_SAMESITE"] = os.environ.get(
        "SESSION_COOKIE_SAMESITE",
        "Lax",
    )
    flask_app.config["SESSION_REFRESH_EACH_REQUEST"] = True


_configure_session_security(app)


def _bot() -> FundingBot:
    bot = g.get("_bot")
    if bot is None:
        bot = FundingBot(db_path=os.environ.get("BOT_DB_PATH", "funding_bot.db"))
        g._bot = bot
    return bot


def _json_error(message: str, status_code: int, *, headers: dict[str, str] | None = None) -> Response:
    response = jsonify({"error": message})
    response.status_code = status_code
    if headers:
        response.headers.update(headers)
    return response


def _auth_challenge(message: str = "Authentication required") -> Response:
    return _json_error(
        message,
        401,
        headers={"WWW-Authenticate": 'Basic realm="Funding Bot Dashboard"'},
    )


def _session_timeout() -> timedelta:
    configured = app.config.get("PERMANENT_SESSION_LIFETIME", timedelta(minutes=DEFAULT_SESSION_TIMEOUT_MINUTES))
    return configured if isinstance(configured, timedelta) else timedelta(minutes=DEFAULT_SESSION_TIMEOUT_MINUTES)


def _parse_session_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _clear_authenticated_session() -> None:
    session.pop(SESSION_ROLE_KEY, None)
    session.pop(SESSION_AUTHENTICATED_AT_KEY, None)
    session.pop(SESSION_LAST_SEEN_AT_KEY, None)


def _establish_authenticated_session(role: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    session.permanent = True
    session[SESSION_ROLE_KEY] = role
    session.setdefault(SESSION_AUTHENTICATED_AT_KEY, now)
    session[SESSION_LAST_SEEN_AT_KEY] = now


def _get_session_role() -> str | None:
    role = session.get(SESSION_ROLE_KEY)
    if not isinstance(role, str) or role not in ROLE_PASSWORD_ENV_VARS:
        _clear_authenticated_session()
        return None
    last_seen = _parse_session_timestamp(session.get(SESSION_LAST_SEEN_AT_KEY))
    if last_seen is None:
        _clear_authenticated_session()
        return None
    if datetime.now(timezone.utc) - last_seen > _session_timeout():
        _clear_authenticated_session()
        return None
    _establish_authenticated_session(role)
    return role


def _get_authenticated_role() -> str:
    session_role = _get_session_role()
    if session_role is not None:
        return session_role

    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        raise PermissionError("Authentication required")

    token = header.split(" ", 1)[1].strip()
    try:
        decoded = base64.b64decode(token, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise PermissionError("Invalid authentication credentials") from exc

    username, separator, password = decoded.partition(":")
    if not separator:
        raise PermissionError("Invalid authentication credentials")

    role = username.strip().lower()
    if role not in ROLE_PASSWORD_ENV_VARS:
        raise PermissionError("Invalid authentication credentials")

    expected_password = os.environ.get(ROLE_PASSWORD_ENV_VARS[role])
    if not expected_password or not hmac.compare_digest(password, expected_password):
        raise PermissionError("Invalid authentication credentials")

    _establish_authenticated_session(role)
    return role


def require_role(*roles: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    allowed_roles = {role.lower() for role in roles}

    def decorator(view_func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view_func)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            try:
                role = _get_authenticated_role()
            except PermissionError:
                return _auth_challenge()

            if role not in allowed_roles:
                return _json_error("Forbidden", 403)

            g.current_role = role
            return view_func(*args, **kwargs)

        return wrapped

    return decorator


def _parse_json_column(value: str | None) -> Any:
    if not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _coerce_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise ValueError(f"Field '{field_name}' must be a boolean.")


def _coerce_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ValueError(f"Field '{field_name}' must be a list or comma-separated string.")


def _serialize_opportunity(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["raw_data"] = _parse_json_column(data.pop("raw_data_json", "{}"))
    return data


def _serialize_donor(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["opted_out"] = bool(data["opted_out"])
    data["preferences"] = _parse_json_column(data.pop("preferences_json", "{}"))
    data["field_classifications"] = _parse_json_column(
        data.pop("field_classifications_json", "{}")
    )
    return data


def _serialize_audit_log(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["details"] = _parse_json_column(data.pop("details_json", "{}"))
    return data


def _serialize_application(row: Any) -> dict[str, Any]:
    return dict(row)


def _serialize_submission_attempt(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["succeeded"] = bool(data["succeeded"])
    return data


def _serialize_task(row: Any) -> dict[str, Any]:
    return dict(row)


def _serialize_task_comment(row: Any) -> dict[str, Any]:
    return dict(row)


def _task_assignment_sender() -> Any | None:
    return SMTPEmailSender.from_env() if SMTPEmailSender.is_configured() else None


def _read_task_import_csv() -> str:
    upload = request.files.get("file")
    if upload is not None:
        return upload.stream.read().decode("utf-8-sig")
    return request.get_data(cache=False, as_text=True)


def _task_scope_for_role(role: str | None) -> str | None:
    if role in {"admin", "auditor"}:
        return None
    return role


def _group_tasks_by_status(tasks: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped = {status: [] for status in FundingBot.TASK_STATUSES}
    for task in tasks:
        grouped.setdefault(task["status"], []).append(task)
    return grouped


def _can_move_task(role: str | None, task: dict[str, Any]) -> bool:
    return role == "admin" or task.get("assigned_to") == role


def _serialize_translation_review(review: Any) -> dict[str, Any]:
    data = dict(review)
    if "locale_metadata" not in data:
        data["locale_metadata"] = _bot().get_locale_definition(data.get("locale"))
    return data


def _fetch_opportunity(signature: str) -> dict[str, Any]:
    row = _bot().connection.execute(
        "SELECT * FROM opportunities WHERE signature = ?",
        (signature,),
    ).fetchone()
    if not row:
        raise OpportunityNotFoundError(f"Unknown opportunity {signature!r}.")
    return _serialize_opportunity(row)


def _fetch_donor(email: str) -> dict[str, Any] | None:
    return _bot().get_donor(email)


def _get_request_json() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object request body.")
    return payload


def _normalize_optional_query_value(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _task_filter_args() -> tuple[dict[str, str | None], bool]:
    current_role = getattr(g, "current_role", None)
    requested_assignee = _normalize_optional_query_value(request.args.get("assignee"))
    normalized_assignee = requested_assignee.lower() if requested_assignee else None
    if current_role not in {"admin", "auditor"}:
        if normalized_assignee and normalized_assignee != current_role:
            return {}, True
        normalized_assignee = current_role

    status = _normalize_optional_query_value(request.args.get("status"))
    due_date_before = _normalize_optional_query_value(request.args.get("due_date_before"))
    due_date_after = _normalize_optional_query_value(request.args.get("due_date_after"))
    sort = _normalize_optional_query_value(request.args.get("sort")) or "updated_at"
    return {
        "assignee": normalized_assignee,
        "status": status,
        "due_date_before": due_date_before,
        "due_date_after": due_date_after,
        "sort": sort,
    }, False


def _task_status_counts(tasks: list[dict[str, Any]]) -> dict[str, int]:
    counts = {status: 0 for status in FundingBot.TASK_STATUSES}
    for task in tasks:
        status = str(task.get("status", ""))
        if status in counts:
            counts[status] += 1
    return counts


def _task_assignee_options() -> list[str]:
    rows = _bot().connection.execute(
        "SELECT DISTINCT assigned_to FROM tasks WHERE assigned_to != '' ORDER BY assigned_to COLLATE NOCASE ASC"
    ).fetchall()
    return [str(row["assigned_to"]) for row in rows]


def _resolve_ui_locale() -> dict[str, Any]:
    requested_locale = request.args.get("locale", DEFAULT_LOCALE_CODE)
    try:
        return _bot().get_locale_definition(requested_locale)
    except ValueError:
        return _bot().get_locale_definition(DEFAULT_LOCALE_CODE)


def _dashboard_context() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    recent_cutoff = (now - timedelta(days=7)).isoformat()
    current_role = getattr(g, "current_role", None)
    task_scope = _task_scope_for_role(current_role)

    new_opportunities_count = _bot().connection.execute(
        "SELECT COUNT(*) FROM opportunities WHERE discovered_at >= ?",
        (recent_cutoff,),
    ).fetchone()[0]
    applications_submitted_count = _bot().connection.execute(
        "SELECT COUNT(*) FROM applications",
    ).fetchone()[0]
    pending_applications_count = _bot().connection.execute(
        "SELECT COUNT(*) FROM applications WHERE status IN ('pending', 'submitted', 'in_review')",
    ).fetchone()[0]
    donor_communications_count = _bot().connection.execute(
        "SELECT COUNT(*) FROM communications",
    ).fetchone()[0]
    pending_translation_reviews_count = _bot().connection.execute(
        "SELECT COUNT(*) FROM translation_reviews WHERE status = 'pending'",
    ).fetchone()[0]

    recent_opportunities = [
        _serialize_opportunity(row)
        for row in _bot().connection.execute(
            "SELECT * FROM opportunities ORDER BY discovered_at DESC LIMIT 10"
        ).fetchall()
    ]
    recent_applications = [
        _serialize_application(row)
        for row in _bot().connection.execute(
            """
            SELECT
                applications.opportunity_signature,
                opportunities.title,
                applications.donor_name,
                applications.status,
                applications.next_action,
                applications.submission_reference,
                applications.submitted_at
            FROM applications
            JOIN opportunities
                ON opportunities.signature = applications.opportunity_signature
            ORDER BY applications.submitted_at DESC
            LIMIT 10
            """
        ).fetchall()
    ]
    my_task_counts = _bot().get_task_status_counts(assigned_to=task_scope) if current_role else {}
    overdue_tasks = [
        _serialize_task(task)
        for task in _bot().list_tasks(
            assigned_to=task_scope,
            due_date_before=now.date().isoformat(),
            sort="due_date",
        )
        if task.get("is_overdue")
    ][:5]

    return {
        "current_role": current_role,
        "new_opportunities_count": new_opportunities_count,
        "applications_submitted_count": applications_submitted_count,
        "pending_applications_count": pending_applications_count,
        "donor_communications_count": donor_communications_count,
        "pending_translation_reviews_count": pending_translation_reviews_count,
        "my_tasks_count": sum(my_task_counts.values()),
        "my_open_tasks_count": sum(
            count for status, count in my_task_counts.items() if status != "done"
        ),
        "overdue_tasks": overdue_tasks,
        "overdue_tasks_count": len(overdue_tasks),
        "recent_opportunities": recent_opportunities,
        "recent_applications": recent_applications,
        "ui_locale": _resolve_ui_locale(),
    }


def _task_dashboard_context(filters: dict[str, str | None]) -> dict[str, Any]:
    current_role = getattr(g, "current_role", None)
    tasks = [
        _serialize_task(task)
        for task in _bot().list_tasks(
            assigned_to=filters["assignee"],
            status=filters["status"],
            due_date_before=filters["due_date_before"],
            due_date_after=filters["due_date_after"],
            sort=filters["sort"],
        )
    ]
    for task in tasks:
        task["can_move"] = _can_move_task(current_role, task)
    counts = _task_status_counts(tasks)
    return {
        "current_role": current_role,
        "tasks": tasks,
        "task_columns": _group_tasks_by_status(tasks),
        "task_counts": counts,
        "total_tasks": len(tasks),
        "task_filters": filters,
        "task_sort_options": TASK_SORT_OPTIONS,
        "task_assignee_options": _task_assignee_options(),
        "can_filter_all_assignees": current_role in {"admin", "auditor"},
        "can_reassign_tasks": current_role == "admin",
        "ui_locale": _resolve_ui_locale(),
    }


def _queue_health_timeout_seconds() -> float:
    configured = os.environ.get(
        "CELERY_HEALTH_TIMEOUT_SECONDS",
        os.environ.get("CELERY_INSPECT_TIMEOUT_SECONDS", str(DEFAULT_CELERY_HEALTH_TIMEOUT_SECONDS)),
    )
    try:
        timeout = float(configured)
    except ValueError:
        return DEFAULT_CELERY_HEALTH_TIMEOUT_SECONDS
    return max(timeout, 0.1)


def _count_tasks_by_worker(task_map: Any) -> int:
    if not isinstance(task_map, dict):
        return 0
    return sum(len(tasks) for tasks in task_map.values() if isinstance(tasks, list))


def _create_celery_health_app() -> Any:
    queue_config = load_queue_config()
    broker_url = queue_config.broker_url
    if not broker_url:
        raise RuntimeError("CELERY_BROKER_URL is not configured.")

    celery_module = importlib.import_module("celery")
    return celery_module.Celery(
        "funding-bot-health",
        broker=broker_url,
        backend=queue_config.result_backend or None,
    )


def _fetch_celery_queue_snapshot() -> dict[str, Any]:
    queue_name = load_queue_config().queue_name
    timeout_seconds = _queue_health_timeout_seconds()
    celery_app = _create_celery_health_app()
    inspect = celery_app.control.inspect(timeout=timeout_seconds)

    active = inspect.active() or {}
    reserved = inspect.reserved() or {}
    scheduled = inspect.scheduled() or {}
    stats = inspect.stats() or {}
    ping = inspect.ping() or {}

    worker_names = sorted({*active.keys(), *reserved.keys(), *scheduled.keys(), *stats.keys(), *ping.keys()})
    workers = []
    for worker_name in worker_names:
        worker_active = active.get(worker_name, []) if isinstance(active, dict) else []
        worker_reserved = reserved.get(worker_name, []) if isinstance(reserved, dict) else []
        worker_scheduled = scheduled.get(worker_name, []) if isinstance(scheduled, dict) else []
        workers.append(
            {
                "name": worker_name,
                "status": "online" if worker_name in ping else "unreachable",
                "active_tasks": len(worker_active) if isinstance(worker_active, list) else 0,
                "reserved_tasks": len(worker_reserved) if isinstance(worker_reserved, list) else 0,
                "scheduled_tasks": len(worker_scheduled) if isinstance(worker_scheduled, list) else 0,
            }
        )

    queue_depth = 0
    with celery_app.connection_for_read() as connection:
        queue = connection.SimpleQueue(queue_name)
        try:
            queue_depth = int(queue.qsize() or 0)
        finally:
            queue.close()

    return {
        "status": "ok",
        "queue_name": queue_name,
        "broker_reachable": True,
        "timeout_seconds": timeout_seconds,
        "active_tasks": _count_tasks_by_worker(active),
        "pending_tasks": queue_depth,
        "queue_depth": queue_depth,
        "worker_count": len(worker_names),
        "workers": workers,
    }


def _get_queue_health_snapshot() -> dict[str, Any]:
    queue_config = load_queue_config()
    queue_name = queue_config.queue_name
    timeout_seconds = _queue_health_timeout_seconds()
    if not queue_config.enable_task_queue:
        return {
            "status": "disabled",
            "mode": queue_config.mode,
            "active_modes": queue_config.active_modes,
            "queue_enabled": queue_config.enable_task_queue,
            "legacy_cron_enabled": queue_config.enable_legacy_cron,
            "queue_name": queue_name,
            "broker_reachable": False,
            "timeout_seconds": timeout_seconds,
            "active_tasks": 0,
            "pending_tasks": 0,
            "queue_depth": 0,
            "worker_count": 0,
            "workers": [],
            "error": "Queue monitoring is disabled because ENABLE_TASK_QUEUE is not enabled.",
        }

    try:
        snapshot = _fetch_celery_queue_snapshot()
        snapshot.update(
            {
                "mode": queue_config.mode,
                "active_modes": queue_config.active_modes,
                "queue_enabled": queue_config.enable_task_queue,
                "legacy_cron_enabled": queue_config.enable_legacy_cron,
            }
        )
        return snapshot
    except TimeoutError as exc:
        return {
            "status": "degraded",
            "mode": queue_config.mode,
            "active_modes": queue_config.active_modes,
            "queue_enabled": queue_config.enable_task_queue,
            "legacy_cron_enabled": queue_config.enable_legacy_cron,
            "queue_name": queue_name,
            "broker_reachable": False,
            "timeout_seconds": timeout_seconds,
            "active_tasks": 0,
            "pending_tasks": 0,
            "queue_depth": 0,
            "worker_count": 0,
            "workers": [],
            "error": f"Timed out while contacting the Celery broker: {exc}",
        }
    except Exception as exc:
        fallback_status = get_queue_status(config=queue_config)
        return {
            "status": "degraded",
            "mode": queue_config.mode,
            "active_modes": queue_config.active_modes,
            "queue_enabled": queue_config.enable_task_queue,
            "legacy_cron_enabled": queue_config.enable_legacy_cron,
            "queue_name": queue_name,
            "broker_reachable": fallback_status["worker_count"] > 0,
            "timeout_seconds": timeout_seconds,
            "active_tasks": fallback_status["active_tasks"],
            "pending_tasks": fallback_status["queue_depth"],
            "queue_depth": fallback_status["queue_depth"],
            "worker_count": fallback_status["worker_count"],
            "workers": fallback_status["workers"],
            "error": f"Unable to query Celery queue health: {exc}",
        }


@app.errorhandler(400)
def handle_bad_request(_: Any) -> Response:
    return _json_error("Bad request", 400)


@app.errorhandler(401)
def handle_unauthorized(_: Any) -> Response:
    return _auth_challenge()


@app.errorhandler(403)
def handle_forbidden(_: Any) -> Response:
    return _json_error("Forbidden", 403)


@app.errorhandler(404)
def handle_not_found(_: Any) -> Response:
    return _json_error("Not found", 404)


@app.errorhandler(DuplicateSubmissionError)
def handle_duplicate_submission(exc: DuplicateSubmissionError) -> Response:
    return _json_error(str(exc), 400)


@app.errorhandler(OpportunityNotFoundError)
def handle_opportunity_not_found(exc: OpportunityNotFoundError) -> Response:
    return _json_error(str(exc), 404)


@app.errorhandler(TaskNotFoundError)
def handle_task_not_found(exc: TaskNotFoundError) -> Response:
    return _json_error(str(exc), 404)


@app.errorhandler(TaskCommentNotFoundError)
def handle_task_comment_not_found(exc: TaskCommentNotFoundError) -> Response:
    return _json_error(str(exc), 404)


@app.errorhandler(FundingBotError)
def handle_funding_bot_error(exc: FundingBotError) -> Response:
    return _json_error(str(exc), 400)


@app.errorhandler(TaskTransitionError)
def handle_task_transition_error(exc: TaskTransitionError) -> Response:
    return _json_error(str(exc), 400)


@app.errorhandler(ValueError)
def handle_value_error(exc: ValueError) -> Response:
    return _json_error(str(exc), 400)


@app.errorhandler(Exception)
def handle_unexpected_error(exc: Exception) -> Response:
    # Do not expose internal details (stack traces, db paths, etc.) to clients.
    app.logger.exception("Unhandled exception: %s", exc)
    return _json_error("Internal server error", 500)


@app.teardown_appcontext
def close_bot(_: Any) -> None:
    bot = g.pop("_bot", None)
    if bot is not None:
        bot.close()


@app.get("/")
def index() -> Response:
    return redirect(url_for("dashboard"))


@app.get("/dashboard")
@require_role("staff", "admin", "auditor")
def dashboard() -> str:
    return render_template("dashboard.html", **_dashboard_context())


@app.get("/dashboard/tasks")
@require_role("staff", "admin", "auditor")
def dashboard_tasks() -> Response | str:
    filters, forbidden = _task_filter_args()
    if forbidden:
        return _json_error("Forbidden", 403)
    return render_template("tasks.html", **_task_dashboard_context(filters))

@app.get("/opportunities")
@require_role("staff", "admin", "auditor")
def list_opportunities() -> Response:
    opportunities = [_serialize_opportunity(row) for row in _bot().connection.execute(
        "SELECT * FROM opportunities ORDER BY discovered_at DESC"
    ).fetchall()]
    return jsonify(opportunities)


@app.get("/opportunities/<signature>")
@require_role("staff", "admin", "auditor")
def get_opportunity(signature: str) -> Response:
    opportunity = _fetch_opportunity(signature)
    application_row = _bot().connection.execute(
        "SELECT * FROM applications WHERE opportunity_signature = ?",
        (signature,),
    ).fetchone()
    attempts = [
        _serialize_submission_attempt(row)
        for row in _bot().connection.execute(
            """
            SELECT attempt_number, succeeded, error_message, happened_at
            FROM submission_attempts
            WHERE opportunity_signature = ?
            ORDER BY attempt_number ASC
            """,
            (signature,),
        ).fetchall()
    ]
    response = {
        "opportunity": opportunity,
        "application": _serialize_application(application_row) if application_row else None,
        "submission_attempts": attempts,
    }
    return jsonify(response)


@app.post("/opportunities/<signature>/submit")
@require_role("admin")
def submit_opportunity(signature: str) -> Response:
    payload = _get_request_json()
    status = str(payload.get("status", "")).strip()
    next_action = str(payload.get("next_action", "")).strip()
    submission_reference = payload.get("submission_reference")

    if not status:
        raise ValueError("Field 'status' is required.")
    if not next_action:
        raise ValueError("Field 'next_action' is required.")
    if submission_reference is not None and not isinstance(submission_reference, str):
        raise ValueError("Field 'submission_reference' must be a string or null.")

    # CSRF protection is intentionally not implemented to keep this Flask app
    # limited to Flask + stdlib. Use flask-wtf or equivalent in production.
    result = _bot().submit_application(
        signature,
        submission_reference=submission_reference,
        status=status,
        next_action=next_action,
    )
    return jsonify(result), 201


@app.get("/donors")
@require_role("admin", "auditor")
def list_donors() -> Response:
    return jsonify(_bot().list_donors())


@app.post("/donors")
@require_role("admin")
def upsert_donor() -> Response:
    payload = _get_request_json()
    email = str(payload.get("email", "")).strip()
    name = str(payload.get("name", "")).strip()
    opted_out = _coerce_bool(payload.get("opted_out", False), "opted_out")
    preferences = payload.get("preferences", {})
    locale = payload.get("locale")
    data_classification = payload.get("data_classification")
    field_classifications = payload.get("field_classifications")

    if not email:
        raise ValueError("Field 'email' is required.")
    if not name:
        raise ValueError("Field 'name' is required.")
    # Validate email format before passing to the bot layer.
    email = _validate_email(email)
    if preferences is None:
        preferences = {}
    if not isinstance(preferences, dict):
        raise ValueError("Field 'preferences' must be an object.")
    if field_classifications is not None and not isinstance(field_classifications, dict):
        raise ValueError("Field 'field_classifications' must be an object.")

    _bot().upsert_donor(
        email=email,
        name=name,
        opted_out=opted_out,
        preferences=preferences,
        locale=None if locale is None else str(locale),
        data_classification=None if data_classification is None else str(data_classification),
        field_classifications=field_classifications,
    )
    donor = _fetch_donor(email)
    return jsonify(donor), 201


@app.post("/donors/<path:email>/opt-out")
@require_role("admin")
def opt_out_donor(email: str) -> Response:
    donor = _fetch_donor(email)
    if donor is None:
        return _json_error("Donor not found", 404)

    _bot().set_donor_opt_out(email, opted_out=True)
    updated_donor = _fetch_donor(email)
    return jsonify(updated_donor)


@app.get("/analytics")
@require_role("admin", "auditor")
def get_analytics() -> Response:
    stats = _bot().get_outreach_analytics()
    return jsonify({"stats": stats})


@app.get("/audit-log")
@require_role("admin", "auditor")
def audit_log() -> Response:
    logs = [_serialize_audit_log(row) for row in _bot().connection.execute(
        """
        SELECT id, happened_at, action, details_json
        FROM audit_logs
        ORDER BY happened_at DESC, id DESC
        LIMIT 100
        """
    ).fetchall()]
    return jsonify(logs)


@app.get("/settings")
@require_role("staff", "admin", "auditor")
def settings_page() -> str:
    bot = _bot()
    smtp_configured = SMTPEmailSender.is_configured()
    context = {
        "current_role": getattr(g, "current_role", None),
        "organization_profile": bot.load_organization_profile(),
        "search_settings": bot.load_search_settings(),
        "credentials": bot.list_credentials(),
        "residency_status": bot.get_data_residency_status(),
        "privacy_policy_versions": bot.list_privacy_policy_versions(limit=10),
        "smtp_configured": smtp_configured,
        "smtp_host": os.environ.get("SMTP_HOST", ""),
        "ui_locale": _resolve_ui_locale(),
        "supported_locales": bot.list_locale_definitions(),
    }
    return render_template("settings.html", **context)


@app.get("/translations")
@require_role("staff", "admin", "auditor")
def translation_review_dashboard() -> str:
    bot = _bot()
    status = request.args.get("status")
    locale = request.args.get("review_locale")
    reviews = [
        _serialize_translation_review(review)
        for review in bot.list_translation_reviews(
            status=status or None,
            locale=locale or None,
        )
    ]
    counts = {
        "pending": len([review for review in reviews if review["status"] == "pending"]),
        "approved": len([review for review in reviews if review["status"] == "approved"]),
        "rejected": len([review for review in reviews if review["status"] == "rejected"]),
    }
    return render_template(
        "translations.html",
        current_role=getattr(g, "current_role", None),
        reviews=reviews,
        review_counts=counts,
        selected_status=status or "",
        selected_review_locale=locale or "",
        ui_locale=_resolve_ui_locale(),
        supported_locales=bot.list_locale_definitions(),
    )


@app.get("/translations/locales")
@require_role("staff", "admin", "auditor")
def translation_locales() -> Response:
    return jsonify({"locales": _bot().list_locale_definitions()})


@app.get("/translations/reviews")
@require_role("staff", "admin", "auditor")
def list_translation_reviews() -> Response:
    status = request.args.get("status")
    locale = request.args.get("locale")
    reviews = [
        _serialize_translation_review(review)
        for review in _bot().list_translation_reviews(
            status=status or None,
            locale=locale or None,
        )
    ]
    return jsonify({"reviews": reviews, "count": len(reviews)})


@app.post("/translations/reviews")
@require_role("staff", "admin")
def create_translation_review() -> Response:
    payload = _get_request_json()
    review = _bot().submit_translation_review(
        locale=str(payload.get("locale", "")),
        translation_key=str(payload.get("translation_key", "")),
        source_text=str(payload.get("source_text", "")),
        translated_text=str(payload.get("translated_text", "")),
        submitter_notes=payload.get("submitter_notes"),
        submitted_by_role=getattr(g, "current_role", None),
    )
    return jsonify(_serialize_translation_review(review)), 201


@app.post("/translations/reviews/<int:review_id>/decision")
@require_role("staff", "admin")
def decide_translation_review(review_id: int) -> Response:
    payload = _get_request_json()
    review = _bot().review_translation(
        review_id,
        status=str(payload.get("status", "")),
        reviewer_notes=payload.get("reviewer_notes"),
        reviewed_by_role=getattr(g, "current_role", None),
    )
    return jsonify(_serialize_translation_review(review))


@app.post("/settings/organization")
@require_role("admin")
def update_organization_settings() -> Response:
    payload = _get_request_json()
    if not payload:
        raise ValueError(
            "Request body must contain at least one profile field, e.g. "
            "'name', 'mission', or 'registration_number'."
        )
    data_classification = payload.pop("data_classification", None)
    field_classifications = payload.pop("field_classifications", None)
    if field_classifications is not None and not isinstance(field_classifications, dict):
        raise ValueError("Field 'field_classifications' must be an object.")
    _bot().store_setting(
        "profile",
        payload,
        data_classification=None if data_classification is None else str(data_classification),
        field_classifications=field_classifications,
    )
    return jsonify({"organization_profile": _bot().load_organization_profile()})


@app.post("/settings/search")
@require_role("admin")
def update_search_settings() -> Response:
    payload = _get_request_json()
    keywords = _coerce_list(payload.get("keywords", []), "keywords")
    trusted_sources = _coerce_list(payload.get("trusted_sources", []), "trusted_sources")

    settings = _bot().store_search_settings(keywords=keywords, trusted_sources=trusted_sources)
    return jsonify({"search_settings": settings})


@app.post("/settings/credentials")
@require_role("admin")
def register_credential_route() -> Response:
    payload = _get_request_json()
    alias = str(payload.get("alias", "")).strip()
    env_var_name = str(payload.get("env_var_name", "")).strip()
    if not alias:
        raise ValueError("Field 'alias' is required.")
    if not env_var_name:
        raise ValueError("Field 'env_var_name' is required.")

    _bot().register_credential(alias, env_var_name)
    return jsonify({"credentials": _bot().list_credentials()}), 201


@app.post("/settings/discover")
@require_role("admin")
def run_discovery_now() -> Response:
    """Trigger a live search across configured donation sources.

    Demonstrates the bot's donation-search capability directly from the
    admin panel: it queries every configured portal connector, filters by
    the saved keyword/source settings (or an ad-hoc override), and persists
    any newly discovered opportunities.
    """
    payload = request.get_json(silent=True) or {}
    keywords = _coerce_list(payload.get("keywords"), "keywords") if "keywords" in payload else None
    trusted_sources = (
        _coerce_list(payload.get("trusted_sources"), "trusted_sources")
        if "trusted_sources" in payload
        else None
    )

    status_code, payload = dispatch_discovery(
        keywords=keywords,
        trusted_sources=trusted_sources,
        db_path=os.environ.get("BOT_DB_PATH", "funding_bot.db"),
    )
    return jsonify(payload), status_code


@app.post("/settings/privacy-policy")
@require_role("admin")
def generate_privacy_policy() -> Response:
    payload = _get_request_json()
    output_dir = str(
        payload.get("output_dir", os.environ.get("PRIVACY_POLICY_OUTPUT_DIR", "generated/privacy_policies"))
    ).strip()
    if not output_dir:
        raise ValueError("Field 'output_dir' must not be empty.")

    generated = _bot().generate_privacy_policies(
        output_dir=output_dir,
        jurisdictions=_coerce_list(payload.get("jurisdictions"), "jurisdictions")
        if payload.get("jurisdictions") is not None
        else None,
        formats=_coerce_list(payload.get("formats"), "formats")
        if payload.get("formats") is not None
        else None,
        effective_date=payload.get("effective_date"),
    )
    return (
        jsonify(
            {
                "policies": generated,
                "residency_status": _bot().get_data_residency_status(),
                "versions": _bot().list_privacy_policy_versions(limit=10),
            }
        ),
        201,
    )


@app.post("/settings/test-outreach")
@require_role("admin")
def send_test_outreach() -> Response:
    """Compose (and optionally send) a donor outreach email from the panel.

    This demonstrates the bot's ability to communicate with a donor without
    requiring CLI access: by default the email is only composed and logged
    (``dry_run``); set ``"dry_run": false`` to actually deliver it via the
    configured SMTP credentials.
    """
    payload = _get_request_json()
    email = str(payload.get("email", "")).strip()
    name = str(payload.get("name", "")).strip()
    dry_run = _coerce_bool(payload.get("dry_run", True), "dry_run")
    subject_template = payload.get("subject_template")
    body_template = payload.get("body_template")
    locale = payload.get("locale")

    if not email:
        raise ValueError("Field 'email' is required.")
    if not name:
        raise ValueError("Field 'name' is required.")

    sender = None if dry_run else SMTPEmailSender.from_env()
    if subject_template is None and body_template is None:
        if locale is not None:
            _bot().upsert_donor(email=email, name=name, locale=str(locale))
        result = _bot().send_outreach_from_template(
            _bot().DEFAULT_OUTREACH_TEMPLATE,
            email,
            name,
            sender=sender,
        )
    else:
        if subject_template is None or body_template is None:
            default_subject, default_body = _bot()._resolve_catalog_template(
                _bot().DEFAULT_OUTREACH_TEMPLATE,
                segment="unknown",
                locale=str(locale) if locale is not None else _bot().DEFAULT_TEMPLATE_LOCALE,
            ) or (
                "Thank you for supporting {organization_name}",
                "Dear {donor_name},\n\nThank you for your continued interest in {organization_name}.",
            )
            subject_template = subject_template or default_subject
            body_template = body_template or default_body
        result = _bot().send_outreach(
            donor_email=email,
            donor_name=name,
            subject_template=subject_template,
            body_template=body_template,
            sender=sender,
            locale=None if locale is None else str(locale),
        )
    result["dry_run"] = dry_run
    return jsonify(result), 201


@app.get("/tasks")
@app.get("/task-directory")
@require_role("staff", "admin", "auditor")
def list_tasks_directory_route() -> Response:
    current_role = getattr(g, "current_role", None)
    assigned_to = request.args.get("assigned_to") or request.args.get("assignee")
    if current_role not in {"admin", "auditor"}:
        normalized_assignee = str(assigned_to or "").strip().lower()
        if normalized_assignee and normalized_assignee != current_role:
            return _json_error("Forbidden", 403)
        assigned_to = current_role
    tasks = _bot().list_tasks(
        assigned_to=assigned_to,
        assignee_email=request.args.get("assignee_email"),
        status=request.args.get("status"),
        due_date_before=request.args.get("due_date_before"),
        due_date_after=request.args.get("due_date_after"),
        source=request.args.get("source"),
        sort=request.args.get("sort"),
        viewer_email=request.args.get("viewer_email"),
    )
    return jsonify(tasks)


@app.post("/tasks")
@app.post("/task-directory")
@require_role("admin")
def create_task_directory_route() -> Response:
    payload = _get_request_json()
    title = str(payload.get("title", "")).strip()
    assigned_to = str(payload.get("assigned_to", "")).strip()
    if not title:
        raise ValueError("Field 'title' is required.")
    if not assigned_to:
        raise ValueError("Field 'assigned_to' is required.")

    task = _bot().create_task(
        title=title,
        assigned_to=assigned_to,
        description=str(payload.get("description", "")),
        status=str(payload.get("status", "todo")),
        due_date=payload.get("due_date"),
        external_id=payload.get("external_id"),
        source=str(payload.get("source", "manual")),
        assignee_email=payload.get("assignee_email"),
        assignee_name=payload.get("assignee_name"),
        sender=_task_assignment_sender(),
    )
    return jsonify({"task": task, "notification": task.get("assignment_notification")}), 201


@app.get("/tasks/<int:task_id>")
@app.get("/task-directory/<int:task_id>")
@require_role("staff", "admin", "auditor")
def get_task_directory_route(task_id: int) -> Response:
    task = _bot().get_task(task_id, viewer_email=request.args.get("viewer_email"))
    current_role = getattr(g, "current_role", None)
    if current_role not in {"admin", "auditor"} and task["assigned_to"] != current_role:
        return _json_error("Forbidden", 403)
    return jsonify({"task": task})


@app.get("/api/tasks/export")
@require_role("admin", "auditor")
def export_tasks_route() -> Response:
    tasks = _bot().list_tasks(
        assigned_to=request.args.get("assigned_to") or request.args.get("assignee"),
        status=request.args.get("status"),
        due_date_before=request.args.get("due_date_before"),
        due_date_after=request.args.get("due_date_after"),
        source=request.args.get("source"),
        sort=request.args.get("sort"),
        assignee_email=request.args.get("assignee_email"),
        viewer_email=request.args.get("viewer_email"),
    )
    return jsonify({"tasks": tasks, "count": len(tasks)})


@app.post("/api/tasks/sync")
@require_role("admin")
def sync_tasks_route() -> Response:
    payload = _get_request_json()
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("Field 'tasks' must be a list of task objects.")
    synced = _bot().sync_tasks(tasks, default_source=str(payload.get("source", "external_sync")))
    return jsonify({"tasks": synced, "count": len(synced)})


@app.post("/api/tasks/import")
@require_role("admin")
def import_tasks_route() -> Response:
    csv_text = _read_task_import_csv()
    source = request.args.get("source") or request.form.get("source") or "csv_import"
    imported = _bot().import_tasks_from_csv(csv_text, default_source=str(source))
    return jsonify({"tasks": imported, "count": len(imported)}), 201


@app.post("/tasks/<int:task_id>/assign")
@app.post("/tasks/<int:task_id>/assignment")
@app.post("/task-directory/<int:task_id>/assignment")
@require_role("admin")
def assign_task_directory_route(task_id: int) -> Response:
    payload = _get_request_json()
    assigned_to = str(payload.get("assigned_to", "")).strip()
    if not assigned_to:
        raise ValueError("Field 'assigned_to' is required.")
    task = _bot().update_task_assignment(
        task_id,
        assigned_to=assigned_to,
        assignee_email=payload.get("assignee_email"),
        assignee_name=payload.get("assignee_name"),
        sender=_task_assignment_sender(),
        changed_by=getattr(g, "current_role", None),
    )
    return jsonify({"task": task, "notification": task.get("assignment_notification")})


@app.get("/tasks/<int:task_id>/comments")
@require_role("staff", "admin", "auditor")
def list_task_comments_route(task_id: int) -> Response:
    payload = _bot().list_task_comments(task_id, viewer_email=request.args.get("viewer_email"))
    payload["comments"] = [_serialize_task_comment(comment) for comment in payload["comments"]]
    return jsonify(payload)


@app.post("/tasks/<int:task_id>/comments")
@require_role("staff", "admin")
def create_task_comment_route(task_id: int) -> Response:
    payload = _get_request_json()
    comment = _bot().create_task_comment(
        task_id,
        author=str(payload.get("author", "")),
        content=str(payload.get("content", "")),
    )
    return jsonify(comment), 201


@app.patch("/tasks/<int:task_id>/comments/<int:comment_id>")
@require_role("staff", "admin")
def update_task_comment_route(task_id: int, comment_id: int) -> Response:
    payload = _get_request_json()
    content = str(payload.get("content", "")).strip()
    if not content:
        raise ValueError("Field 'content' is required.")
    comment = _bot().update_task_comment(task_id, comment_id, content=content)
    return jsonify(comment)


@app.delete("/tasks/<int:task_id>/comments/<int:comment_id>")
@require_role("staff", "admin")
def delete_task_comment_route(task_id: int, comment_id: int) -> Response:
    _bot().delete_task_comment(task_id, comment_id)
    return Response(status=204)


@app.post("/tasks/<int:task_id>/comments/read")
@require_role("staff", "admin", "auditor")
def mark_task_comments_read_route(task_id: int) -> Response:
    payload = _get_request_json()
    reader_email = str(payload.get("reader_email", "")).strip()
    if not reader_email:
        raise ValueError("Field 'reader_email' is required.")
    result = _bot().mark_task_comments_read(task_id, reader_email=reader_email)
    return jsonify(result)


@app.post("/tasks/<int:task_id>/status")
@require_role("staff", "admin", "auditor")
def transition_task_status_route(task_id: int) -> Response:
    payload = _get_request_json()
    new_status = str(payload.get("status", "")).strip()
    if not new_status:
        raise ValueError("Field 'status' is required.")

    task = _bot().get_task(task_id)
    current_role = getattr(g, "current_role", None)
    if current_role != "admin" and task["assigned_to"] != current_role:
        return _json_error("Forbidden", 403)

    updated_task = _bot().transition_task_status(
        task_id,
        new_status=new_status,
        changed_by=current_role or "unknown",
    )
    return jsonify({"task": updated_task, "notification": updated_task.get("notification")})


@app.get("/health")
def health() -> Response:
    return jsonify({"status": "ok", "queue": _get_queue_health_snapshot()})


@app.get("/health/queue")
def queue_health() -> Response:
    snapshot = _get_queue_health_snapshot()
    status_code = 200 if snapshot["status"] in {"ok", "disabled"} else 503
    return jsonify(snapshot), status_code


@app.get("/metrics")
@require_role("admin", "auditor")
def metrics() -> Response:
    """Prometheus-compatible text metrics endpoint.

    Exposes basic operational counters so that a Prometheus scraper or
    Grafana agent can ingest them without an external library.
    """
    bot = _bot()
    conn = bot.connection

    opportunities_total = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
    applications_total = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    pending_applications = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE status IN ('pending','submitted','in_review')"
    ).fetchone()[0]
    donors_total = conn.execute("SELECT COUNT(*) FROM donors").fetchone()[0]
    opted_out_donors = conn.execute("SELECT COUNT(*) FROM donors WHERE opted_out = 1").fetchone()[0]
    audit_log_total = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
    communications_total = conn.execute("SELECT COUNT(*) FROM communications").fetchone()[0]
    tasks_total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    uptime_seconds = time.time() - _APP_START_TIME
    task_counts = bot.get_task_status_counts()
    queue_metrics = bot.get_queue_metrics()
    queue_health = _get_queue_health_snapshot()
    queue_status_value = 1 if queue_health["status"] == "ok" else 0
    task_assignments = conn.execute(
        "SELECT assigned_to, COUNT(*) AS total FROM tasks GROUP BY assigned_to ORDER BY assigned_to ASC"
    ).fetchall()

    lines = [
        "# HELP funding_bot_opportunities_total Total funding opportunities discovered",
        "# TYPE funding_bot_opportunities_total counter",
        f"funding_bot_opportunities_total {opportunities_total}",
        "# HELP funding_bot_applications_total Total grant applications recorded",
        "# TYPE funding_bot_applications_total counter",
        f"funding_bot_applications_total {applications_total}",
        "# HELP funding_bot_pending_applications Applications awaiting a decision",
        "# TYPE funding_bot_pending_applications gauge",
        f"funding_bot_pending_applications {pending_applications}",
        "# HELP funding_bot_donors_total Total donor records",
        "# TYPE funding_bot_donors_total gauge",
        f"funding_bot_donors_total {donors_total}",
        "# HELP funding_bot_opted_out_donors Donors who have opted out of outreach",
        "# TYPE funding_bot_opted_out_donors gauge",
        f"funding_bot_opted_out_donors {opted_out_donors}",
        "# HELP funding_bot_audit_log_entries_total Total audit log entries",
        "# TYPE funding_bot_audit_log_entries_total counter",
        f"funding_bot_audit_log_entries_total {audit_log_total}",
        "# HELP funding_bot_communications_total Total outreach emails logged",
        "# TYPE funding_bot_communications_total counter",
        f"funding_bot_communications_total {communications_total}",
        *FundingBot.render_connector_metrics_prometheus(),
        "# HELP funding_bot_tasks_total Total collaboration tasks",
        "# TYPE funding_bot_tasks_total gauge",
        f"funding_bot_tasks_total {tasks_total}",
        "# HELP funding_bot_uptime_seconds Seconds since the web process started",
        "# TYPE funding_bot_uptime_seconds gauge",
        f"funding_bot_uptime_seconds {uptime_seconds:.3f}",
        "# HELP funding_bot_queue_health_status Queue health status (1=ok, 0=disabled/degraded)",
        "# TYPE funding_bot_queue_health_status gauge",
        f"funding_bot_queue_health_status {queue_status_value}",
        "# HELP funding_bot_queue_broker_up Whether the Celery broker is reachable (1=yes, 0=no)",
        "# TYPE funding_bot_queue_broker_up gauge",
        f"funding_bot_queue_broker_up {1 if queue_health['broker_reachable'] else 0}",
        "# HELP funding_bot_queue_active_tasks Active Celery tasks currently executing",
        "# TYPE funding_bot_queue_active_tasks gauge",
        f"funding_bot_queue_active_tasks {queue_health['active_tasks']}",
        "# HELP funding_bot_queue_pending_tasks Tasks waiting in the monitored queue",
        "# TYPE funding_bot_queue_pending_tasks gauge",
        f"funding_bot_queue_pending_tasks {queue_health['pending_tasks']}",
        "# HELP funding_bot_queue_depth Broker queue depth for the monitored Celery queue",
        "# TYPE funding_bot_queue_depth gauge",
        f"funding_bot_queue_depth {queue_health['queue_depth']}",
        "# HELP funding_bot_queue_workers Online Celery workers detected",
        "# TYPE funding_bot_queue_workers gauge",
        f"funding_bot_queue_workers {queue_health['worker_count']}",
        "# HELP funding_bot_queue_task_runs_running Queue task runs currently marked running in SQLite",
        "# TYPE funding_bot_queue_task_runs_running gauge",
        f"funding_bot_queue_task_runs_running {queue_metrics['running']}",
        "# HELP funding_bot_queue_task_runs_completed Queue task runs completed successfully in SQLite",
        "# TYPE funding_bot_queue_task_runs_completed counter",
        f"funding_bot_queue_task_runs_completed {queue_metrics['completed']}",
        "# HELP funding_bot_queue_task_runs_failed Queue task runs that exhausted retries and failed",
        "# TYPE funding_bot_queue_task_runs_failed counter",
        f"funding_bot_queue_task_runs_failed {queue_metrics['failed']}",
        "# HELP funding_bot_queue_task_runs_cancelled Queue task runs cancelled during graceful shutdown",
        "# TYPE funding_bot_queue_task_runs_cancelled counter",
        f"funding_bot_queue_task_runs_cancelled {queue_metrics['cancelled']}",
        "# HELP funding_bot_queue_task_retries_total Retry attempts scheduled with exponential backoff",
        "# TYPE funding_bot_queue_task_retries_total counter",
        f"funding_bot_queue_task_retries_total {queue_metrics['retries_scheduled']}",
        "# HELP funding_bot_dead_letter_queue_total Queue task runs stored in the dead-letter queue",
        "# TYPE funding_bot_dead_letter_queue_total gauge",
        f"funding_bot_dead_letter_queue_total {queue_metrics['dead_lettered']}",
        "# HELP funding_bot_queue_duplicate_preventions_total Duplicate queue executions prevented by idempotency keys",
        "# TYPE funding_bot_queue_duplicate_preventions_total counter",
        f"funding_bot_queue_duplicate_preventions_total {queue_metrics['duplicate_preventions']}",
    ]
    lines.extend(
        [
            "# HELP funding_bot_tasks_status_total Tasks by workflow status",
            "# TYPE funding_bot_tasks_status_total gauge",
        ]
    )
    for status, total in task_counts.items():
        lines.append(f'funding_bot_tasks_status_total{{status="{status}"}} {total}')
    lines.extend(
        [
            "# HELP funding_bot_tasks_assigned_total Tasks assigned per dashboard role",
            "# TYPE funding_bot_tasks_assigned_total gauge",
        ]
    )
    for row in task_assignments:
        lines.append(
            f'funding_bot_tasks_assigned_total{{assigned_to="{row["assigned_to"]}"}} {row["total"]}'
        )
    lines.extend(
        [
            "# HELP funding_bot_connector_cache_hits_total Connector cache hits",
            "# TYPE funding_bot_connector_cache_hits_total counter",
            "# HELP funding_bot_connector_cache_misses_total Connector cache misses",
            "# TYPE funding_bot_connector_cache_misses_total counter",
            "# HELP funding_bot_connector_cache_entries Connector cache entries",
            "# TYPE funding_bot_connector_cache_entries gauge",
            "# HELP funding_bot_connector_cache_ttl_seconds Connector cache TTL in seconds",
            "# TYPE funding_bot_connector_cache_ttl_seconds gauge",
            "# HELP funding_bot_connector_page_size Connector page size",
            "# TYPE funding_bot_connector_page_size gauge",
        ]
    )
    for connector in default_connectors():
        cache_metrics = connector.cache_metrics()
        labels = f'connector_id="{cache_metrics["connector_id"]}"'
        lines.extend(
            [
                f'funding_bot_connector_cache_hits_total{{{labels}}} {int(cache_metrics.get("hits", 0))}',
                f'funding_bot_connector_cache_misses_total{{{labels}}} {int(cache_metrics.get("misses", 0))}',
                f'funding_bot_connector_cache_entries{{{labels}}} {int(cache_metrics.get("size", 0))}',
                f'funding_bot_connector_cache_ttl_seconds{{{labels}}} {float(cache_metrics.get("ttl_seconds", 0.0))}',
                f'funding_bot_connector_page_size{{{labels}}} {int(cache_metrics.get("page_size", 0))}',
            ]
        )
    return Response("\n".join(lines) + "\n", mimetype="text/plain; version=0.0.4")


@app.post("/feedback")
@require_role("staff", "admin")
def submit_feedback() -> Response:
    """Accept partner feature-request feedback.

    Expected JSON body::

        {
            "category": "feature_request" | "bug_report" | "general",
            "message":  "Free-text feedback from the partner.",
            "contact":  "optional-reply-to@example.org"
        }

    The entry is stored in the audit log under the ``partner_feedback``
    action so it can be reviewed during monthly compliance reports.
    """
    payload = _get_request_json()
    category = str(payload.get("category", "general")).strip().lower()
    message = str(payload.get("message", "")).strip()
    contact = str(payload.get("contact", "")).strip() or None

    allowed_categories = {"feature_request", "bug_report", "general"}
    if category not in allowed_categories:
        raise ValueError(
            f"Field 'category' must be one of {sorted(allowed_categories)}."
        )
    if not message:
        raise ValueError("Field 'message' is required.")
    if len(message) > MAX_FEEDBACK_MESSAGE_LENGTH:
        raise ValueError(
            f"Field 'message' must not exceed {MAX_FEEDBACK_MESSAGE_LENGTH} characters."
        )
    if contact:
        contact = _validate_email(contact)

    _bot()._log_action(
        "partner_feedback",
        category=category,
        message=message,
        contact=contact,
        submitted_by_role=getattr(g, "current_role", None),
    )
    return jsonify({"status": "received", "category": category}), 201


if __name__ == "__main__":
    app.run(
        host=os.environ.get("FLASK_HOST", "127.0.0.1"),
        port=int(os.environ.get("FLASK_PORT", "5000")),
    )
