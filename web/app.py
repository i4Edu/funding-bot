from __future__ import annotations

import base64
import binascii
import hmac
import importlib
import json
import os
import secrets
import socket
import sqlite3
import ssl
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

from flask import Flask, Response, g, jsonify, redirect, render_template, request, session, url_for
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from opentelemetry.trace import SpanKind

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from funding_bot import (  # noqa: E402
    DEFAULT_LOCALE_CODE,
    AccountLockedError,
    DuplicateSubmissionError,
    FundingBot,
    FundingBotError,
    MFARequiredError,
    OpportunityNotFoundError,
    SMTPEmailSender,
    TaskCommentNotFoundError,
    TaskNotFoundError,
    TaskTransitionError,
    _validate_email,
    default_connectors,
    sanitize_user_mapping,
    sanitize_user_string,
    validate_credential_alias,
    validate_env_var_name,
)
from observability import (  # noqa: E402
    configure_tracing,
    current_trace_id,
    extract_context,
    inject_context,
    record_slo_event,
    set_span_error,
    start_span,
    tracing_configuration_summary,
)
from task_queue import (  # noqa: E402
    dispatch_discovery,
    dispatch_export,
    get_queue_status,
    load_queue_config,
)

REQUEST_SPAN_KIND = getattr(SpanKind, "SERVER", getattr(SpanKind, "INTERNAL", SpanKind.CLIENT))

app = Flask(__name__, template_folder=str(Path(__file__).resolve().parent / "templates"))
app.config["JSON_SORT_KEYS"] = False
configure_tracing()
MAX_FEEDBACK_MESSAGE_LENGTH = 2000
DEFAULT_CELERY_HEALTH_TIMEOUT_SECONDS = 2.0
DEFAULT_HEALTH_CHECK_TIMEOUT_SECONDS = 1.0
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
TASK_BOARD_COLUMNS = (
    {"key": "pending", "label": "Todo", "request_status": "todo"},
    {"key": "in_progress", "label": "In Progress", "request_status": "in-progress"},
    {"key": "completed", "label": "Done", "request_status": "done"},
    {"key": "blocked", "label": "Blocked", "request_status": "blocked"},
)
TASK_STATUS_LABELS = {column["key"]: column["label"] for column in TASK_BOARD_COLUMNS}

ROLE_PASSWORD_ENV_VARS = {
    "admin": "ADMIN_PASSWORD",
    "staff": "STAFF_PASSWORD",
    "auditor": "AUDITOR_PASSWORD",
}
DEFAULT_SESSION_TIMEOUT_MINUTES = 30
DEFAULT_LOGIN_LOCKOUT_ATTEMPTS = 5
DEFAULT_LOGIN_LOCKOUT_MINUTES = 15
SESSION_ROLE_KEY = "authenticated_role"
SESSION_AUTHENTICATED_AT_KEY = "authenticated_at"
SESSION_LAST_SEEN_AT_KEY = "last_seen_at"
SESSION_CSRF_TOKEN_KEY = "csrf_token"
CSRF_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Track server start time for the uptime metric.
_APP_START_TIME = time.time()
_HEALTH_CHECK_METRICS_LOCK = threading.Lock()
_HEALTH_CHECK_METRICS = {
    "endpoints": {
        "health": {"checks_performed": 0, "failures": 0},
        "ready": {"checks_performed": 0, "failures": 0},
    },
    "components": {},
}


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
    raw_value = os.environ.get(
        "DASHBOARD_SESSION_TIMEOUT_MINUTES", str(DEFAULT_SESSION_TIMEOUT_MINUTES)
    )
    try:
        parsed = int(raw_value)
    except ValueError:
        return DEFAULT_SESSION_TIMEOUT_MINUTES
    return max(1, parsed)


def _read_positive_int_env(name: str, default: int) -> int:
    raw_value = os.environ.get(name, str(default))
    try:
        parsed = int(raw_value)
    except ValueError:
        return default
    return max(1, parsed)


def _login_lockout_attempts() -> int:
    return _read_positive_int_env(
        "WEB_LOGIN_LOCKOUT_ATTEMPTS",
        DEFAULT_LOGIN_LOCKOUT_ATTEMPTS,
    )


def _login_lockout_minutes() -> int:
    return _read_positive_int_env(
        "WEB_LOGIN_LOCKOUT_MINUTES",
        DEFAULT_LOGIN_LOCKOUT_MINUTES,
    )


class AuthenticationChallengeError(PermissionError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 401,
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.headers = headers or {}
        self.payload = payload or {}


def _csv_env_values(name: str, *, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw_value = os.environ.get(name)
    if raw_value is None:
        values = default
    else:
        values = tuple(item.strip() for item in raw_value.split(","))
    return tuple(value.rstrip("/") for value in values if value.rstrip("/"))


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


def _configure_request_protection(flask_app: Flask) -> None:
    flask_app.config.setdefault(
        "RATE_LIMIT_AUTH",
        os.environ.get("WEB_AUTH_RATE_LIMIT", "30 per minute"),
    )
    flask_app.config.setdefault(
        "RATE_LIMIT_API",
        os.environ.get("WEB_API_RATE_LIMIT", "120 per minute"),
    )
    flask_app.config.setdefault(
        "RATE_LIMIT_EXPORT",
        os.environ.get("WEB_EXPORT_RATE_LIMIT", "10 per minute"),
    )
    flask_app.config.setdefault(
        "RATE_LIMIT_STORAGE_URI",
        os.environ.get("WEB_RATE_LIMIT_STORAGE_URI", "memory://"),
    )


_configure_request_protection(app)


def _default_content_security_policy() -> str:
    directives = {
        "default-src": "'self'",
        "base-uri": "'self'",
        "form-action": "'self'",
        "object-src": "'none'",
        "frame-ancestors": "'none'",
        "frame-src": "'self'",
        "script-src": "'self' 'unsafe-inline'",
        "style-src": "'self' 'unsafe-inline'",
        "img-src": "'self' data:",
        "font-src": "'self' data:",
        "connect-src": "'self'",
    }
    return "; ".join(f"{directive} {value}" for directive, value in directives.items())


def _configure_http_security(flask_app: Flask) -> None:
    x_frame_options = os.environ.get("WEB_X_FRAME_OPTIONS", "DENY").strip().upper()
    if x_frame_options not in {"DENY", "SAMEORIGIN"}:
        x_frame_options = "DENY"

    hsts_max_age = _read_positive_int_env("WEB_HSTS_MAX_AGE_SECONDS", default=63072000)

    flask_app.config["SECURITY_CONTENT_SECURITY_POLICY"] = os.environ.get(
        "WEB_CONTENT_SECURITY_POLICY",
        _default_content_security_policy(),
    )
    flask_app.config["SECURITY_X_FRAME_OPTIONS"] = x_frame_options
    flask_app.config["SECURITY_X_CONTENT_TYPE_OPTIONS"] = "nosniff"
    flask_app.config["SECURITY_HSTS_POLICY"] = f"max-age={hsts_max_age}; includeSubDomains"
    flask_app.config["API_CORS_ALLOWED_ORIGINS"] = _csv_env_values(
        "WEB_API_CORS_ALLOWED_ORIGINS",
        default=(
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "https://localhost:3000",
            "https://127.0.0.1:3000",
        ),
    )
    flask_app.config["API_CORS_ALLOW_METHODS"] = (
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "OPTIONS",
    )
    flask_app.config["API_CORS_ALLOW_HEADERS"] = (
        "Authorization",
        "Content-Type",
        "X-CSRF-Token",
        "X-CSRFToken",
    )
    flask_app.config["API_CORS_EXPOSE_HEADERS"] = (
        "Retry-After",
        "WWW-Authenticate",
        "X-CSRF-Token",
        "X-RateLimit-Limit",
        "X-RateLimit-Remaining",
        "X-RateLimit-Reset",
    )
    flask_app.config["API_CORS_MAX_AGE_SECONDS"] = _read_positive_int_env(
        "WEB_API_CORS_MAX_AGE_SECONDS",
        default=86400,
    )
    flask_app.config["API_CORS_ALLOW_CREDENTIALS"] = True


_configure_http_security(app)


def _csrf_token_value() -> str:
    token = session.get(SESSION_CSRF_TOKEN_KEY)
    if isinstance(token, str) and token:
        return token
    token = secrets.token_urlsafe(32)
    session[SESSION_CSRF_TOKEN_KEY] = token
    return token


def _csrf_token_from_request() -> str:
    token = request.headers.get("X-CSRF-Token") or request.headers.get("X-CSRFToken")
    if token:
        return token
    return request.form.get("csrf_token", "")


def _has_explicit_basic_auth() -> bool:
    return request.headers.get("Authorization", "").startswith("Basic ")


def _format_rate_limit_reset(reset_at: Any) -> tuple[str | None, int | None]:
    if isinstance(reset_at, datetime):
        reset_time = reset_at if reset_at.tzinfo else reset_at.replace(tzinfo=timezone.utc)
    elif isinstance(reset_at, (int, float)):
        reset_time = datetime.fromtimestamp(float(reset_at), tz=timezone.utc)
    else:
        return None, None
    retry_after = max(int(reset_time.timestamp() - time.time()), 0)
    return reset_time.isoformat(), retry_after


def _rate_limit_breach_response(request_limit: Any) -> Response:
    reset_at, retry_after = _format_rate_limit_reset(getattr(request_limit, "reset_at", None))
    payload: dict[str, Any] = {
        "error": "Rate limit exceeded. Retry the request after the limit window resets.",
    }
    headers: dict[str, str] = {}
    if retry_after is not None:
        payload["retry_after"] = retry_after
        headers["Retry-After"] = str(retry_after)
    if reset_at is not None:
        payload["reset_at"] = reset_at
    return _build_json_response(payload, 429, headers=headers)


def _build_json_response(
    payload: dict[str, Any],
    status_code: int,
    *,
    headers: dict[str, str] | None = None,
) -> Response:
    response = jsonify(payload)
    response.status_code = status_code
    if headers:
        response.headers.update(headers)
    return response


def _prometheus_label_value(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _render_query_metrics_prometheus(query_metrics: dict[str, Any]) -> list[str]:
    buckets = list(query_metrics.get("buckets", []))
    summary = _mapping_or_default(query_metrics.get("summary"), {})
    statements = _mapping_or_default(query_metrics.get("statements"), {})
    rendered = [
        "# HELP funding_bot_db_query_slow_threshold_seconds Configured slow-query threshold in seconds",
        "# TYPE funding_bot_db_query_slow_threshold_seconds gauge",
        f"funding_bot_db_query_slow_threshold_seconds {float(query_metrics.get('slow_query_threshold_seconds', 0.25))}",
        "# HELP funding_bot_db_queries_in_flight Database queries currently executing",
        "# TYPE funding_bot_db_queries_in_flight gauge",
        "# HELP funding_bot_db_queries_total Database queries observed by statement type and final status",
        "# TYPE funding_bot_db_queries_total counter",
        "# HELP funding_bot_db_query_errors_total Database query failures by statement type",
        "# TYPE funding_bot_db_query_errors_total counter",
        "# HELP funding_bot_db_query_timeouts_total Database query timeouts or lock timeouts by statement type",
        "# TYPE funding_bot_db_query_timeouts_total counter",
        "# HELP funding_bot_db_slow_queries_total Database queries slower than the configured threshold",
        "# TYPE funding_bot_db_slow_queries_total counter",
        "# HELP funding_bot_db_query_duration_seconds Database query execution time histogram by statement type",
        "# TYPE funding_bot_db_query_duration_seconds histogram",
        "# HELP funding_bot_db_query_duration_seconds_max Maximum observed database query execution time by statement type",
        "# TYPE funding_bot_db_query_duration_seconds_max gauge",
    ]

    def _emit_statement(statement: str, metric: dict[str, Any]) -> None:
        statement_label = _prometheus_label_value(statement)
        rendered.append(
            f"funding_bot_db_queries_in_flight{{statement={statement_label}}} {int(metric.get('in_flight', 0))}"
        )
        for status in ("success", "error", "timeout"):
            rendered.append(
                f"funding_bot_db_queries_total{{statement={statement_label},status={_prometheus_label_value(status)}}} {int(metric.get(status, 0))}"
            )
        rendered.append(
            f"funding_bot_db_query_errors_total{{statement={statement_label}}} {int(metric.get('error', 0))}"
        )
        rendered.append(
            f"funding_bot_db_query_timeouts_total{{statement={statement_label}}} {int(metric.get('timeout', 0))}"
        )
        rendered.append(
            f"funding_bot_db_slow_queries_total{{statement={statement_label}}} {int(metric.get('slow', 0))}"
        )
        cumulative = 0
        bucket_counts = list(metric.get("bucket_counts", []))
        for bucket_limit, bucket_count in zip(buckets, bucket_counts):
            cumulative += int(bucket_count)
            rendered.append(
                f"funding_bot_db_query_duration_seconds_bucket{{statement={statement_label},le={_prometheus_label_value(str(bucket_limit))}}} {cumulative}"
            )
        rendered.append(
            f"funding_bot_db_query_duration_seconds_bucket{{statement={statement_label},le=\"+Inf\"}} {int(metric.get('count', 0))}"
        )
        rendered.append(
            f"funding_bot_db_query_duration_seconds_sum{{statement={statement_label}}} {float(metric.get('sum_duration_seconds', 0.0))}"
        )
        rendered.append(
            f"funding_bot_db_query_duration_seconds_count{{statement={statement_label}}} {int(metric.get('count', 0))}"
        )
        rendered.append(
            f"funding_bot_db_query_duration_seconds_max{{statement={statement_label}}} {float(metric.get('max_duration_seconds', 0.0))}"
        )

    _emit_statement("all", summary)
    for statement, metric in sorted(statements.items()):
        if isinstance(metric, dict):
            _emit_statement(statement, metric)
    return rendered


def _rate_limit_value(config_key: str) -> Callable[[], str]:
    def _resolver() -> str:
        return str(app.config[config_key])

    return _resolver


limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    headers_enabled=True,
    storage_uri=str(app.config["RATE_LIMIT_STORAGE_URI"]),
    on_breach=_rate_limit_breach_response,
    retry_after="delta-seconds",
)

auth_rate_limit = limiter.limit(_rate_limit_value("RATE_LIMIT_AUTH"), override_defaults=False)
api_rate_limit = limiter.limit(_rate_limit_value("RATE_LIMIT_API"), override_defaults=False)
export_rate_limit = limiter.limit(_rate_limit_value("RATE_LIMIT_EXPORT"), override_defaults=False)


def _bot() -> FundingBot:
    bot = g.get("_bot")
    if bot is None or getattr(bot, "connection", None) is None:
        bot = FundingBot(db_path=os.environ.get("BOT_DB_PATH", "funding_bot.db"))
        g._bot = bot
    return bot


def _is_dashboard_request_path(path: str) -> bool:
    return path == "/dashboard" or path.startswith("/dashboard/")


@app.before_request
def _start_request_trace() -> None:
    g.request_started_at = time.perf_counter()
    request_span = start_span(
        f"{request.method} {request.path}",
        kind=REQUEST_SPAN_KIND,
        carrier=dict(request.headers),
        attributes={
            "http.request.method": request.method,
            "url.path": request.path,
            "url.query": request.query_string.decode("utf-8", errors="ignore"),
        },
    )
    g.request_span_context_manager = request_span
    g.request_span = request_span.__enter__()


@app.after_request
def _finish_request_trace(response: Response) -> Response:
    started_at = getattr(g, "request_started_at", None)
    span = getattr(g, "request_span", None)
    if span is not None:
        span.set_attribute("http.response.status_code", int(response.status_code))
        trace_id = current_trace_id()
        if trace_id:
            response.headers["X-Trace-Id"] = trace_id
        inject_context(response.headers)
    if isinstance(started_at, (int, float)) and _is_dashboard_request_path(request.path):
        bot = _bot()
        record_slo_event(
            "dashboard_response_time",
            component=request.path,
            latency_seconds=time.perf_counter() - started_at,
            success=response.status_code < 500,
            metadata={"status_code": response.status_code},
            connection=bot.connection,
        )
        bot.connection.commit()
    context_manager = g.pop("request_span_context_manager", None)
    if context_manager is not None:
        context_manager.__exit__(None, None, None)
    g.pop("request_span", None)
    return response


def _mapping_or_default(value: Any, default: dict[str, Any]) -> dict[str, Any]:
    return value if isinstance(value, dict) else dict(default)


def _json_error(
    message: str,
    status_code: int,
    *,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> Response:
    body = {"error": message}
    if payload:
        body.update(payload)
    return _build_json_response(body, status_code, headers=headers)


def _auth_challenge(
    message: str = "Authentication required",
    *,
    status_code: int = 401,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> Response:
    combined_headers = {"WWW-Authenticate": 'Basic realm="Funding Bot Dashboard"'}
    if headers:
        combined_headers.update(headers)
    return _json_error(
        message,
        status_code,
        headers=combined_headers,
        payload=payload,
    )


def _session_timeout() -> timedelta:
    configured = app.config.get(
        "PERMANENT_SESSION_LIFETIME", timedelta(minutes=DEFAULT_SESSION_TIMEOUT_MINUTES)
    )
    return (
        configured
        if isinstance(configured, timedelta)
        else timedelta(minutes=DEFAULT_SESSION_TIMEOUT_MINUTES)
    )


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
    session.pop(SESSION_CSRF_TOKEN_KEY, None)


def _establish_authenticated_session(role: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    session.permanent = True
    session[SESSION_ROLE_KEY] = role
    session.setdefault(SESSION_AUTHENTICATED_AT_KEY, now)
    session[SESSION_LAST_SEEN_AT_KEY] = now
    session.setdefault(SESSION_CSRF_TOKEN_KEY, secrets.token_urlsafe(32))


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


def _mfa_code_from_request() -> str | None:
    raw_code = request.headers.get("X-MFA-Code") or request.headers.get("X-Backup-Code")
    if raw_code is None:
        return None
    return sanitize_user_string(
        raw_code,
        field_name="mfa_code",
        allow_empty=False,
        max_length=64,
    )


def _call_bot_auth_method(name: str, *args: Any, default: Any = None, **kwargs: Any) -> Any:
    bot = _bot()
    if not hasattr(type(bot), name):
        return default
    try:
        method = getattr(bot, name)
    except AttributeError:
        return default
    if not callable(method):
        return default
    return method(*args, **kwargs)


def _raise_auth_failure(
    role: str, *, reason: str, message: str = "Invalid authentication credentials"
) -> None:
    result = _call_bot_auth_method(
        "record_failed_authentication",
        role,
        lockout_threshold=_login_lockout_attempts(),
        lockout_minutes=_login_lockout_minutes(),
        reason=reason,
        default={"locked": False, "lockout_until": None},
    )
    if result["locked"]:
        raise AuthenticationChallengeError(
            f"Account '{role}' is temporarily locked.",
            status_code=423,
            headers={"Retry-After": str(_login_lockout_minutes() * 60)},
            payload={"lockout_until": result["lockout_until"]},
        )
    raise AuthenticationChallengeError(message)


def _get_authenticated_role() -> str:
    header = request.headers.get("Authorization", "")
    if not header:
        session_role = _get_session_role()
        if session_role is not None:
            return session_role
        raise AuthenticationChallengeError("Authentication required")
    if not header.startswith("Basic "):
        raise AuthenticationChallengeError("Invalid authentication credentials")

    token = header.split(" ", 1)[1].strip()
    try:
        decoded = base64.b64decode(token, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise AuthenticationChallengeError("Invalid authentication credentials") from exc

    username, separator, password = decoded.partition(":")
    if not separator:
        raise AuthenticationChallengeError("Invalid authentication credentials")

    role = username.strip().lower()
    if role not in ROLE_PASSWORD_ENV_VARS:
        raise AuthenticationChallengeError("Invalid authentication credentials")

    try:
        _call_bot_auth_method("assert_account_not_locked", role)
    except AccountLockedError as exc:
        state = _call_bot_auth_method("get_auth_security_state", role, default={})
        raise AuthenticationChallengeError(
            str(exc),
            status_code=423,
            headers={"Retry-After": str(_login_lockout_minutes() * 60)},
            payload={"lockout_until": state.get("lockout_until")},
        ) from exc

    expected_password = os.environ.get(ROLE_PASSWORD_ENV_VARS[role])
    if not expected_password or not hmac.compare_digest(password, expected_password):
        _raise_auth_failure(role, reason="password")

    state = _call_bot_auth_method(
        "get_auth_security_state",
        role,
        default={"mfa_enabled": False},
    )
    if state["mfa_enabled"]:
        mfa_code = _mfa_code_from_request()
        if not mfa_code:
            raise AuthenticationChallengeError(
                "MFA code required.",
                headers={"X-MFA-Required": "1"},
                payload={"mfa_required": True},
            )
        verification = _call_bot_auth_method(
            "verify_mfa_code",
            role,
            mfa_code,
            default={"verified": False},
        )
        if not verification["verified"]:
            _raise_auth_failure(role, reason="mfa", message="Invalid MFA code.")

    _call_bot_auth_method("clear_auth_failures", role)
    _establish_authenticated_session(role)
    return role


def _csrf_error_response(message: str = "CSRF token missing or invalid.") -> Response:
    return _build_json_response(
        {"error": message, "csrf_token": _csrf_token_value()},
        400,
    )


def _is_api_route(path: str) -> bool:
    return path.startswith("/api/")


def _origin_from_request() -> str | None:
    origin = request.headers.get("Origin", "").strip()
    if not origin:
        return None
    return origin.rstrip("/")


def _is_allowed_cors_origin(origin: str | None) -> bool:
    if not origin:
        return False
    return origin in set(app.config.get("API_CORS_ALLOWED_ORIGINS", ()))


def _append_vary_header(response: Response, *values: str) -> None:
    existing = [
        item.strip() for item in response.headers.get("Vary", "").split(",") if item.strip()
    ]
    seen = set(existing)
    for value in values:
        if value not in seen:
            existing.append(value)
            seen.add(value)
    if existing:
        response.headers["Vary"] = ", ".join(existing)


def _apply_cors_headers(response: Response, *, origin: str, preflight: bool = False) -> Response:
    response.headers["Access-Control-Allow-Origin"] = origin
    if app.config.get("API_CORS_ALLOW_CREDENTIALS", True):
        response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Expose-Headers"] = ", ".join(
        app.config["API_CORS_EXPOSE_HEADERS"]
    )
    _append_vary_header(response, "Origin")
    if preflight:
        response.headers["Access-Control-Allow-Methods"] = ", ".join(
            app.config["API_CORS_ALLOW_METHODS"]
        )
        response.headers["Access-Control-Allow-Headers"] = ", ".join(
            app.config["API_CORS_ALLOW_HEADERS"]
        )
        response.headers["Access-Control-Max-Age"] = str(app.config["API_CORS_MAX_AGE_SECONDS"])
        _append_vary_header(
            response,
            "Access-Control-Request-Method",
            "Access-Control-Request-Headers",
        )
    return response


def _request_is_secure() -> bool:
    if request.is_secure:
        return True
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "")
    return forwarded_proto.split(",", 1)[0].strip().lower() == "https"


def _build_cors_preflight_response() -> Response:
    origin = _origin_from_request()
    if not _is_allowed_cors_origin(origin):
        return _json_error("Origin not allowed for this API.", 403)
    assert origin is not None, "Origin must be non-None when CORS validation passes"
    response = app.response_class(status=204)
    return _apply_cors_headers(response, origin=origin, preflight=True)


@app.context_processor
def inject_csrf_token() -> dict[str, Callable[[], str]]:
    return {"csrf_token": _csrf_token_value}


@app.before_request
def validate_csrf_token() -> Response | None:
    if request.method == "OPTIONS" and _is_api_route(request.path):
        return _build_cors_preflight_response()
    if request.method not in CSRF_UNSAFE_METHODS:
        return None
    if _has_explicit_basic_auth():
        return None
    if not session.get(SESSION_ROLE_KEY):
        return None
    supplied_token = _csrf_token_from_request()
    expected_token = session.get(SESSION_CSRF_TOKEN_KEY)
    if (
        not isinstance(expected_token, str)
        or not expected_token
        or not supplied_token
        or not hmac.compare_digest(supplied_token, expected_token)
    ):
        session[SESSION_CSRF_TOKEN_KEY] = secrets.token_urlsafe(32)
        return _csrf_error_response()
    return None


@app.after_request
def attach_security_headers(response: Response) -> Response:
    response.headers.setdefault(
        "Content-Security-Policy",
        app.config["SECURITY_CONTENT_SECURITY_POLICY"],
    )
    response.headers.setdefault("X-Frame-Options", app.config["SECURITY_X_FRAME_OPTIONS"])
    response.headers.setdefault(
        "X-Content-Type-Options",
        app.config["SECURITY_X_CONTENT_TYPE_OPTIONS"],
    )
    if _request_is_secure():
        response.headers.setdefault(
            "Strict-Transport-Security",
            app.config["SECURITY_HSTS_POLICY"],
        )
    if _is_api_route(request.path):
        origin = _origin_from_request()
        if _is_allowed_cors_origin(origin):
            assert origin is not None, "Origin must be non-None when CORS validation passes"
            response = _apply_cors_headers(
                response,
                origin=origin,
                preflight=request.method == "OPTIONS",
            )
        elif origin:
            _append_vary_header(response, "Origin")
    if session.get(SESSION_ROLE_KEY):
        response.headers.setdefault("X-CSRF-Token", _csrf_token_value())
    return response


def require_role(*roles: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    allowed_roles = {role.lower() for role in roles}

    def decorator(view_func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view_func)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            try:
                role = _get_authenticated_role()
            except AuthenticationChallengeError as exc:
                return _auth_challenge(
                    str(exc),
                    status_code=exc.status_code,
                    headers=exc.headers,
                    payload=exc.payload,
                )
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
        return [
            sanitize_user_string(
                item,
                field_name=field_name,
                allow_empty=False,
                html_escape=True,
            )
            for item in value.split(",")
            if item.strip()
        ]
    if isinstance(value, list):
        return [
            sanitize_user_string(
                item,
                field_name=field_name,
                allow_empty=False,
                html_escape=True,
            )
            for item in value
            if str(item).strip()
        ]
    raise ValueError(f"Field '{field_name}' must be a list or comma-separated string.")


def _serialize_opportunity(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["raw_data"] = _parse_json_column(data.pop("raw_data_json", "{}"))
    return data


def _serialize_donor(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["opted_out"] = bool(data["opted_out"])
    data["preferences"] = _parse_json_column(data.pop("preferences_json", "{}"))
    data["field_classifications"] = _parse_json_column(data.pop("field_classifications_json", "{}"))
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


def _export_schedule_snapshot() -> dict[str, Any]:
    return {
        "hour": int(os.environ.get("DATA_EXPORT_SCHEDULE_HOUR", "1")),
        "minute": int(os.environ.get("DATA_EXPORT_SCHEDULE_MINUTE", "0")),
        "datasets": [
            dataset.strip()
            for dataset in os.environ.get(
                "DATA_EXPORT_DATASETS", "donors,tasks,matches,results"
            ).split(",")
            if dataset.strip()
        ],
        "format": os.environ.get("DATA_EXPORT_FORMAT", "json"),
        "output_dir": os.environ.get("DATA_EXPORT_OUTPUT_DIR", "generated/exports"),
        "archive": _env_flag("DATA_EXPORT_ARCHIVE", default=True),
    }


def _task_scope_for_role(role: str | None) -> str | None:
    if role in {"admin", "auditor"}:
        return None
    return role


def _group_tasks_by_status(tasks: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {status: [] for status in FundingBot.TASK_STATUSES}
    for task in tasks:
        grouped.setdefault(task["status"], []).append(task)
    return grouped


def _can_move_task(role: str | None, task: dict[str, Any]) -> bool:
    return role == "admin" or task.get("assignee") == role


def _task_status_label(status: str) -> str:
    return TASK_STATUS_LABELS.get(status, status.replace("_", " ").replace("-", " ").title())


def _serialize_translation_review(review: Any) -> dict[str, Any]:
    data = dict(review)
    if "locale_metadata" not in data:
        data["locale_metadata"] = _bot().get_locale_definition(data.get("locale"))
    return data


def _fetch_opportunity(signature: str) -> dict[str, Any]:
    row = (
        _bot()
        .connection.execute(
            "SELECT * FROM opportunities WHERE signature = ?",
            (signature,),
        )
        .fetchone()
    )
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
    normalized = sanitize_user_string(value, field_name="query", max_length=256)
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


def _string_iterable_or_empty(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return []


def _task_assignee_options() -> list[str]:
    rows = _bot().connection.execute("""
        SELECT DISTINCT assigned_to AS assignee
        FROM tasks
        WHERE assigned_to != ''
        ORDER BY assigned_to COLLATE NOCASE ASC
        """).fetchall()
    return [str(row["assignee"]) for row in rows]


def _task_assignment_options() -> list[str]:
    options = {"admin", "staff", "auditor"}
    options.update(_task_assignee_options())
    return sorted(options)


def _resolve_ui_locale() -> dict[str, Any]:
    requested_locale = request.args.get("locale", DEFAULT_LOCALE_CODE)
    try:
        return _bot().get_locale_definition(requested_locale)
    except ValueError:
        return _bot().get_locale_definition(DEFAULT_LOCALE_CODE)


def _dashboard_context() -> dict[str, Any]:
    with start_span("dashboard.load_context", kind=SpanKind.INTERNAL):
        now = datetime.now(timezone.utc)
        recent_cutoff = (now - timedelta(days=7)).isoformat()
        current_role = getattr(g, "current_role", None)
        task_scope = _task_scope_for_role(current_role)
        bot = _bot()

        new_opportunities_count = bot.connection.execute(
            "SELECT COUNT(*) FROM opportunities WHERE discovered_at >= ?",
            (recent_cutoff,),
        ).fetchone()[0]
        applications_submitted_count = bot.connection.execute(
            "SELECT COUNT(*) FROM applications",
        ).fetchone()[0]
        pending_applications_count = bot.connection.execute(
            "SELECT COUNT(*) FROM applications WHERE status IN ('pending', 'submitted', 'in_review')",
        ).fetchone()[0]
        donor_communications_count = bot.connection.execute(
            "SELECT COUNT(*) FROM communications",
        ).fetchone()[0]
        pending_translation_reviews_count = bot.connection.execute(
            "SELECT COUNT(*) FROM translation_reviews WHERE status = 'pending'",
        ).fetchone()[0]

        recent_opportunities = [
            _serialize_opportunity(row)
            for row in bot.connection.execute(
                "SELECT * FROM opportunities ORDER BY discovered_at DESC LIMIT 10"
            ).fetchall()
        ]
        recent_applications = [_serialize_application(row) for row in bot.connection.execute("""
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
                """).fetchall()]
        my_task_counts = bot.get_task_status_counts(assigned_to=task_scope) if current_role else {}
        overdue_tasks = [
            _serialize_task(task)
            for task in bot.list_tasks(
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
            "slo_summary": bot.get_slo_summary(),
            "tracing_summary": tracing_configuration_summary(),
            "ui_locale": _resolve_ui_locale(),
        }


def _task_dashboard_context(filters: dict[str, str | None]) -> dict[str, Any]:
    with start_span("dashboard.load_task_board", kind=SpanKind.INTERNAL):
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
            "task_board_columns": TASK_BOARD_COLUMNS,
            "task_status_labels": TASK_STATUS_LABELS,
            "task_sort_options": TASK_SORT_OPTIONS,
            "task_assignee_options": _task_assignee_options(),
            "task_assignment_options": _task_assignment_options(),
            "can_filter_all_assignees": current_role in {"admin", "auditor"},
            "can_reassign_tasks": current_role == "admin",
            "can_manage_tasks": current_role == "admin",
            "can_export_tasks": current_role in {"admin", "auditor"},
            "ui_locale": _resolve_ui_locale(),
        }


def _queue_health_timeout_seconds() -> float:
    configured = os.environ.get(
        "CELERY_HEALTH_TIMEOUT_SECONDS",
        os.environ.get(
            "CELERY_INSPECT_TIMEOUT_SECONDS", str(DEFAULT_CELERY_HEALTH_TIMEOUT_SECONDS)
        ),
    )
    try:
        timeout = float(configured)
    except ValueError:
        return DEFAULT_CELERY_HEALTH_TIMEOUT_SECONDS
    return max(timeout, 0.1)


def _health_check_timeout_seconds() -> float:
    configured = os.environ.get(
        "HEALTH_CHECK_TIMEOUT_SECONDS", str(DEFAULT_HEALTH_CHECK_TIMEOUT_SECONDS)
    )
    try:
        timeout = float(configured)
    except ValueError:
        return DEFAULT_HEALTH_CHECK_TIMEOUT_SECONDS
    return max(timeout, 0.1)


def _count_tasks_by_worker(task_map: Any) -> int:
    if not isinstance(task_map, dict):
        return 0
    return sum(len(tasks) for tasks in task_map.values() if isinstance(tasks, list))


def reset_health_check_metrics() -> None:
    with _HEALTH_CHECK_METRICS_LOCK:
        _HEALTH_CHECK_METRICS["endpoints"] = {
            "health": {"checks_performed": 0, "failures": 0},
            "ready": {"checks_performed": 0, "failures": 0},
        }
        _HEALTH_CHECK_METRICS["components"] = {}


def _health_check_metrics_snapshot() -> dict[str, Any]:
    with _HEALTH_CHECK_METRICS_LOCK:
        return {
            "endpoints": {
                name: dict(metrics) for name, metrics in _HEALTH_CHECK_METRICS["endpoints"].items()
            },
            "components": {
                name: dict(metrics) for name, metrics in _HEALTH_CHECK_METRICS["components"].items()
            },
        }


def _record_health_check_metrics(
    endpoint: str, checks: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    failing_components = [
        name
        for name, result in checks.items()
        if str(result.get("status", "error")) not in {"ok", "disabled"}
    ]
    with _HEALTH_CHECK_METRICS_LOCK:
        endpoint_metrics = _HEALTH_CHECK_METRICS["endpoints"].setdefault(
            endpoint,
            {"checks_performed": 0, "failures": 0},
        )
        endpoint_metrics["checks_performed"] += 1
        if failing_components:
            endpoint_metrics["failures"] += 1
        for name, result in checks.items():
            component_metrics = _HEALTH_CHECK_METRICS["components"].setdefault(
                name,
                {"checks_performed": 0, "failures": 0},
            )
            component_metrics["checks_performed"] += 1
            if str(result.get("status", "error")) not in {"ok", "disabled"}:
                component_metrics["failures"] += 1
    return _health_check_metrics_snapshot()


def _redact_service_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    netloc = parsed.hostname or ""
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    if parsed.username:
        netloc = f"{parsed.username}:***@{netloc}"
    return urlunparse(parsed._replace(netloc=netloc))


def _redis_ping(url: str, *, timeout: float) -> dict[str, Any]:
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 6379
    payload = {
        "url": _redact_service_url(url),
        "host": host,
        "port": port,
        "scheme": parsed.scheme or "redis",
        "database": (parsed.path or "/0").lstrip("/") or "0",
        "timeout_seconds": timeout,
    }
    try:
        connection = socket.create_connection((host, port), timeout=timeout)
        try:
            if parsed.scheme == "rediss":
                connection = ssl.create_default_context().wrap_socket(
                    connection, server_hostname=host
                )
            connection.sendall(b"*1\r\n$4\r\nPING\r\n")
            response = connection.recv(16)
        finally:
            connection.close()
    except Exception as exc:
        return {**payload, "status": "error", "reachable": False, "error": str(exc)}
    if response.startswith(b"+PONG"):
        return {**payload, "status": "ok", "reachable": True}
    return {
        **payload,
        "status": "error",
        "reachable": False,
        "error": f"Unexpected Redis response: {response!r}",
    }


def _check_database_health(bot: FundingBot) -> dict[str, Any]:
    metrics = _mapping_or_default(getattr(bot, "get_database_pool_metrics", lambda: {})(), {})
    try:
        row = bot.connection.execute("SELECT 1").fetchone()
        reachable = bool(row and int(row[0]) == 1)
        status = "ok" if reachable else "error"
    except Exception as exc:
        return {
            "status": "error",
            "checked": True,
            "reachable": False,
            "error": str(exc),
            "metrics": metrics,
        }
    return {
        "status": status,
        "checked": True,
        "reachable": reachable,
        "metrics": metrics,
    }


def _check_database_health_from_path(
    db_path: str, *, initialization_error: str | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "checked": True,
        "db_path": db_path,
        "reachable": False,
        "metrics": {"db_path": db_path, "status": "unavailable"},
    }
    try:
        connection = sqlite3.connect(db_path)
        try:
            row = connection.execute("SELECT 1").fetchone()
        finally:
            connection.close()
    except Exception as exc:
        payload["status"] = "error"
        payload["error"] = initialization_error or str(exc)
        return payload
    reachable = bool(row and int(row[0]) == 1)
    payload["reachable"] = reachable
    payload["status"] = "ok" if reachable else "error"
    if initialization_error:
        payload["status"] = "degraded"
        payload["initialization_error"] = initialization_error
    return payload


def _try_create_bot_for_health() -> tuple[FundingBot | None, str | None]:
    try:
        return FundingBot(db_path=os.environ.get("BOT_DB_PATH", "funding_bot.db")), None
    except Exception as exc:
        return None, str(exc)


def _check_redis_health(queue_config: Any) -> dict[str, Any]:
    timeout_seconds = _health_check_timeout_seconds()
    cache_backend = (
        os.environ.get("FUNDING_BOT_CACHE_BACKEND", "memory").strip().lower() or "memory"
    )
    targets: list[dict[str, Any]] = []
    cache_url = os.environ.get("FUNDING_BOT_CACHE_URL", "")
    if cache_backend == "redis" and cache_url:
        targets.append({"role": "cache", **_redis_ping(cache_url, timeout=timeout_seconds)})
    if getattr(queue_config, "enable_task_queue", False):
        for role, url in (
            ("broker", getattr(queue_config, "broker_url", "")),
            ("result_backend", getattr(queue_config, "result_backend", "")),
        ):
            if urlparse(str(url)).scheme in {"redis", "rediss"}:
                targets.append({"role": role, **_redis_ping(str(url), timeout=timeout_seconds)})
    if not targets:
        return {
            "status": "disabled",
            "checked": False,
            "targets": [],
            "cache_backend": cache_backend,
            "message": "Redis is not configured for the current cache or queue settings.",
        }
    return {
        "status": "error" if any(target["status"] != "ok" for target in targets) else "ok",
        "checked": True,
        "targets": targets,
        "cache_backend": cache_backend,
    }


def _check_celery_health() -> dict[str, Any]:
    snapshot = _get_queue_health_snapshot()
    return {
        **snapshot,
        "checked": bool(snapshot.get("queue_enabled")),
    }


def _active_connectors(bot: FundingBot) -> list[Any]:
    if bot.connector_configs:
        return bot.connector_registry.build_connectors(
            bot.connector_configs,
            credential_resolver=bot.resolve_credential,
            cache_manager=bot.cache_manager,
        )
    return default_connectors(cache_manager=bot.cache_manager)


def _check_connector_health(
    bot: FundingBot | None, *, initialization_error: str | None = None
) -> dict[str, Any]:
    if bot is None:
        return {
            "status": "error",
            "checked": True,
            "count": 0,
            "healthy_count": 0,
            "connectors": [],
            "error": initialization_error or "Unable to initialize FundingBot.",
        }
    try:
        active_connectors = _active_connectors(bot)
    except Exception as exc:
        return {
            "status": "error",
            "checked": True,
            "count": 0,
            "healthy_count": 0,
            "connectors": [],
            "error": str(exc),
        }
    checks = []
    for connector in active_connectors:
        connector_name = str(
            getattr(
                connector,
                "connector_slug",
                getattr(connector, "source_name", type(connector).__name__),
            )
        )
        source_name = str(getattr(connector, "source_name", connector_name))
        mode = (
            "remote"
            if (
                getattr(connector, "http_client", None) is not None
                or getattr(connector, "transport", "") == "http"
            )
            else "demo"
        )
        try:
            health = connector.check_health()
            healthy = bool(health.get("healthy", True))
            checks.append(
                {
                    "connector": connector_name,
                    "source": source_name,
                    "mode": mode,
                    "status": "ok" if healthy else "degraded",
                    "healthy": healthy,
                    "health": health,
                }
            )
        except Exception as exc:
            checks.append(
                {
                    "connector": connector_name,
                    "source": source_name,
                    "mode": mode,
                    "status": "error",
                    "healthy": False,
                    "error": str(exc),
                }
            )
    if not checks:
        return {
            "status": "disabled",
            "checked": True,
            "count": 0,
            "healthy_count": 0,
            "connectors": [],
        }
    statuses = {entry["status"] for entry in checks}
    overall_status = (
        "error" if "error" in statuses else ("degraded" if "degraded" in statuses else "ok")
    )
    return {
        "status": overall_status,
        "checked": True,
        "count": len(checks),
        "healthy_count": sum(1 for entry in checks if entry["status"] == "ok"),
        "connectors": checks,
    }


def _status_code_for_health(payload_status: str) -> int:
    return 200 if payload_status == "ok" else 503


def _build_health_payload(endpoint: str) -> dict[str, Any]:
    db_path = os.environ.get("BOT_DB_PATH", "funding_bot.db")
    bot: FundingBot | None = None
    bot_initialization_error: str | None = None
    if endpoint != "health":
        bot, bot_initialization_error = _try_create_bot_for_health()
    queue_config = load_queue_config()
    checked_at = datetime.now(timezone.utc).isoformat()
    queue_mode = {
        "mode": queue_config.mode,
        "queue_enabled": queue_config.enable_task_queue,
        "legacy_cron_enabled": queue_config.enable_legacy_cron,
        "queue_name": queue_config.queue_name,
    }
    if endpoint == "health":
        checks = {
            "application": {
                "status": "ok",
                "checked": True,
                "uptime_seconds": round(time.time() - _APP_START_TIME, 3),
            },
            "database": _check_database_health_from_path(db_path),
        }
    else:
        checks = {
            "database": (
                _check_database_health(bot)
                if bot is not None
                else _check_database_health_from_path(
                    db_path,
                    initialization_error=bot_initialization_error,
                )
            ),
            "redis": _check_redis_health(queue_config),
            "celery": _check_celery_health(),
            "connectors": _check_connector_health(
                bot, initialization_error=bot_initialization_error
            ),
        }
    overall_status = (
        "ok"
        if all(
            str(result.get("status", "error")) in {"ok", "disabled"} for result in checks.values()
        )
        else "degraded"
    )
    failing_checks = [
        name
        for name, result in checks.items()
        if str(result.get("status", "error")) not in {"ok", "disabled"}
    ]
    metrics = _record_health_check_metrics(endpoint, checks)
    payload = {
        "status": overall_status,
        "service": "funding-bot",
        "endpoint": f"/{endpoint}",
        "checked_at": checked_at,
        "uptime_seconds": round(time.time() - _APP_START_TIME, 3),
        "queue": queue_mode,
        "checks": checks,
        "failing_checks": failing_checks,
        "metrics": metrics,
    }
    if endpoint == "health":
        payload["healthy"] = overall_status == "ok"
        payload["database"] = checks["database"]
    else:
        payload["ready"] = overall_status == "ok"
        payload["database"] = checks["database"]
        payload["redis"] = checks["redis"]
        payload["celery"] = checks["celery"]
        payload["connectors"] = checks["connectors"]
    if bot is not None:
        bot.close()
    return payload


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

    worker_names = sorted(
        {*active.keys(), *reserved.keys(), *scheduled.keys(), *stats.keys(), *ping.keys()}
    )
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
                "scheduled_tasks": (
                    len(worker_scheduled) if isinstance(worker_scheduled, list) else 0
                ),
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


def _flower_dashboard_url() -> str:
    configured = os.environ.get("FLOWER_DASHBOARD_URL", "").strip()
    if configured:
        return configured
    return "http://127.0.0.1:5555"


def _queue_monitoring_payload() -> dict[str, Any]:
    bot = _bot()
    return {
        "queue": _get_queue_health_snapshot(),
        "task_metrics": bot.get_queue_metrics(),
        "flower": {
            "url": _flower_dashboard_url(),
            "enabled": bool(_flower_dashboard_url()),
        },
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


@app.errorhandler(429)
def handle_rate_limited(exc: Any) -> Response:
    existing_response = getattr(exc, "get_response", lambda: None)()
    if isinstance(existing_response, Response):
        return existing_response
    return _build_json_response(
        {"error": "Rate limit exceeded. Retry the request after the limit window resets."},
        429,
    )


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
    set_span_error(getattr(g, "request_span", None), exc)
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
@auth_rate_limit
@require_role("staff", "admin", "auditor")
def dashboard() -> str:
    return render_template("dashboard.html", **_dashboard_context())


@app.get("/dashboard/tasks")
@auth_rate_limit
@require_role("staff", "admin", "auditor")
def dashboard_tasks() -> Response | str:
    filters, forbidden = _task_filter_args()
    if forbidden:
        return _json_error("Forbidden", 403)
    return render_template("tasks.html", **_task_dashboard_context(filters))


@app.get("/opportunities")
@api_rate_limit
@require_role("staff", "admin", "auditor")
def list_opportunities() -> Response:
    opportunities = [
        _serialize_opportunity(row)
        for row in _bot()
        .connection.execute("SELECT * FROM opportunities ORDER BY discovered_at DESC")
        .fetchall()
    ]
    return jsonify(opportunities)


@app.get("/opportunities/<signature>")
@api_rate_limit
@require_role("staff", "admin", "auditor")
def get_opportunity(signature: str) -> Response:
    opportunity = _fetch_opportunity(signature)
    application_row = (
        _bot()
        .connection.execute(
            "SELECT * FROM applications WHERE opportunity_signature = ?",
            (signature,),
        )
        .fetchone()
    )
    attempts = [
        _serialize_submission_attempt(row)
        for row in _bot()
        .connection.execute(
            """
            SELECT attempt_number, succeeded, error_message, happened_at
            FROM submission_attempts
            WHERE opportunity_signature = ?
            ORDER BY attempt_number ASC
            """,
            (signature,),
        )
        .fetchall()
    ]
    response = {
        "opportunity": opportunity,
        "application": _serialize_application(application_row) if application_row else None,
        "submission_attempts": attempts,
    }
    return jsonify(response)


@app.post("/opportunities/<signature>/submit")
@api_rate_limit
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
@api_rate_limit
@require_role("admin", "auditor")
def list_donors() -> Response:
    return jsonify(_bot().list_donors())


@app.post("/donors")
@api_rate_limit
@require_role("admin")
def upsert_donor() -> Response:
    payload = _get_request_json()
    email = str(payload.get("email", "")).strip()
    name = sanitize_user_string(
        payload.get("name", ""),
        field_name="name",
        allow_empty=False,
        html_escape=True,
    )
    opted_out = _coerce_bool(payload.get("opted_out", False), "opted_out")
    preferences = sanitize_user_mapping(payload.get("preferences", {}), field_name="preferences")
    locale = payload.get("locale")
    data_classification = payload.get("data_classification")
    field_classifications = payload.get("field_classifications")

    if not email:
        raise ValueError("Field 'email' is required.")
    # Validate email format before passing to the bot layer.
    email = _validate_email(email)
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
@api_rate_limit
@require_role("admin")
def opt_out_donor(email: str) -> Response:
    donor = _fetch_donor(email)
    if donor is None:
        return _json_error("Donor not found", 404)

    _bot().set_donor_opt_out(email, opted_out=True)
    updated_donor = _fetch_donor(email)
    return jsonify(updated_donor)


@app.get("/analytics")
@api_rate_limit
@require_role("admin", "auditor")
def get_analytics() -> Response:
    bot = _bot()
    start_at = request.args.get("start_at")
    end_at = request.args.get("end_at")
    return jsonify(
        {
            "stats": bot.get_outreach_analytics(),
            "dashboard": bot.get_analytics_dashboard_data(start_at=start_at, end_at=end_at),
        }
    )


@app.get("/analytics/funnel")
@api_rate_limit
@require_role("admin", "auditor")
def get_funnel_analytics() -> Response:
    return jsonify(
        _bot().get_funnel_analytics(
            start_at=request.args.get("start_at"),
            end_at=request.args.get("end_at"),
            connector_name=request.args.get("connector_name"),
        )
    )


@app.get("/analytics/costs")
@api_rate_limit
@require_role("admin", "auditor")
def get_cost_analytics() -> Response:
    return jsonify(
        _bot().get_connector_cost_analytics(
            start_at=request.args.get("start_at"),
            end_at=request.args.get("end_at"),
            connector_name=request.args.get("connector_name"),
        )
    )


@app.get("/analytics/attribution")
@api_rate_limit
@require_role("admin", "auditor")
def get_attribution_analytics() -> Response:
    return jsonify(
        {
            "connectors": _bot().get_source_attribution_analytics(
                start_at=request.args.get("start_at"),
                end_at=request.args.get("end_at"),
            )
        }
    )


@app.get("/analytics/anomalies")
@api_rate_limit
@require_role("admin", "auditor")
def get_analytics_anomalies() -> Response:
    return jsonify(
        _bot().detect_metric_anomalies(
            end_at=request.args.get("end_at"),
            current_window_hours=int(request.args.get("current_window_hours", "24")),
            baseline_days=int(request.args.get("baseline_days", "7")),
        )
    )


@app.get("/analytics/dashboard")
@api_rate_limit
@require_role("admin", "auditor")
def get_analytics_dashboard() -> Response:
    return jsonify(
        _bot().get_analytics_dashboard_data(
            start_at=request.args.get("start_at"),
            end_at=request.args.get("end_at"),
        )
    )


@app.get("/audit-log")
@api_rate_limit
@require_role("admin", "auditor")
def audit_log() -> Response:
    logs = [_serialize_audit_log(row) for row in _bot().connection.execute("""
        SELECT id, happened_at, action, details_json
        FROM audit_logs
        ORDER BY happened_at DESC, id DESC
        LIMIT 100
        """).fetchall()]
    return jsonify(logs)


@app.get("/settings")
@auth_rate_limit
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
        "queue_monitoring": _queue_monitoring_payload(),
        "ui_locale": _resolve_ui_locale(),
        "supported_locales": bot.list_locale_definitions(),
    }
    return render_template("settings.html", **context)


@app.get("/settings/security/mfa")
@api_rate_limit
@require_role("staff", "admin", "auditor")
def mfa_status_route() -> Response:
    role = getattr(g, "current_role", None)
    if role is None:
        return _auth_challenge()
    return jsonify({"mfa": _bot().get_auth_security_state(role)})


@app.post("/settings/security/mfa/setup")
@api_rate_limit
@require_role("staff", "admin", "auditor")
def mfa_setup_route() -> Response:
    role = getattr(g, "current_role", None)
    if role is None:
        return _auth_challenge()
    setup = _bot().begin_mfa_setup(role)
    return jsonify({"mfa_setup": setup}), 201


@app.post("/settings/security/mfa/verify")
@api_rate_limit
@require_role("staff", "admin", "auditor")
def mfa_verify_route() -> Response:
    role = getattr(g, "current_role", None)
    if role is None:
        return _auth_challenge()
    payload = _get_request_json()
    enabled = _bot().enable_mfa(
        role,
        sanitize_user_string(
            payload.get("code", ""),
            field_name="code",
            allow_empty=False,
            max_length=64,
        ),
    )
    return jsonify({"mfa": enabled})


@app.post("/settings/security/mfa/backup-codes/regenerate")
@api_rate_limit
@require_role("staff", "admin", "auditor")
def regenerate_backup_codes_route() -> Response:
    role = getattr(g, "current_role", None)
    if role is None:
        return _auth_challenge()
    payload = _get_request_json()
    code = sanitize_user_string(
        payload.get("code", ""),
        field_name="code",
        allow_empty=False,
        max_length=64,
    )
    verification = _bot().verify_mfa_code(role, code)
    if not verification["verified"]:
        raise ValueError("Invalid MFA code.")
    return jsonify({"mfa": _bot().regenerate_backup_codes(role)})


@app.get("/translations")
@auth_rate_limit
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
@api_rate_limit
@require_role("staff", "admin", "auditor")
def translation_locales() -> Response:
    return jsonify({"locales": _bot().list_locale_definitions()})


@app.get("/translations/reviews")
@api_rate_limit
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
@api_rate_limit
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
@api_rate_limit
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
@api_rate_limit
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
@api_rate_limit
@require_role("admin")
def update_search_settings() -> Response:
    payload = _get_request_json()
    keywords = _coerce_list(payload.get("keywords", []), "keywords")
    trusted_sources = _coerce_list(payload.get("trusted_sources", []), "trusted_sources")

    settings = _bot().store_search_settings(keywords=keywords, trusted_sources=trusted_sources)
    return jsonify({"search_settings": settings})


@app.post("/settings/credentials")
@api_rate_limit
@require_role("admin")
def register_credential_route() -> Response:
    payload = _get_request_json()
    alias = validate_credential_alias(str(payload.get("alias", "")))
    env_var_name = validate_env_var_name(str(payload.get("env_var_name", "")))

    _bot().register_credential(alias, env_var_name)
    return jsonify({"credentials": _bot().list_credentials()}), 201


@app.post("/settings/discover")
@api_rate_limit
@require_role("admin")
def run_discovery_now() -> Response:
    """Trigger a live search across configured donation sources.

    Demonstrates the bot's donation-search capability directly from the
    admin panel: it queries every configured portal connector, filters by
    the saved keyword/source settings (or an ad-hoc override), and persists
    any newly discovered opportunities.
    """
    payload = _get_request_json() if request.get_data(cache=True) else {}
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
@api_rate_limit
@require_role("admin")
def generate_privacy_policy() -> Response:
    payload = _get_request_json()
    output_dir = sanitize_user_string(
        payload.get(
            "output_dir", os.environ.get("PRIVACY_POLICY_OUTPUT_DIR", "generated/privacy_policies")
        ),
        field_name="output_dir",
        allow_empty=False,
        max_length=512,
    )

    generated = _bot().generate_privacy_policies(
        output_dir=output_dir,
        jurisdictions=(
            _coerce_list(payload.get("jurisdictions"), "jurisdictions")
            if payload.get("jurisdictions") is not None
            else None
        ),
        formats=(
            _coerce_list(payload.get("formats"), "formats")
            if payload.get("formats") is not None
            else None
        ),
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
@api_rate_limit
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
    name = sanitize_user_string(
        payload.get("name", ""),
        field_name="name",
        allow_empty=False,
        html_escape=True,
    )
    dry_run = _coerce_bool(payload.get("dry_run", True), "dry_run")
    subject_template = payload.get("subject_template")
    body_template = payload.get("body_template")
    locale = payload.get("locale")

    if not email:
        raise ValueError("Field 'email' is required.")
    email = _validate_email(email)

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
            subject_template=sanitize_user_string(
                subject_template,
                field_name="subject_template",
                allow_empty=False,
                html_escape=True,
            ),
            body_template=sanitize_user_string(
                body_template,
                field_name="body_template",
                allow_empty=False,
                multiline=True,
                html_escape=True,
            ),
            sender=sender,
            locale=None if locale is None else str(locale),
        )
    result["dry_run"] = dry_run
    return jsonify(result), 201


@app.get("/tasks")
@app.get("/task-directory")
@api_rate_limit
@require_role("staff", "admin", "auditor")
def list_tasks_route() -> Response:
    current_role = getattr(g, "current_role", None)
    assigned_to = request.args.get("assigned_to") or request.args.get("assignee")
    if current_role not in {"admin", "auditor"}:
        normalized_assignee = str(assigned_to or "").strip().lower()
        if normalized_assignee and normalized_assignee != current_role:
            return _json_error("Forbidden", 403)
        assigned_to = current_role
    tasks = _bot().list_tasks(
        assignee=assigned_to,
        assignee_email=request.args.get("assignee_email"),
        status=request.args.get("status"),
        due_date_before=request.args.get("due_before") or request.args.get("due_date_before"),
        due_date_after=request.args.get("due_after") or request.args.get("due_date_after"),
        source=request.args.get("source"),
        sort=request.args.get("sort"),
        sort_by=request.args.get("sort_by"),
        sort_order=request.args.get("sort_order"),
        viewer_email=request.args.get("viewer_email"),
    )
    return jsonify(tasks)


@app.post("/tasks")
@app.post("/task-directory")
@api_rate_limit
@require_role("admin")
def create_task_route() -> Response:
    payload = _get_request_json()
    title = str(payload.get("title", "")).strip()
    assigned_to = str(payload.get("assignee", payload.get("assigned_to", ""))).strip().lower()
    due_date = payload.get("due_date")
    if not title:
        raise ValueError("Field 'title' is required.")
    if not assigned_to:
        raise ValueError("Field 'assignee' is required.")
    if assigned_to not in ROLE_PASSWORD_ENV_VARS:
        raise ValueError(f"Field 'assignee' must be one of {sorted(ROLE_PASSWORD_ENV_VARS)}.")
    if due_date in (None, ""):
        raise ValueError("Field 'due_date' is required.")

    task = _bot().create_task(
        title=title,
        assignee=assigned_to,
        description=str(payload.get("description", "")),
        status=str(payload.get("status", "pending")),
        due_date=due_date,
        external_id=payload.get("external_id"),
        source=str(payload.get("source", "manual")),
        attributed_connector=payload.get("attributed_connector"),
        opportunity_signature=payload.get("opportunity_signature"),
        assignee_email=payload.get("assignee_email"),
        assignee_name=payload.get("assignee_name"),
        sender=_task_assignment_sender(),
    )
    return jsonify({"task": task, "notification": task.get("assignment_notification")}), 201


@app.put("/tasks/<int:task_id>")
@api_rate_limit
@require_role("admin")
def update_task_route(task_id: int) -> Response:
    payload = _get_request_json()
    if not payload:
        raise ValueError("Request body must include at least one task field to update.")
    assignee = payload.get("assignee", payload.get("assigned_to"))
    if assignee not in (None, "") and str(assignee).strip().lower() not in ROLE_PASSWORD_ENV_VARS:
        raise ValueError(f"Field 'assignee' must be one of {sorted(ROLE_PASSWORD_ENV_VARS)}.")
    task = _bot().update_task(
        task_id,
        title=payload.get("title"),
        description=payload.get("description"),
        assignee=assignee,
        status=payload.get("status"),
        due_date=payload.get("due_date"),
        attributed_connector=payload.get("attributed_connector"),
        opportunity_signature=payload.get("opportunity_signature"),
    )
    return jsonify({"task": task})


@app.get("/tasks/<int:task_id>")
@app.get("/task-directory/<int:task_id>")
@api_rate_limit
@require_role("staff", "admin", "auditor")
def get_task_route(task_id: int) -> Response:
    task = _bot().get_task(task_id, viewer_email=request.args.get("viewer_email"))
    current_role = getattr(g, "current_role", None)
    if current_role not in {"admin", "auditor"} and task["assigned_to"] != current_role:
        return _json_error("Forbidden", 403)
    payload = {"task": task}
    payload.update(task)
    return jsonify(payload)


@app.get("/api/tasks/export")
@export_rate_limit
@require_role("admin", "auditor")
def export_tasks_route() -> Response:
    tasks = _bot().list_tasks(
        assignee=request.args.get("assigned_to") or request.args.get("assignee"),
        status=request.args.get("status"),
        due_date_before=request.args.get("due_before") or request.args.get("due_date_before"),
        due_date_after=request.args.get("due_after") or request.args.get("due_date_after"),
        source=request.args.get("source"),
        sort=request.args.get("sort"),
        sort_by=request.args.get("sort_by"),
        sort_order=request.args.get("sort_order"),
        assignee_email=request.args.get("assignee_email"),
        viewer_email=request.args.get("viewer_email"),
    )
    return jsonify({"tasks": tasks, "count": len(tasks)})


@app.get("/api/exports")
@export_rate_limit
@require_role("admin", "auditor")
def list_exports_route() -> Response:
    audits = [
        _serialize_audit_log(entry)
        for entry in _bot().list_audit_logs(limit=20)
        if entry.get("action") in {"data_warehouse_exported", "data_retention_enforced"}
    ]
    return jsonify(
        {"schedule": _export_schedule_snapshot(), "exports": audits, "count": len(audits)}
    )


@app.post("/api/exports")
@export_rate_limit
@require_role("admin", "auditor")
def create_export_route() -> Response:
    payload = _get_request_json()
    datasets = payload.get("datasets")
    if datasets is not None and not isinstance(datasets, list):
        raise ValueError("Field 'datasets' must be a list of dataset names.")
    export_format = str(payload.get("format", "json")).strip().lower()
    output_dir = str(
        payload.get("output_dir", os.environ.get("DATA_EXPORT_OUTPUT_DIR", "generated/exports"))
    ).strip()
    if not output_dir:
        raise ValueError("Field 'output_dir' must not be empty.")
    archive = _coerce_bool(payload.get("archive", True), "archive")
    async_requested = _coerce_bool(payload.get("async", False), "async")
    if async_requested:
        status_code, result = dispatch_export(
            datasets=None if datasets is None else [str(dataset) for dataset in datasets],
            export_format=export_format,
            output_dir=output_dir,
            archive=archive,
            db_path=os.environ.get("BOT_DB_PATH", "funding_bot.db"),
        )
        return jsonify(result), status_code
    result = _bot().export_data_warehouse(
        datasets=None if datasets is None else [str(dataset) for dataset in datasets],
        export_format=export_format,
        output_dir=output_dir,
        archive=archive,
    )
    return jsonify(result), 201


@app.post("/api/tasks/sync")
@api_rate_limit
@require_role("admin")
def sync_tasks_route() -> Response:
    payload = _get_request_json()
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("Field 'tasks' must be a list of task objects.")
    synced = _bot().sync_tasks(tasks, default_source=str(payload.get("source", "external_sync")))
    return jsonify({"tasks": synced, "count": len(synced)})


@app.post("/api/tasks/import")
@api_rate_limit
@require_role("admin")
def import_tasks_route() -> Response:
    csv_text = _read_task_import_csv()
    source = request.args.get("source") or request.form.get("source") or "csv_import"
    imported = _bot().import_tasks_from_csv(csv_text, default_source=str(source))
    return jsonify({"tasks": imported, "count": len(imported)}), 201


@app.post("/tasks/<int:task_id>/assign")
@app.post("/tasks/<int:task_id>/assignment")
@app.post("/task-directory/<int:task_id>/assignment")
@api_rate_limit
@require_role("admin")
def assign_task_route(task_id: int) -> Response:
    payload = _get_request_json()
    assigned_to = str(payload.get("assigned_to", "")).strip().lower()
    if not assigned_to:
        raise ValueError("Field 'assigned_to' is required.")
    if assigned_to not in ROLE_PASSWORD_ENV_VARS:
        raise ValueError(f"Field 'assigned_to' must be one of {sorted(ROLE_PASSWORD_ENV_VARS)}.")
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
@api_rate_limit
@require_role("staff", "admin", "auditor")
def list_task_comments_route(task_id: int) -> Response:
    payload = _bot().list_task_comments(task_id, viewer_email=request.args.get("viewer_email"))
    payload["comments"] = [_serialize_task_comment(comment) for comment in payload["comments"]]
    return jsonify(payload)


@app.post("/tasks/<int:task_id>/comments")
@api_rate_limit
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
@api_rate_limit
@require_role("staff", "admin")
def update_task_comment_route(task_id: int, comment_id: int) -> Response:
    payload = _get_request_json()
    content = str(payload.get("content", "")).strip()
    if not content:
        raise ValueError("Field 'content' is required.")
    comment = _bot().update_task_comment(task_id, comment_id, content=content)
    return jsonify(comment)


@app.delete("/tasks/<int:task_id>/comments/<int:comment_id>")
@api_rate_limit
@require_role("staff", "admin")
def delete_task_comment_route(task_id: int, comment_id: int) -> Response:
    _bot().delete_task_comment(task_id, comment_id)
    return Response(status=204)


@app.post("/tasks/<int:task_id>/comments/read")
@api_rate_limit
@require_role("staff", "admin", "auditor")
def mark_task_comments_read_route(task_id: int) -> Response:
    payload = _get_request_json()
    reader_email = str(payload.get("reader_email", "")).strip()
    if not reader_email:
        raise ValueError("Field 'reader_email' is required.")
    result = _bot().mark_task_comments_read(task_id, reader_email=reader_email)
    return jsonify(result)


@app.post("/tasks/<int:task_id>/status")
@api_rate_limit
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
    payload = _build_health_payload("health")
    return jsonify(payload), _status_code_for_health(payload["status"])


@app.get("/ready")
def ready() -> Response:
    payload = _build_health_payload("ready")
    return jsonify(payload), _status_code_for_health(payload["status"])


@app.get("/health/queue")
def queue_health() -> Response:
    snapshot = _get_queue_health_snapshot()
    status_code = 200 if snapshot["status"] in {"ok", "disabled"} else 503
    return jsonify(snapshot), status_code


@app.get("/monitoring/queue")
@api_rate_limit
@require_role("staff", "admin", "auditor")
def queue_monitoring() -> Response:
    return jsonify(_queue_monitoring_payload())


@app.get("/health/database")
def database_health() -> Response:
    bot = _bot()
    payload = bot.get_database_pool_metrics()
    payload["queries"] = bot.get_database_query_metrics()
    payload["indexes"] = bot.get_database_index_metrics()
    return jsonify(payload)


@app.get("/health/cache")
def cache_health() -> Response:
    return jsonify(_bot().get_cache_metrics()["health"])


@app.get("/api/slo")
@api_rate_limit
@require_role("admin", "auditor")
def slo_dashboard() -> Response:
    return jsonify(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tracing": tracing_configuration_summary(),
            "slos": _bot().get_slo_summary(),
        }
    )


@app.get("/metrics")
@api_rate_limit
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
    task_counts = _mapping_or_default(bot.get_task_status_counts(), {})
    raw_queue_metrics: dict[str, Any] = getattr(bot, "get_queue_metrics", lambda: {})()
    database_metrics = _mapping_or_default(
        getattr(bot, "get_database_pool_metrics", lambda: {})(),
        {
            "size": 0,
            "checked_in": 0,
            "checked_out": 0,
            "overflow": 0,
            "connects": 0,
            "checkouts": 0,
            "checkins": 0,
            "invalidations": 0,
        },
    )
    query_metrics = _mapping_or_default(
        getattr(bot, "get_database_query_metrics", lambda: {})(),
        {
            "slow_query_threshold_seconds": 0.25,
            "buckets": [],
            "summary": {},
            "statements": {},
        },
    )
    cache_metrics = _mapping_or_default(
        getattr(bot, "get_cache_metrics", lambda: {})(),
        {"namespaces": {}},
    )
    index_metrics = _mapping_or_default(
        getattr(bot, "get_database_index_metrics", lambda: {})(),
        {
            "summary": {"expected": 0, "present": 0},
            "indexes": [],
            "query_plans": [],
            "connector_responses": [],
        },
    )
    queue_metrics = {
        "running": 0,
        "completed": 0,
        "failed": 0,
        "cancelled": 0,
        "retries_scheduled": 0,
        "dead_lettered": 0,
        "duplicate_preventions": 0,
        "duration_seconds_sum": 0.0,
        "duration_seconds_count": 0,
        "duration_seconds_average": 0.0,
        "duration_seconds_max": 0.0,
    }
    if isinstance(raw_queue_metrics, dict):
        for key in queue_metrics:
            caster: Callable[[Any], Any] = float if "seconds" in key else int
            queue_metrics[key] = caster(raw_queue_metrics.get(key, 0) or 0)
    queue_health = _get_queue_health_snapshot()
    queue_status_value = 1 if queue_health["status"] == "ok" else 0
    health_metrics = _health_check_metrics_snapshot()
    connector_metrics = _string_iterable_or_empty(
        FundingBot.render_connector_metrics_prometheus()
    )
    batch_metrics = _string_iterable_or_empty(FundingBot.render_batch_metrics_prometheus())
    task_assignments = conn.execute("""
        SELECT assigned_to AS assignee, COUNT(*) AS total
        FROM tasks
        GROUP BY assigned_to
        ORDER BY assigned_to ASC
        """).fetchall()
    slo_metrics = _string_iterable_or_empty(
        getattr(bot, "render_slo_metrics_prometheus", lambda: [])()
    )

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
        *connector_metrics,
        *batch_metrics,
        *slo_metrics,
        "# HELP funding_bot_tasks_total Total collaboration tasks",
        "# TYPE funding_bot_tasks_total gauge",
        f"funding_bot_tasks_total {tasks_total}",
        "# HELP funding_bot_uptime_seconds Seconds since the web process started",
        "# TYPE funding_bot_uptime_seconds gauge",
        f"funding_bot_uptime_seconds {uptime_seconds:.3f}",
        "# HELP funding_bot_db_pool_size SQLAlchemy connection pool size",
        "# TYPE funding_bot_db_pool_size gauge",
        f"funding_bot_db_pool_size {database_metrics['size']}",
        "# HELP funding_bot_db_pool_checked_in SQLAlchemy connections currently idle in the pool",
        "# TYPE funding_bot_db_pool_checked_in gauge",
        f"funding_bot_db_pool_checked_in {database_metrics['checked_in']}",
        "# HELP funding_bot_db_pool_checked_out SQLAlchemy connections currently checked out",
        "# TYPE funding_bot_db_pool_checked_out gauge",
        f"funding_bot_db_pool_checked_out {database_metrics['checked_out']}",
        "# HELP funding_bot_db_pool_overflow SQLAlchemy overflow connections currently in use",
        "# TYPE funding_bot_db_pool_overflow gauge",
        f"funding_bot_db_pool_overflow {database_metrics['overflow']}",
        "# HELP funding_bot_db_pool_connects_total SQLAlchemy physical database connections opened",
        "# TYPE funding_bot_db_pool_connects_total counter",
        f"funding_bot_db_pool_connects_total {database_metrics['connects']}",
        "# HELP funding_bot_db_pool_checkouts_total SQLAlchemy pool checkout events",
        "# TYPE funding_bot_db_pool_checkouts_total counter",
        f"funding_bot_db_pool_checkouts_total {database_metrics['checkouts']}",
        "# HELP funding_bot_db_pool_checkins_total SQLAlchemy pool checkin events",
        "# TYPE funding_bot_db_pool_checkins_total counter",
        f"funding_bot_db_pool_checkins_total {database_metrics['checkins']}",
        "# HELP funding_bot_db_pool_invalidations_total SQLAlchemy pool invalidation events",
        "# TYPE funding_bot_db_pool_invalidations_total counter",
        f"funding_bot_db_pool_invalidations_total {database_metrics['invalidations']}",
        "# HELP funding_bot_db_indexes_expected_total Database indexes expected by the application",
        "# TYPE funding_bot_db_indexes_expected_total gauge",
        f"funding_bot_db_indexes_expected_total {int(index_metrics['summary'].get('expected', 0) or 0)}",
        "# HELP funding_bot_db_indexes_present_total Database indexes currently present",
        "# TYPE funding_bot_db_indexes_present_total gauge",
        f"funding_bot_db_indexes_present_total {int(index_metrics['summary'].get('present', 0) or 0)}",
        *_render_query_metrics_prometheus(query_metrics),
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
        "# HELP funding_bot_health_checks_total Total /health and /ready probes performed",
        "# TYPE funding_bot_health_checks_total counter",
        *[
            f'funding_bot_health_checks_total{{endpoint="{endpoint}"}} {metrics["checks_performed"]}'
            for endpoint, metrics in sorted(health_metrics["endpoints"].items())
        ],
        "# HELP funding_bot_health_failures_total Total failing /health and /ready probes",
        "# TYPE funding_bot_health_failures_total counter",
        *[
            f'funding_bot_health_failures_total{{endpoint="{endpoint}"}} {metrics["failures"]}'
            for endpoint, metrics in sorted(health_metrics["endpoints"].items())
        ],
        "# HELP funding_bot_health_component_checks_total Total component checks performed by health endpoints",
        "# TYPE funding_bot_health_component_checks_total counter",
        *[
            f'funding_bot_health_component_checks_total{{component="{component}"}} {metrics["checks_performed"]}'
            for component, metrics in sorted(health_metrics["components"].items())
        ],
        "# HELP funding_bot_health_component_failures_total Total component health check failures",
        "# TYPE funding_bot_health_component_failures_total counter",
        *[
            f'funding_bot_health_component_failures_total{{component="{component}"}} {metrics["failures"]}'
            for component, metrics in sorted(health_metrics["components"].items())
        ],
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
        f"funding_bot_queue_task_retries_total {queue_metrics.get('retries_scheduled', 0)}",
        "# HELP funding_bot_dead_letter_queue_total Queue task runs stored in the dead-letter queue",
        "# TYPE funding_bot_dead_letter_queue_total gauge",
        f"funding_bot_dead_letter_queue_total {queue_metrics.get('dead_lettered', 0)}",
        "# HELP funding_bot_queue_duplicate_preventions_total Duplicate queue executions prevented by idempotency keys",
        "# TYPE funding_bot_queue_duplicate_preventions_total counter",
        f"funding_bot_queue_duplicate_preventions_total {queue_metrics['duplicate_preventions']}",
        "# HELP funding_bot_queue_task_duration_seconds_sum Total completed queue task runtime in seconds",
        "# TYPE funding_bot_queue_task_duration_seconds_sum counter",
        f"funding_bot_queue_task_duration_seconds_sum {queue_metrics['duration_seconds_sum']:.6f}",
        "# HELP funding_bot_queue_task_duration_seconds_count Completed queue tasks included in runtime metrics",
        "# TYPE funding_bot_queue_task_duration_seconds_count counter",
        f"funding_bot_queue_task_duration_seconds_count {int(queue_metrics['duration_seconds_count'])}",
        "# HELP funding_bot_queue_task_duration_seconds_average Average completed queue task runtime in seconds",
        "# TYPE funding_bot_queue_task_duration_seconds_average gauge",
        f"funding_bot_queue_task_duration_seconds_average {queue_metrics['duration_seconds_average']:.6f}",
        "# HELP funding_bot_queue_task_duration_seconds_max Longest completed queue task runtime in seconds",
        "# TYPE funding_bot_queue_task_duration_seconds_max gauge",
        f"funding_bot_queue_task_duration_seconds_max {queue_metrics['duration_seconds_max']:.6f}",
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
            "# HELP funding_bot_db_index_present Whether an expected database index is present (1=yes, 0=no)",
            "# TYPE funding_bot_db_index_present gauge",
        ]
    )
    for index_row in index_metrics.get("indexes", []):
        lines.append(
            "funding_bot_db_index_present{index_name="
            + _prometheus_label_value(str(index_row.get("name", "")))
            + ",table="
            + _prometheus_label_value(str(index_row.get("table", "")))
            + "} "
            + str(1 if index_row.get("present") else 0)
        )
    lines.extend(
        [
            "# HELP funding_bot_db_query_plan_uses_index Whether the representative query plan uses an index (1=yes, 0=no)",
            "# TYPE funding_bot_db_query_plan_uses_index gauge",
        ]
    )
    for plan in index_metrics.get("query_plans", []):
        lines.append(
            "funding_bot_db_query_plan_uses_index{query_name="
            + _prometheus_label_value(str(plan.get("name", "")))
            + "} "
            + str(1 if plan.get("uses_index") else 0)
        )
    lines.extend(
        [
            "# HELP funding_bot_connector_response_cache_total Connector cache entries grouped by source_status",
            "# TYPE funding_bot_connector_response_cache_total gauge",
        ]
    )
    for connector_response in index_metrics.get("connector_responses", []):
        lines.append(
            "funding_bot_connector_response_cache_total{source_status="
            + _prometheus_label_value(str(connector_response.get("source_status", "")))
            + "} "
            + str(int(connector_response.get("total", 0) or 0))
        )
    lines.extend(
        [
            "# HELP funding_bot_tasks_assigned_total Tasks assigned per dashboard role",
            "# TYPE funding_bot_tasks_assigned_total gauge",
        ]
    )
    for row in task_assignments:
        lines.append(
            f'funding_bot_tasks_assigned_total{{assigned_to="{row["assignee"]}"}} {row["total"]}'
        )
    lines.extend(
        [
            "# HELP funding_bot_cache_hits_total Cache hits by namespace",
            "# TYPE funding_bot_cache_hits_total counter",
            "# HELP funding_bot_cache_misses_total Cache misses by namespace",
            "# TYPE funding_bot_cache_misses_total counter",
            "# HELP funding_bot_cache_sets_total Cache writes by namespace",
            "# TYPE funding_bot_cache_sets_total counter",
            "# HELP funding_bot_cache_invalidations_total Cache invalidations by namespace",
            "# TYPE funding_bot_cache_invalidations_total counter",
            "# HELP funding_bot_cache_entries Cache entries by namespace",
            "# TYPE funding_bot_cache_entries gauge",
            "# HELP funding_bot_cache_ttl_seconds Cache TTL configuration by namespace",
            "# TYPE funding_bot_cache_ttl_seconds gauge",
        ]
    )
    for namespace, namespace_metrics in sorted(cache_metrics.get("namespaces", {}).items()):
        labels = f'cache="{namespace}",backend="{namespace_metrics.get("backend", "memory")}"'
        lines.extend(
            [
                f"funding_bot_cache_hits_total{{{labels}}} {int(namespace_metrics.get('hits', 0))}",
                f"funding_bot_cache_misses_total{{{labels}}} {int(namespace_metrics.get('misses', 0))}",
                f"funding_bot_cache_sets_total{{{labels}}} {int(namespace_metrics.get('sets', 0))}",
                f"funding_bot_cache_invalidations_total{{{labels}}} {int(namespace_metrics.get('invalidations', 0))}",
                f"funding_bot_cache_entries{{{labels}}} {int(namespace_metrics.get('size', 0))}",
                f"funding_bot_cache_ttl_seconds{{{labels}}} {float(namespace_metrics.get('ttl_seconds', 0.0))}",
            ]
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
@api_rate_limit
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
    message = sanitize_user_string(
        payload.get("message", ""),
        field_name="message",
        allow_empty=False,
        multiline=True,
        html_escape=True,
        max_length=MAX_FEEDBACK_MESSAGE_LENGTH,
    )
    contact = str(payload.get("contact", "")).strip() or None

    allowed_categories = {"feature_request", "bug_report", "general"}
    if category not in allowed_categories:
        raise ValueError(f"Field 'category' must be one of {sorted(allowed_categories)}.")
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
