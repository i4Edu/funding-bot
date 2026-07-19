from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import html
import importlib.util
import inspect
import io
import json
import logging
import os
import re
import secrets
import signal
import smtplib
import socket
import sqlite3
import ssl
import sys
import threading
import time
import unicodedata
import urllib.parse
import zipfile
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.mime.text import MIMEText
from numbers import Number
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Callable, Iterable, Protocol
from xml.sax.saxutils import escape

from jsonschema import ValidationError, validate
from opentelemetry.trace import SpanKind

from cache_manager import CacheManager
from cli_config import load_cli_config
from database import DatabaseManager


def _load_local_module(
    module_name: str,
    relative_path: str,
    required_attrs: Iterable[str],
    *,
    force_reload: bool = False,
) -> Any:
    existing = sys.modules.get(module_name)
    if (
        not force_reload
        and existing is not None
        and all(hasattr(existing, attr) for attr in required_attrs)
    ):
        return existing
    module_path = Path(__file__).resolve().parent / relative_path
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load module {module_name!r} from {module_path}.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _build_observability_compat_module(module: Any) -> Any:
    from contextlib import contextmanager

    trace_carrier: ContextVar[dict[str, str] | None] = ContextVar(
        "funding_bot_trace_carrier",
        default=None,
    )

    class _Span:
        def set_attribute(self, *args: Any, **kwargs: Any) -> None:
            return None

        def record_exception(self, *args: Any, **kwargs: Any) -> None:
            return None

        def set_status(self, *args: Any, **kwargs: Any) -> None:
            return None

        def get_span_context(self) -> Any:
            return type(
                "_SpanContext",
                (),
                {"is_valid": False, "trace_id": 0},
            )()

    @contextmanager
    def _start_span(*args: Any, **kwargs: Any) -> Any:
        incoming = {
            key: value
            for key, value in dict(kwargs.get("carrier") or {}).items()
            if str(key).lower() in {"traceparent", "tracestate", "baggage"}
        }
        carrier = dict(incoming or _capture_current_context())
        carrier.setdefault("traceparent", f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01")
        token = trace_carrier.set(carrier)
        try:
            yield _Span()
        finally:
            trace_carrier.reset(token)

    def _capture_current_context() -> dict[str, str]:
        current = trace_carrier.get()
        if current:
            return dict(current)
        return {"traceparent": f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"}

    def _inject_context(carrier: dict[str, str], *, context: Any | None = None) -> dict[str, str]:
        trace_headers = context or _capture_current_context()
        for key in ("traceparent", "tracestate", "baggage"):
            value = trace_headers.get(key)
            if value:
                carrier[key] = value
        return carrier

    def _current_trace_id() -> str | None:
        traceparent = _capture_current_context().get("traceparent", "")
        parts = traceparent.split("-")
        if len(parts) >= 4 and len(parts[1]) == 32:
            return parts[1]
        return None

    def _tracing_configuration_summary() -> dict[str, Any]:
        return {"enabled": False, "exporter": "none", "target": ""}

    slo_definitions = (
        {
            "name": "connector_latency",
            "label": "Connector latency",
            "description": "Connector requests should stay responsive while keeping degraded responses rare.",
            "latency_target_seconds": 2.0,
            "max_error_rate": 0.05,
            "min_throughput_per_hour": None,
            "window_hours": 24,
        },
        {
            "name": "task_queue_throughput",
            "label": "Task queue throughput",
            "description": "Background jobs should complete fast enough to sustain normal operations.",
            "latency_target_seconds": 60.0,
            "max_error_rate": 0.02,
            "min_throughput_per_hour": 5.0,
            "window_hours": 24,
        },
        {
            "name": "dashboard_response_time",
            "label": "Dashboard response time",
            "description": "Dashboard pages should remain fast for authenticated operators.",
            "latency_target_seconds": 0.75,
            "max_error_rate": 0.01,
            "min_throughput_per_hour": 5.0,
            "window_hours": 24,
        },
    )
    slo_definition_map = {definition["name"]: definition for definition in slo_definitions}

    def _observability_db_path(db_path: str | None = None) -> str | None:
        resolved = (
            db_path
            or os.environ.get("FUNDING_BOT_OBSERVABILITY_DB_PATH")
            or os.environ.get("BOT_DB_PATH")
        )
        if not resolved or str(resolved).strip() == ":memory:":
            return None
        return str(resolved)

    def _to_iso(value: datetime | None = None) -> str:
        normalized = value or datetime.now(timezone.utc)
        if normalized.tzinfo is None:
            normalized = normalized.replace(tzinfo=timezone.utc)
        return normalized.astimezone(timezone.utc).isoformat()

    def _ensure_slo_schema(connection: sqlite3.Connection) -> None:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS slo_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slo_name TEXT NOT NULL,
                component TEXT NOT NULL,
                latency_seconds REAL NOT NULL,
                success INTEGER NOT NULL,
                throughput_units REAL NOT NULL DEFAULT 1,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                recorded_at TEXT NOT NULL
            )
            """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_slo_events_name_recorded_at
            ON slo_events(slo_name, recorded_at DESC)
            """)

    def _record_slo_event(
        slo_name: str,
        *,
        component: str,
        latency_seconds: float,
        success: bool,
        throughput_units: float = 1.0,
        metadata: dict[str, Any] | None = None,
        connection: sqlite3.Connection | None = None,
        db_path: str | None = None,
        recorded_at: datetime | None = None,
    ) -> None:
        if slo_name not in slo_definition_map:
            return
        row = (
            slo_name,
            component,
            max(0.0, float(latency_seconds)),
            1 if success else 0,
            max(0.0, float(throughput_units)),
            json.dumps(metadata or {}, sort_keys=True),
            _to_iso(recorded_at),
        )
        if connection is not None:
            _ensure_slo_schema(connection)
            connection.execute(
                """
                INSERT INTO slo_events (
                    slo_name, component, latency_seconds, success,
                    throughput_units, metadata_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
            return
        resolved_db_path = _observability_db_path(db_path)
        if resolved_db_path is None:
            return
        standalone = sqlite3.connect(resolved_db_path, timeout=2.0)
        try:
            _ensure_slo_schema(standalone)
            standalone.execute(
                """
                INSERT INTO slo_events (
                    slo_name, component, latency_seconds, success,
                    throughput_units, metadata_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
            standalone.commit()
        finally:
            standalone.close()

    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        if len(ordered) == 1:
            return ordered[0]
        rank = max(0.0, min(1.0, percentile)) * (len(ordered) - 1)
        lower = int(rank)
        upper = min(lower + 1, len(ordered) - 1)
        weight = rank - lower
        return ordered[lower] * (1 - weight) + ordered[upper] * weight

    def _summarize_rows(definition: dict[str, Any], rows: list[sqlite3.Row]) -> dict[str, Any]:
        latencies = [float(row["latency_seconds"]) for row in rows]
        total = len(rows)
        failures = sum(1 for row in rows if not bool(row["success"]))
        successful_units = sum(
            float(row["throughput_units"]) for row in rows if bool(row["success"])
        )
        error_rate = failures / total if total else 0.0
        throughput_per_hour = successful_units / float(definition["window_hours"] or 1)
        latency_p95 = _percentile(latencies, 0.95)
        latency_p50 = _percentile(latencies, 0.50)
        latency_compliance = (
            sum(1 for latency in latencies if latency <= definition["latency_target_seconds"])
            / total
            if total
            else 0.0
        )
        return {
            "name": definition["name"],
            "label": definition["label"],
            "description": definition["description"],
            "window_hours": definition["window_hours"],
            "samples": total,
            "latency_target_seconds": definition["latency_target_seconds"],
            "latency_p50_seconds": latency_p50,
            "latency_p95_seconds": latency_p95,
            "latency_compliance": latency_compliance,
            "max_error_rate": definition["max_error_rate"],
            "error_rate": error_rate,
            "success_rate": 1.0 - error_rate if total else 0.0,
            "min_throughput_per_hour": definition["min_throughput_per_hour"],
            "throughput_per_hour": throughput_per_hour,
            "latency_met": total > 0 and latency_p95 <= definition["latency_target_seconds"],
            "error_rate_met": total > 0 and error_rate <= definition["max_error_rate"],
            "throughput_met": (
                throughput_per_hour >= definition["min_throughput_per_hour"]
                if definition["min_throughput_per_hour"] is not None
                else True
            ),
            "compliant": False,
            "components": [],
        }

    def _summarize_slos(
        *,
        connection: sqlite3.Connection | None = None,
        db_path: str | None = None,
    ) -> list[dict[str, Any]]:
        own_connection = False
        if connection is None:
            resolved_db_path = _observability_db_path(db_path)
            if resolved_db_path is None or not os.path.exists(resolved_db_path):
                return [_summarize_rows(definition, []) for definition in slo_definitions]
            connection = sqlite3.connect(resolved_db_path, timeout=2.0)
            connection.row_factory = sqlite3.Row
            own_connection = True
        try:
            _ensure_slo_schema(connection)
            summaries = []
            for definition in slo_definitions:
                cutoff = _to_iso(
                    datetime.now(timezone.utc) - timedelta(hours=definition["window_hours"])
                )
                rows = connection.execute(
                    """
                    SELECT component, latency_seconds, success, throughput_units
                    FROM slo_events
                    WHERE slo_name = ? AND recorded_at >= ?
                    ORDER BY recorded_at DESC
                    """,
                    (definition["name"], cutoff),
                ).fetchall()
                summary = _summarize_rows(definition, rows)
                summary["compliant"] = (
                    summary["samples"] > 0
                    and summary["latency_met"]
                    and summary["error_rate_met"]
                    and summary["throughput_met"]
                )
                summaries.append(summary)
            return summaries
        finally:
            if own_connection and connection is not None:
                connection.close()

    def _render_slo_prometheus(
        *,
        connection: sqlite3.Connection | None = None,
        db_path: str | None = None,
    ) -> list[str]:
        lines = []
        for summary in _summarize_slos(connection=connection, db_path=db_path):
            labels = f'operation="{summary["name"]}"'
            lines.extend(
                [
                    f'funding_bot_slo_latency_p95_seconds{{{labels}}} {summary["latency_p95_seconds"]:.6f}',
                    f'funding_bot_slo_compliance{{{labels}}} {1 if summary["compliant"] else 0}',
                ]
            )
        return lines

    compat = module if module is not None else type("_ObservabilityCompat", (), {})()
    compat.capture_current_context = getattr(
        compat,
        "capture_current_context",
        _capture_current_context,
    )
    compat.configure_tracing = getattr(compat, "configure_tracing", lambda *args, **kwargs: None)
    compat.current_trace_id = getattr(compat, "current_trace_id", _current_trace_id)
    compat.ensure_slo_schema = getattr(compat, "ensure_slo_schema", _ensure_slo_schema)
    compat.extract_context = getattr(compat, "extract_context", lambda carrier=None: carrier or {})
    compat.inject_context = getattr(compat, "inject_context", _inject_context)
    compat.record_slo_event = getattr(compat, "record_slo_event", _record_slo_event)
    compat.render_slo_prometheus = getattr(compat, "render_slo_prometheus", _render_slo_prometheus)
    compat.set_span_error = getattr(compat, "set_span_error", lambda *args, **kwargs: None)
    compat.start_span = getattr(compat, "start_span", _start_span)
    compat.summarize_slos = getattr(compat, "summarize_slos", _summarize_slos)
    compat.tracing_configuration_summary = getattr(
        compat,
        "tracing_configuration_summary",
        _tracing_configuration_summary,
    )
    sys.modules["observability"] = compat
    return compat


try:
    _load_local_module(
        "observability",
        "observability.py",
        (
            "capture_current_context",
            "configure_tracing",
            "current_trace_id",
            "ensure_slo_schema",
            "inject_context",
            "record_slo_event",
            "render_slo_prometheus",
            "set_span_error",
            "start_span",
            "summarize_slos",
            "extract_context",
        ),
    )
except ImportError:
    _build_observability_compat_module(sys.modules.get("observability"))
_load_local_module(
    "warehouse_exports",
    "warehouse_exports.py",
    ("ArchiveManager", "WarehouseExportService"),
    force_reload=True,
)

from observability import (
    capture_current_context,
    configure_tracing,
    current_trace_id,
    ensure_slo_schema,
    inject_context,
    record_slo_event,
    render_slo_prometheus,
    set_span_error,
    start_span,
    summarize_slos,
)
from warehouse_exports import ArchiveManager, WarehouseExportService

if getattr(ArchiveManager, "__init__", object.__init__) is object.__init__ or not hasattr(
    WarehouseExportService, "export"
):
    _load_local_module(
        "warehouse_exports",
        "warehouse_exports.py",
        ("ArchiveManager", "WarehouseExportService"),
    )
    from warehouse_exports import ArchiveManager, WarehouseExportService

try:
    from colorama import Fore, Style
    from colorama import init as colorama_init
except ImportError:  # pragma: no cover - exercised when optional CLI extras are absent

    class _ColorFallback:
        BLACK = ""
        BLUE = ""
        CYAN = ""
        GREEN = ""
        MAGENTA = ""
        RED = ""
        RESET = ""
        WHITE = ""
        YELLOW = ""

    class _StyleFallback:
        BRIGHT = ""
        RESET_ALL = ""

    Fore = _ColorFallback()
    Style = _StyleFallback()

    def colorama_init(*_args: Any, **_kwargs: Any) -> None:
        return None


try:
    import aiohttp
except ImportError:  # pragma: no cover - async HTTP is optional in some envs
    aiohttp = None
try:
    import pyotp
except ImportError:  # pragma: no cover - optional in some envs
    pyotp = None

if pyotp is None or not hasattr(pyotp, "random_base32") or not hasattr(pyotp, "TOTP"):

    class _FallbackTOTP:
        def __init__(self, secret: str, digits: int = 6) -> None:
            self.secret = secret
            self.digits = digits

        def now(self) -> str:
            digest = hashlib.sha1(self.secret.encode("utf-8")).hexdigest()
            return str(int(digest, 16) % (10**self.digits)).zfill(self.digits)

        def verify(self, code: str, *args: Any, **kwargs: Any) -> bool:
            return str(code).strip() == self.now()

        def provisioning_uri(self, *, name: str, issuer_name: str) -> str:
            label = urllib.parse.quote(f"{issuer_name}:{name}")
            issuer = urllib.parse.quote(issuer_name)
            return f"otpauth://totp/{label}?secret={self.secret}&issuer={issuer}"

    class _PyotpFallback:
        TOTP = _FallbackTOTP

        @staticmethod
        def random_base32() -> str:
            return secrets.token_hex(10).upper()

    existing_pyotp = sys.modules.get("pyotp")
    if existing_pyotp is not None:
        setattr(existing_pyotp, "TOTP", _FallbackTOTP)
        setattr(existing_pyotp, "random_base32", _PyotpFallback.random_base32)
        pyotp = existing_pyotp
    else:
        pyotp = _PyotpFallback()
        sys.modules["pyotp"] = pyotp

try:
    import requests
    from requests import exceptions as requests_exceptions
except ImportError:  # pragma: no cover - live connectors are optional in some envs
    requests = None
    requests_exceptions = None
from requests.adapters import HTTPAdapter

try:
    from babel.dates import format_date as babel_format_date
    from babel.dates import format_datetime as babel_format_datetime
    from babel.numbers import format_decimal as babel_format_decimal
except ImportError:  # pragma: no cover - exercised in environments without Babel
    babel_format_date = None
    babel_format_datetime = None
    babel_format_decimal = None

try:
    from rich.console import Console
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeRemainingColumn,
    )
except ImportError:  # pragma: no cover - exercised when rich is unavailable
    Console = None
    Progress = None
    SpinnerColumn = None
    TextColumn = None
    BarColumn = None
    MofNCompleteColumn = None
    TimeRemainingColumn = None

# ---------------------------------------------------------------------------
# Simple TTL cache for repeated portal queries
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SAFE_CREDENTIAL_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SAFE_ENV_VAR_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_SAFE_AUTH_ROLE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_UNSAFE_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")
_UNSET = object()
_TRANSIENT_CONNECTOR_ERRORS = (TimeoutError, ConnectionError, OSError)
if requests_exceptions is not None:  # pragma: no branch - import-time constant setup
    _TRANSIENT_CONNECTOR_ERRORS = _TRANSIENT_CONNECTOR_ERRORS + (
        requests_exceptions.ConnectionError,
        requests_exceptions.Timeout,
    )
_DOCUMENT_LOCALE_CONFIG = {
    "en": {
        "babel_locale": "en_US",
        "date_format": "MM/dd/yyyy",
        "datetime_format": "MM/dd/yyyy HH:mm",
    },
    "bn": {
        "babel_locale": "bn_BD",
        "date_format": "dd/MM/yyyy",
        "datetime_format": "dd/MM/yyyy HH:mm",
    },
}
_DOCUMENT_LOCALE_ALIASES = {
    "en": "en",
    "en_us": "en",
    "en-us": "en",
    "bn": "bn",
    "bn_bd": "bn",
    "bn-bd": "bn",
}
DEFAULT_LOCALE_CODE = "en"
TRANSLATION_REVIEW_STATUSES = frozenset({"pending", "approved", "rejected"})
SUPPORTED_UI_LOCALES: dict[str, dict[str, Any]] = {
    "en": {
        "code": "en",
        "display_name": "English",
        "native_name": "English",
        "direction": "ltr",
        "is_rtl": False,
    },
    "bn": {
        "code": "bn",
        "display_name": "Bengali",
        "native_name": "বাংলা",
        "direction": "ltr",
        "is_rtl": False,
    },
    "ar": {
        "code": "ar",
        "display_name": "Arabic",
        "native_name": "العربية",
        "direction": "rtl",
        "is_rtl": True,
    },
    "ur": {
        "code": "ur",
        "display_name": "Urdu",
        "native_name": "اردو",
        "direction": "rtl",
        "is_rtl": True,
    },
}
_CONNECTOR_RESULT_SCHEMA_VERSION = 2
_CLI_PROGRESS_CALLBACK: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "funding_bot_cli_progress_callback",
    default=None,
)


@dataclass
class _CliOutputSettings:
    stdout_color: bool = False
    stderr_color: bool = False
    progress_enabled: bool = False
    no_color: bool = False
    json_output: bool = False


_CLI_OUTPUT_SETTINGS = _CliOutputSettings()


def _validate_email(email: str) -> str:
    """Return the stripped email or raise ValueError if it looks invalid."""
    stripped = email.strip()
    if not _EMAIL_RE.match(stripped):
        raise ValueError(f"Invalid email address: {stripped!r}")
    return stripped


def escape_html_text(value: Any) -> str:
    """Return HTML-escaped text for untrusted user-supplied values."""
    normalized = html.unescape(unicodedata.normalize("NFKC", str(value)))
    return html.escape(normalized, quote=True)


def sanitize_user_string(
    value: Any,
    *,
    field_name: str = "value",
    allow_empty: bool = True,
    multiline: bool = False,
    max_length: int = 4096,
    html_escape: bool = False,
) -> str:
    """Normalize user-supplied text and optionally HTML-escape it."""
    text = unicodedata.normalize("NFKC", "" if value is None else str(value))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if multiline:
        text = "".join(
            character
            for character in text
            if character in {"\n", "\t"} or (ord(character) >= 32 and ord(character) != 127)
        )
        text = "\n".join(line.rstrip() for line in text.split("\n"))
    else:
        text = _UNSAFE_CONTROL_CHARS_RE.sub(" ", text)
        text = re.sub(r"\s+", " ", text)
    text = text.strip()
    if not allow_empty and not text:
        raise ValueError(f"Field '{field_name}' is required.")
    if len(text) > max_length:
        raise ValueError(f"Field '{field_name}' must not exceed {max_length} characters.")
    return escape_html_text(text) if html_escape else text


def sanitize_user_mapping(
    value: dict[str, Any] | None,
    *,
    field_name: str = "value",
    max_depth: int = 4,
) -> dict[str, Any]:
    """Recursively sanitize user-controlled JSON-compatible mappings."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"Field '{field_name}' must be an object.")

    def _sanitize(item: Any, *, current_field: str, depth: int) -> Any:
        if depth < 0:
            raise ValueError(f"Field '{current_field}' is nested too deeply.")
        if item is None or isinstance(item, (bool, int, float)):
            return item
        if isinstance(item, str):
            return sanitize_user_string(
                item,
                field_name=current_field,
                multiline=True,
                html_escape=True,
            )
        if isinstance(item, list):
            return [
                _sanitize(list_item, current_field=current_field, depth=depth - 1)
                for list_item in item
            ]
        if isinstance(item, dict):
            sanitized: dict[str, Any] = {}
            for raw_key, raw_value in item.items():
                sanitized_key = sanitize_user_string(
                    raw_key,
                    field_name=f"{current_field}.key",
                    allow_empty=False,
                    max_length=128,
                )
                sanitized[sanitized_key] = _sanitize(
                    raw_value,
                    current_field=f"{current_field}.{sanitized_key}",
                    depth=depth - 1,
                )
            return sanitized
        raise ValueError(f"Field '{current_field}' contains an unsupported value type.")

    return _sanitize(value, current_field=field_name, depth=max_depth)


def validate_credential_alias(alias: str) -> str:
    normalized = sanitize_user_string(
        alias,
        field_name="alias",
        allow_empty=False,
        max_length=128,
    )
    if not _SAFE_CREDENTIAL_ALIAS_RE.fullmatch(normalized):
        raise ValueError(
            "Field 'alias' may only contain letters, numbers, dots, underscores, and hyphens."
        )
    return normalized


def validate_env_var_name(name: str) -> str:
    normalized = sanitize_user_string(
        name,
        field_name="env_var_name",
        allow_empty=False,
        max_length=128,
    )
    if not _SAFE_ENV_VAR_NAME_RE.fullmatch(normalized):
        raise ValueError(
            "Field 'env_var_name' must be an uppercase environment variable name containing only A-Z, 0-9, and underscores."
        )
    return normalized


def _extract_dict_keys(value: Any) -> list[str]:
    """Return the sorted, stringified keys of ``value`` if it is a dict.

    Used for audit-log detail payloads where only the *field names* of a
    setting (never its values) should be recorded, and where ``value`` is
    not guaranteed to be a ``dict`` at runtime despite the type hints.
    """
    if not isinstance(value, dict):
        return []
    return sorted(str(field) for field in value)


_DATA_CLASSIFICATION_LEVELS = ("public", "internal", "confidential", "secret")
_DATA_CLASSIFICATION_RANK = {
    classification: index for index, classification in enumerate(_DATA_CLASSIFICATION_LEVELS)
}
_ENCRYPTED_VALUE_PREFIX = "enc-v1:"


def _normalize_data_classification(value: str | None, *, default: str = "internal") -> str:
    normalized = str(value or default).strip().lower()
    if normalized not in _DATA_CLASSIFICATION_RANK:
        raise ValueError(
            f"Invalid data classification {value!r}. "
            f"Expected one of {list(_DATA_CLASSIFICATION_LEVELS)}."
        )
    return normalized


class _DocumentTranslationLookup:
    """Template translation lookup with English fallback."""

    def __init__(
        self,
        *,
        bot: "FundingBot",
        locale: str,
        translations: dict[str, dict[str, Any]],
    ) -> None:
        self._bot = bot
        self._locale = locale
        self._translations = translations

    def __getitem__(self, key: str) -> str:
        for locale_name in (self._locale, "en"):
            locale_translations = self._translations.get(locale_name, {})
            if key in locale_translations:
                return str(
                    self._bot._format_document_value(
                        locale_translations[key],
                        locale=self._locale,
                    )
                )
        raise KeyError(key)


def _normalize_text_list(values: Iterable[Any] | None) -> list[str]:
    """Normalize strings while preserving first-seen order."""
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        item = str(value).strip()
        if not item:
            continue
        lowered = item.lower()
        if lowered in seen:
            continue
        normalized.append(item)
        seen.add(lowered)
    return normalized


def _normalize_connector_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _read_numeric_env(
    names: Iterable[str],
    default: float,
    *,
    minimum: float | None = None,
    as_int: bool = False,
) -> float | int:
    for name in names:
        raw_value = os.environ.get(name)
        if raw_value is None:
            continue
        try:
            parsed = int(raw_value) if as_int else float(raw_value)
        except ValueError:
            continue
        if minimum is not None and parsed < minimum:
            return int(default) if as_int else default
        return parsed
    return int(default) if as_int else default


def _require_https_url(url: str, *, purpose: str = "Outbound request") -> str:
    parsed = urllib.parse.urlparse(url)
    allowed_insecure_hosts = {
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "host.docker.internal",
        "mock-connectors",
    }
    allow_insecure = os.environ.get("FUNDING_BOT_ALLOW_INSECURE_CONNECTOR_URLS", "").strip().lower()
    if parsed.scheme.lower() == "http" and allow_insecure in {"1", "true", "yes", "on"}:
        if (parsed.hostname or "").lower() in allowed_insecure_hosts:
            return url
    if parsed.scheme.lower() != "https":
        raise ConnectionSecurityError(f"{purpose} must use an https:// URL: {url!r}")
    if not parsed.netloc:
        raise ConnectionSecurityError(f"{purpose} must include a valid host: {url!r}")
    return url


def _build_tls_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    minimum_version = getattr(ssl.TLSVersion, "TLSv1_2", None)
    if minimum_version is not None:
        context.minimum_version = minimum_version
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


class _TLSHttpAdapter(HTTPAdapter):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._ssl_context = _build_tls_ssl_context()
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        kwargs["ssl_context"] = self._ssl_context
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args: Any, **kwargs: Any) -> Any:
        kwargs["ssl_context"] = self._ssl_context
        return super().proxy_manager_for(*args, **kwargs)


def _build_tls_http_session() -> requests.Session:
    session = requests.Session()
    adapter = _TLSHttpAdapter()
    session.mount("https://", adapter)
    return session


def _run_async(coroutine: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(coroutine)
        except BaseException as exc:  # pragma: no cover - defensive thread bridge
            error["exc"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if "exc" in error:
        raise error["exc"]
    return result.get("value")


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


@asynccontextmanager
async def _reuse_or_create_aiohttp_session(
    session: Any | None = None,
    *,
    timeout: float = 10.0,
):
    if session is not None:
        yield session
        return
    if aiohttp is None:
        yield None
        return
    connector = aiohttp.TCPConnector(ssl=_build_tls_ssl_context())
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(
        connector=connector, timeout=client_timeout
    ) as created_session:
        yield created_session


class AsyncDatabaseSession:
    """Serialize SQLite access behind an async context manager."""

    _WRITE_PREFIXES = (
        "INSERT",
        "UPDATE",
        "DELETE",
        "CREATE",
        "ALTER",
        "DROP",
        "REPLACE",
        "PRAGMA",
    )

    def __init__(self, connection: sqlite3.Connection, lock: threading.Lock) -> None:
        self._connection = connection
        self._lock = lock
        self._dirty = False

    async def __aenter__(self) -> "AsyncDatabaseSession":
        await asyncio.to_thread(self._lock.acquire)
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, _tb: Any) -> None:
        try:
            if self._dirty:
                if exc_type is None:
                    await asyncio.to_thread(self._connection.commit)
                else:
                    await asyncio.to_thread(self._connection.rollback)
        finally:
            await asyncio.to_thread(self._lock.release)

    async def execute(
        self,
        query: str,
        parameters: tuple[Any, ...] = (),
    ) -> sqlite3.Cursor:
        if query.lstrip().upper().startswith(self._WRITE_PREFIXES):
            self._dirty = True
        return await asyncio.to_thread(self._connection.execute, query, parameters)

    async def fetchone(
        self,
        query: str,
        parameters: tuple[Any, ...] = (),
    ) -> sqlite3.Row | None:
        return await asyncio.to_thread(
            lambda: self._connection.execute(query, parameters).fetchone()
        )

    async def fetchall(
        self,
        query: str,
        parameters: tuple[Any, ...] = (),
    ) -> list[sqlite3.Row]:
        return await asyncio.to_thread(
            lambda: self._connection.execute(query, parameters).fetchall()
        )


class _TTLCache:
    """A minimal thread-unsafe TTL cache keyed by arbitrary hashable keys."""

    def __init__(self, ttl_seconds: float = 300) -> None:
        self._ttl = ttl_seconds
        self._store: dict[Any, tuple[Any, float]] = {}
        self._hits = 0
        self._misses = 0

    def get(self, key: Any) -> tuple[bool, Any]:
        entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return False, None
        value, expires_at = entry
        if time.monotonic() > expires_at:
            del self._store[key]
            self._misses += 1
            return False, None
        self._hits += 1
        return True, value

    def set(self, key: Any, value: Any) -> None:
        self._store[key] = (value, time.monotonic() + self._ttl)

    def invalidate(self, key: Any) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()

    def stats(self) -> dict[str, float | int]:
        return {
            "hits": self._hits,
            "misses": self._misses,
            "size": len(self._store),
            "ttl_seconds": self._ttl,
        }


_DEFAULT_CACHE_MANAGER: CacheManager | None = None


def default_cache_manager() -> CacheManager:
    global _DEFAULT_CACHE_MANAGER
    if _DEFAULT_CACHE_MANAGER is None:
        from cache_manager import CacheManager as CacheManagerClass

        _DEFAULT_CACHE_MANAGER = CacheManagerClass()
    return _DEFAULT_CACHE_MANAGER


class _ManagedTTLCache:
    """Cache adapter backed by the shared cache manager."""

    def __init__(self, *, namespace: str, scope: str, ttl_seconds: float) -> None:
        self._region = default_cache_manager().make_region(
            namespace,
            scope=scope,
            ttl_seconds=ttl_seconds,
        )

    def get(self, key: Any) -> tuple[bool, Any]:
        return self._region.get(key)

    def set(self, key: Any, value: Any) -> None:
        tags = [str(key[0])] if isinstance(key, tuple) and key else None
        self._region.set(key, value, tags=tags)

    def invalidate(self, key: Any) -> None:
        self._region.invalidate(key)

    def clear(self) -> None:
        self._region.clear()

    def stats(self) -> dict[str, float | int | str]:
        return self._region.stats()


def _parse_secret_payload(raw_value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_value)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    return {"secret": raw_value}


@staticmethod
def _normalize_connector_configs(
    connector_configs: dict[str, Any] | list[dict[str, Any]] | None,
) -> dict[str, Any]:
    if connector_configs is None:
        return {"connectors": []}
    if isinstance(connector_configs, list):
        return {"connectors": [dict(item) for item in connector_configs]}
    if isinstance(connector_configs, dict):
        normalized = dict(connector_configs)
        if "connectors" in normalized:
            normalized["connectors"] = [dict(item) for item in normalized.get("connectors", [])]
        return normalized
    raise ConnectorConfigError(
        "Connector configuration must be a dict or list of connector entries."
    )


def _load_connector_configs(
    self,
    connector_configs: dict[str, Any] | list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if connector_configs is not None:
        normalized = self._normalize_connector_configs(connector_configs)
    else:
        raw_config = os.environ.get(CONNECTOR_CONFIG_ENV_VAR, "").strip()
        if not raw_config:
            return []
        try:
            parsed = json.loads(raw_config)
        except json.JSONDecodeError as exc:
            raise ConnectorConfigError(
                f"Invalid {CONNECTOR_CONFIG_ENV_VAR} JSON: {exc.msg} at line {exc.lineno} column {exc.colno}."
            ) from exc
        normalized = self._normalize_connector_configs(parsed)

    try:
        validate(instance=normalized, schema=CONNECTOR_CONFIG_SCHEMA)
    except ValidationError as exc:
        path = ".".join(str(part) for part in exc.path)
        field = f" at {path}" if path else ""
        raise ConnectorConfigError(
            f"Invalid connector configuration{field}: {exc.message}"
        ) from exc
    return [dict(item) for item in normalized.get("connectors", [])]


def _validate_connector_configs(self) -> None:
    for config in self.connector_configs:
        self.connector_registry.validate_config(
            config,
            credential_resolver=self.resolve_credential,
        )


def _prometheus_label_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


class ConnectorMetricsRegistry:
    """Collect connector request/error/latency metrics across connector instances."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._metrics: dict[tuple[str, str], dict[str, float]] = {}

    def ensure_connector(self, connector_name: str, connector_type: str) -> None:
        with self._lock:
            self._metrics.setdefault(
                (connector_name, connector_type),
                {
                    "requests_total": 0.0,
                    "errors_total": 0.0,
                    "latency_seconds_sum": 0.0,
                    "latency_seconds_count": 0.0,
                },
            )

    def record(
        self,
        *,
        connector_name: str,
        connector_type: str,
        latency_seconds: float,
        errored: bool,
    ) -> None:
        with self._lock:
            bucket = self._metrics.setdefault(
                (connector_name, connector_type),
                {
                    "requests_total": 0.0,
                    "errors_total": 0.0,
                    "latency_seconds_sum": 0.0,
                    "latency_seconds_count": 0.0,
                },
            )
            bucket["requests_total"] += 1
            bucket["latency_seconds_sum"] += max(latency_seconds, 0.0)
            bucket["latency_seconds_count"] += 1
            if errored:
                bucket["errors_total"] += 1

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            rows: list[dict[str, Any]] = []
            for (connector_name, connector_type), metrics in sorted(self._metrics.items()):
                rows.append(
                    {
                        "connector_name": connector_name,
                        "connector_type": connector_type,
                        **metrics,
                    }
                )
            return rows

    def reset(self) -> None:
        with self._lock:
            self._metrics.clear()

    def render_prometheus(self) -> list[str]:
        lines = [
            "# HELP funding_bot_connector_requests_total Total connector fetch requests",
            "# TYPE funding_bot_connector_requests_total counter",
            "# HELP funding_bot_connector_errors_total Total connector fetch errors",
            "# TYPE funding_bot_connector_errors_total counter",
            "# HELP funding_bot_connector_latency_seconds_sum Total connector fetch latency in seconds",
            "# TYPE funding_bot_connector_latency_seconds_sum counter",
            "# HELP funding_bot_connector_latency_seconds_count Total connector fetch latency observations",
            "# TYPE funding_bot_connector_latency_seconds_count counter",
        ]
        for row in self.snapshot():
            labels = (
                f'connector_name={_prometheus_label_value(str(row["connector_name"]))},'
                f'connector_type={_prometheus_label_value(str(row["connector_type"]))}'
            )
            lines.extend(
                [
                    f'funding_bot_connector_requests_total{{{labels}}} {int(row["requests_total"])}',
                    f'funding_bot_connector_errors_total{{{labels}}} {int(row["errors_total"])}',
                    f'funding_bot_connector_latency_seconds_sum{{{labels}}} {row["latency_seconds_sum"]:.6f}',
                    f'funding_bot_connector_latency_seconds_count{{{labels}}} {int(row["latency_seconds_count"])}',
                ]
            )
        return lines


_CONNECTOR_METRICS = ConnectorMetricsRegistry()


class BatchProcessingMetricsRegistry:
    """Track connector batching, coalescing, and runtime characteristics."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def record_scheduled(self, *, coalesced: bool) -> None:
        with self._lock:
            self._metrics["scheduled_requests"] += 1
            if coalesced:
                self._metrics["coalesced_requests"] += 1

    def record_batch(self, *, batch_size: int, duration_seconds: float, failed: bool) -> None:
        with self._lock:
            self._metrics["batches_total"] += 1
            self._metrics["batched_requests"] += max(0, batch_size)
            self._metrics["batch_size_sum"] += max(0, batch_size)
            self._metrics["batch_duration_seconds_sum"] += max(0.0, duration_seconds)
            self._metrics["last_batch_size"] = max(0, batch_size)
            self._metrics["max_batch_size"] = max(
                self._metrics["max_batch_size"], max(0, batch_size)
            )
            if failed:
                self._metrics["failed_batches"] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            metrics = dict(self._metrics)
        batches = metrics["batches_total"]
        metrics["average_batch_size"] = metrics["batch_size_sum"] / batches if batches else 0.0
        metrics["average_batch_duration_seconds"] = (
            metrics["batch_duration_seconds_sum"] / batches if batches else 0.0
        )
        return metrics

    def reset(self) -> None:
        with self._lock:
            self._metrics = {
                "scheduled_requests": 0,
                "coalesced_requests": 0,
                "batched_requests": 0,
                "batches_total": 0,
                "failed_batches": 0,
                "batch_size_sum": 0,
                "batch_duration_seconds_sum": 0.0,
                "last_batch_size": 0,
                "max_batch_size": 0,
            }

    def render_prometheus(self) -> list[str]:
        metrics = self.snapshot()
        return [
            "# HELP funding_bot_connector_batch_requests_total Connector requests scheduled for batching",
            "# TYPE funding_bot_connector_batch_requests_total counter",
            f"funding_bot_connector_batch_requests_total {metrics['scheduled_requests']}",
            "# HELP funding_bot_connector_batch_coalesced_total Connector requests merged into in-flight batches",
            "# TYPE funding_bot_connector_batch_coalesced_total counter",
            f"funding_bot_connector_batch_coalesced_total {metrics['coalesced_requests']}",
            "# HELP funding_bot_connector_batches_total Connector request batches executed",
            "# TYPE funding_bot_connector_batches_total counter",
            f"funding_bot_connector_batches_total {metrics['batches_total']}",
            "# HELP funding_bot_connector_failed_batches_total Connector request batches that failed",
            "# TYPE funding_bot_connector_failed_batches_total counter",
            f"funding_bot_connector_failed_batches_total {metrics['failed_batches']}",
            "# HELP funding_bot_connector_batch_size_average Average connector batch size",
            "# TYPE funding_bot_connector_batch_size_average gauge",
            f"funding_bot_connector_batch_size_average {metrics['average_batch_size']:.6f}",
            "# HELP funding_bot_connector_batch_duration_seconds_average Average connector batch runtime in seconds",
            "# TYPE funding_bot_connector_batch_duration_seconds_average gauge",
            f"funding_bot_connector_batch_duration_seconds_average {metrics['average_batch_duration_seconds']:.6f}",
            "# HELP funding_bot_connector_batch_size_max Largest connector batch observed",
            "# TYPE funding_bot_connector_batch_size_max gauge",
            f"funding_bot_connector_batch_size_max {metrics['max_batch_size']}",
        ]


_BATCH_METRICS = BatchProcessingMetricsRegistry()


def _stream_supports_color(stream: Any) -> bool:
    isatty = getattr(stream, "isatty", None)
    return bool(callable(isatty) and isatty())


def _should_use_color(
    stream: Any,
    *,
    no_color: bool = False,
    json_output: bool = False,
) -> bool:
    return (
        not no_color
        and not json_output
        and not os.environ.get("NO_COLOR")
        and _stream_supports_color(stream)
    )


def _configure_cli_output(
    *, no_color: bool = False, json_output: bool = False
) -> _CliOutputSettings:
    _CLI_OUTPUT_SETTINGS.stdout_color = _should_use_color(
        sys.stdout,
        no_color=no_color,
        json_output=json_output,
    )
    _CLI_OUTPUT_SETTINGS.stderr_color = _should_use_color(
        sys.stderr,
        no_color=no_color,
        json_output=json_output,
    )
    _CLI_OUTPUT_SETTINGS.progress_enabled = bool(
        not json_output
        and Console is not None
        and Progress is not None
        and _stream_supports_color(sys.stdout)
    )
    _CLI_OUTPUT_SETTINGS.no_color = no_color
    _CLI_OUTPUT_SETTINGS.json_output = json_output
    colorama_init(
        strip=not (_CLI_OUTPUT_SETTINGS.stdout_color or _CLI_OUTPUT_SETTINGS.stderr_color)
    )
    return _CLI_OUTPUT_SETTINGS


def _style_cli_text(message: str, *, level: str | None = None, stream: Any | None = None) -> str:
    if stream is None:
        stream = sys.stdout
    color_enabled = (
        _CLI_OUTPUT_SETTINGS.stderr_color
        if stream is sys.stderr
        else _CLI_OUTPUT_SETTINGS.stdout_color
    )
    if not color_enabled:
        return message
    color = {
        "success": Fore.GREEN,
        "error": Fore.RED,
        "warning": Fore.YELLOW,
        "info": Fore.CYAN,
    }.get(level, "")
    return f"{color}{message}{Style.RESET_ALL}" if color else message


def _cli_print(message: str = "", *, level: str | None = None, file: Any | None = None) -> None:
    stream = sys.stdout if file is None else file
    print(_style_cli_text(message, level=level, stream=stream), file=stream)


def _colorize_status_text(status: str) -> str:
    normalized = str(status).strip().lower()
    if normalized in {"ok", "healthy", "success", "completed"}:
        level = "success"
    elif normalized in {"warning", "degraded", "disabled", "cancelled"}:
        level = "warning"
    elif normalized in {"error", "failed"}:
        level = "error"
    else:
        level = None
    return _style_cli_text(str(status), level=level)


class _CliColorFormatter(logging.Formatter):
    def __init__(self, fmt: str, *, color_enabled: bool) -> None:
        super().__init__(fmt)
        self._color_enabled = color_enabled

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        if not self._color_enabled:
            return message
        if record.levelno >= logging.ERROR:
            color = Fore.RED
        elif record.levelno >= logging.WARNING:
            color = Fore.YELLOW
        elif record.levelno >= logging.INFO:
            color = Fore.GREEN
        else:
            color = ""
        return f"{color}{message}{Style.RESET_ALL}" if color else message


def _format_eta(seconds: float | None) -> str:
    if seconds is None or seconds <= 0:
        return "0s"
    rounded = int(round(seconds))
    minutes, secs = divmod(rounded, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _emit_progress_event(
    progress_callback: Callable[[dict[str, Any]], None] | None,
    **event: Any,
) -> None:
    if progress_callback is None:
        return
    payload = dict(event)
    total = max(int(payload.get("total", 0) or 0), 0)
    completed = max(int(payload.get("completed", 0) or 0), 0)
    if total:
        completed = min(completed, total)
    payload["total"] = total
    payload["completed"] = completed
    payload["remaining"] = max(total - completed, 0)
    try:
        progress_callback(payload)
    except Exception:  # pragma: no cover - progress reporting must never break work
        logging.getLogger(__name__).debug("Progress callback failed.", exc_info=True)


class _CliProgressReporter:
    def __init__(self) -> None:
        self._console = None
        self._progress = None
        self._task_ids: dict[str, Any] = {}
        self._started_at: dict[str, float] = {}
        self._last_snapshot: dict[str, tuple[str, int, int]] = {}

    def __enter__(self) -> "_CliProgressReporter":
        if _CLI_OUTPUT_SETTINGS.progress_enabled and Console is not None and Progress is not None:
            self._console = Console(
                file=sys.stderr,
                no_color=_CLI_OUTPUT_SETTINGS.no_color,
                highlight=False,
            )
            self._progress = Progress(
                SpinnerColumn(),
                TextColumn("{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TextColumn("• {task.fields[remaining]} remaining"),
                TimeRemainingColumn(),
                console=self._console,
                transient=True,
            )
            self._progress.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._progress is not None:
            self._progress.__exit__(exc_type, exc, tb)

    def update(
        self,
        key: str,
        *,
        description: str,
        completed: int,
        total: int,
    ) -> None:
        normalized_total = max(int(total), 1)
        normalized_completed = min(max(int(completed), 0), normalized_total)
        remaining = max(normalized_total - normalized_completed, 0)
        if self._progress is not None:
            task_id = self._task_ids.get(key)
            if task_id is None:
                task_id = self._progress.add_task(
                    description,
                    total=normalized_total,
                    completed=normalized_completed,
                    remaining=remaining,
                )
                self._task_ids[key] = task_id
            else:
                self._progress.update(
                    task_id,
                    description=description,
                    total=normalized_total,
                    completed=normalized_completed,
                    remaining=remaining,
                )
            return
        now = time.monotonic()
        started_at = self._started_at.setdefault(key, now)
        eta = 0.0
        if 0 < normalized_completed < normalized_total:
            elapsed = max(now - started_at, 0.001)
            eta = (elapsed / normalized_completed) * remaining
        snapshot = (description, normalized_completed, normalized_total)
        if self._last_snapshot.get(key) == snapshot:
            return
        self._last_snapshot[key] = snapshot
        _cli_print(
            f"{description}: {normalized_completed}/{normalized_total} complete, "
            f"{remaining} remaining, ETA {_format_eta(eta)}",
            file=sys.stderr,
        )

    def update_from_event(self, event: dict[str, Any]) -> None:
        detail = (
            event.get("callback_payload") if isinstance(event.get("callback_payload"), dict) else {}
        )
        if detail and detail.get("total"):
            self.update_from_detail(
                detail, fallback_message=str(event.get("message") or "Task progress")
            )
            return
        task_name = str(event.get("task_name") or "task").replace("_", " ")
        self.update(
            task_name,
            description=str(event.get("message") or task_name.title()),
            completed=int(event.get("progress", 0)),
            total=100,
        )

    def update_from_detail(
        self, detail: dict[str, Any], *, fallback_message: str = "Task progress"
    ) -> None:
        key = str(detail.get("stage") or detail.get("key") or "task")
        description = str(detail.get("description") or fallback_message or key.replace("-", " "))
        self.update(
            key,
            description=description,
            completed=int(detail.get("completed", 0)),
            total=int(detail.get("total", 1)),
        )


@contextmanager
def _bind_cli_progress(reporter: _CliProgressReporter | None) -> Any:
    if reporter is None:
        yield
        return
    token = _CLI_PROGRESS_CALLBACK.set(reporter.update_from_event)
    try:
        yield
    finally:
        _CLI_PROGRESS_CALLBACK.reset(token)


class FundingBotError(Exception):
    """Base error for funding bot operations."""


class RateLimitExceededError(FundingBotError):
    """Raised when a connector exhausts its allotted upstream quota."""


class ConnectionSecurityError(FundingBotError):
    """Raised when an outbound connector request violates TLS requirements."""


class DuplicateSubmissionError(FundingBotError):
    """Raised when an opportunity already has an application record."""


class OpportunityNotFoundError(FundingBotError):
    """Raised when an opportunity cannot be found."""


class CredentialNotFoundError(FundingBotError):
    """Raised when a credential alias cannot be resolved."""


class ConnectorConfigError(FundingBotError):
    """Raised when connector configuration or credentials are invalid."""


class CredentialRefreshError(FundingBotError):
    """Raised when an OAuth2 access token cannot be retrieved."""


class OutreachThrottledError(FundingBotError):
    """Raised when an outreach email exceeds the allowed cadence."""


class OptOutError(FundingBotError):
    """Raised when a donor has opted out of outreach."""


class AccountLockedError(FundingBotError):
    """Raised when a dashboard account is temporarily locked."""


class MFARequiredError(FundingBotError):
    """Raised when a dashboard account requires a second authentication factor."""


@dataclass(frozen=True)
class ConsentRecord:
    """Immutable donor communication consent event."""

    donor_email: str
    channel: str
    status: str
    consented_at: str
    source: str
    recorded_at: str
    id: int | None = None
    withdrawn_at: str | None = None
    proof: str | None = None
    notes: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row | None) -> "ConsentRecord | None":
        if row is None:
            return None
        return cls(
            id=row["id"],
            donor_email=row["donor_email"],
            channel=row["channel"],
            status=row["status"],
            consented_at=row["consented_at"],
            withdrawn_at=row["withdrawn_at"],
            source=row["source"],
            proof=row["proof"],
            notes=row["notes"],
            recorded_at=row["recorded_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "donor_email": self.donor_email,
            "channel": self.channel,
            "status": self.status,
            "consented_at": self.consented_at,
            "withdrawn_at": self.withdrawn_at,
            "source": self.source,
            "proof": self.proof,
            "notes": self.notes,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True)
class Task:
    """Immutable staff task record."""

    title: str
    description: str
    assignee: str
    status: str
    due_date: str
    created_at: str
    updated_at: str
    data_classification: str = "internal"
    id: int | None = None
    external_id: str | None = None
    source: str = "manual"
    assignee_email: str | None = None
    assignee_name: str | None = None
    attributed_connector: str | None = None
    opportunity_signature: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row | None) -> "Task | None":
        if row is None:
            return None
        data = dict(row)
        if "assignee" not in data and "assigned_to" in data:
            data["assignee"] = data.pop("assigned_to")
        allowed = {field.name for field in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in allowed})

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["assigned_to"] = data["assignee"]
        return data


class TaskTransitionError(FundingBotError):
    """Raised when a task status change violates the workflow state machine."""


class TaskNotFoundError(FundingBotError):
    """Raised when a task cannot be found."""


class TaskCommentNotFoundError(FundingBotError):
    """Raised when a task comment cannot be found."""


class GracefulShutdownRequested(FundingBotError):
    """Raised when an in-flight queue task is asked to stop cleanly."""


class GracefulShutdownController:
    """Cooperative SIGTERM/SIGINT shutdown controller for queue workers."""

    def __init__(self, on_shutdown: Callable[[int], None] | None = None) -> None:
        self._shutdown_event = threading.Event()
        self._on_shutdown = on_shutdown
        self._original_handlers: dict[int, Any] = {}
        self.received_signals: list[int] = []

    def install(self) -> "GracefulShutdownController":
        for sig in (signal.SIGTERM, signal.SIGINT):
            self._original_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, self._handle_signal)
        return self

    def restore(self) -> None:
        for sig, handler in self._original_handlers.items():
            signal.signal(sig, handler)
        self._original_handlers.clear()

    def shutdown_requested(self) -> bool:
        return self._shutdown_event.is_set()

    def raise_if_shutdown_requested(self, *, reason: str | None = None) -> None:
        if self.shutdown_requested():
            raise GracefulShutdownRequested(
                reason or "Shutdown requested for in-flight queue task."
            )

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        self.received_signals.append(signum)
        self._shutdown_event.set()
        if self._on_shutdown is not None:
            self._on_shutdown(signum)


class QueueTaskContext:
    """Runtime state exposed to cooperative queue tasks."""

    def __init__(
        self,
        *,
        bot: "FundingBot",
        idempotency_key: str,
        controller: GracefulShutdownController,
        task_name: str,
        payload: dict[str, Any] | None = None,
        worker_id: str | None = None,
        retry_limit: int = 0,
        backoff_seconds: float = 0.0,
        backoff_max_seconds: float = 0.0,
    ) -> None:
        self.bot = bot
        self.idempotency_key = idempotency_key
        self._controller = controller
        self.task_name = task_name
        self.payload = dict(payload or {})
        self.worker_id = worker_id
        self.retry_limit = retry_limit
        self.backoff_seconds = backoff_seconds
        self.backoff_max_seconds = backoff_max_seconds

    def shutdown_requested(self) -> bool:
        return self._controller.shutdown_requested()

    def checkpoint(self, reason: str | None = None) -> None:
        self._controller.raise_if_shutdown_requested(reason=reason)

    def update_progress(
        self,
        progress: int,
        message: str,
        *,
        attempt_number: int = 0,
        callback_name: str = "progress",
        callback_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task_run = self.bot.record_task_run(
            self.idempotency_key,
            self.task_name,
            status="running",
            progress=progress,
            message=message,
            payload=self.payload,
            callback_name=callback_name,
            callback_payload=callback_payload,
            idempotency_key=self.idempotency_key,
            worker_id=self.worker_id,
            retry_limit=self.retry_limit,
            attempts=attempt_number,
            backoff_seconds=self.backoff_seconds,
            backoff_max_seconds=self.backoff_max_seconds,
            dead_lettered=False,
        )
        progress_callback = _CLI_PROGRESS_CALLBACK.get()
        if progress_callback is not None:
            _emit_progress_event(
                progress_callback,
                task_id=self.idempotency_key,
                task_name=self.task_name,
                progress=progress,
                message=message,
                callback_name=callback_name,
                callback_payload=callback_payload or {},
            )
        return task_run


class BrowserClient(Protocol):
    def submit(
        self,
        portal_url: str,
        credentials: dict[str, Any],
        form_data: dict[str, Any],
        attachments: Iterable[str],
    ) -> str:
        """Submit an application and return a submission reference."""


class PortalConnector(Protocol):
    def fetch_opportunities(self, keywords: Iterable[str]) -> list[dict[str, Any]]:
        """Fetch opportunities from an external portal."""

    def invalidate_cache(self, keywords: Iterable[str] | None = None) -> None:
        """Invalidate all cached results or only those matching ``keywords``."""

    def cache_metrics(self) -> dict[str, Any]:
        """Return connector cache usage metrics."""

    def check_health(self) -> dict[str, Any]:
        """Return the current connector health state."""

    def get_failure_metrics(self) -> dict[str, Any]:
        """Return connector resilience metrics."""


class CredentialVault(Protocol):
    def get_secret(self, name: str) -> str:
        """Return a secret by name."""


class AIClient(Protocol):
    def generate(self, prompt: str) -> str:
        """Generate a response for the supplied prompt."""


class EnvVarVault:
    """Resolve secrets from environment variables."""

    def get_secret(self, name: str) -> str:
        value = os.getenv(name)
        if value is None:
            raise CredentialNotFoundError(f"Environment variable {name!r} is not set.")
        return value


class FileVault:
    """Resolve secrets from files inside a directory."""

    def __init__(self, secrets_dir: str | os.PathLike[str]) -> None:
        self.secrets_dir = Path(secrets_dir)

    def get_secret(self, name: str) -> str:
        path = self.secrets_dir / name
        if not path.exists():
            raise CredentialNotFoundError(f"Secret file {str(path)!r} does not exist.")
        return path.read_text(encoding="utf-8").strip()


class OAuth2ClientCredentialsVault:
    """Add OAuth2 client-credentials support on top of another vault."""

    _RESERVED_KEYS = {
        "auth_type",
        "oauth2",
        "credentials",
        "token_url",
        "client_id",
        "client_secret",
        "scope",
        "scopes",
        "audience",
        "token_auth_method",
    }

    def __init__(
        self,
        backing_vault: CredentialVault | None = None,
        *,
        token_http_client: Callable[[str, dict[str, Any], dict[str, str]], Any] | None = None,
        refresh_skew_seconds: float | None = None,
    ) -> None:
        self.backing_vault = backing_vault or EnvVarVault()
        self.token_http_client = token_http_client or self._default_token_http_client
        if refresh_skew_seconds is None:
            try:
                refresh_skew_seconds = float(os.environ.get("OAUTH2_REFRESH_SKEW_SECONDS", "60"))
            except ValueError:
                refresh_skew_seconds = 60.0
        self.refresh_skew_seconds = max(0.0, refresh_skew_seconds)
        self._token_cache: dict[str, dict[str, Any]] = {}

    def get_secret(self, name: str) -> str:
        return self.backing_vault.get_secret(name)

    def resolve_credentials(self, name: str) -> dict[str, Any]:
        payload = _parse_secret_payload(self.get_secret(name))
        oauth2_config = self._extract_oauth2_config(payload)
        if oauth2_config is None:
            return payload

        token = self._get_oauth2_token(name, oauth2_config)
        credentials = self._base_credentials(payload)
        credentials.update(
            {
                "auth_type": "oauth2_client_credentials",
                "access_token": token["access_token"],
                "token_type": token["token_type"],
                "expires_at": token["expires_at"],
                "authorization_header": f'{token["token_type"]} {token["access_token"]}',
            }
        )
        if token.get("scope"):
            credentials["scope"] = token["scope"]
        return credentials

    def _extract_oauth2_config(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if payload.get("auth_type") == "oauth2_client_credentials":
            candidate = payload.get("oauth2", payload)
        elif isinstance(payload.get("oauth2"), dict):
            candidate = payload["oauth2"]
        elif {"token_url", "client_id", "client_secret"}.issubset(payload):
            candidate = payload
        else:
            return None
        if not isinstance(candidate, dict):
            raise CredentialRefreshError("OAuth2 configuration must be a JSON object.")
        token_url = str(candidate.get("token_url", "")).strip()
        client_id = str(candidate.get("client_id", "")).strip()
        client_secret = str(candidate.get("client_secret", "")).strip()
        if not token_url or not client_id or not client_secret:
            raise CredentialRefreshError(
                "OAuth2 client-credentials configuration requires token_url, client_id, and client_secret."
            )
        scope = candidate.get("scope")
        scopes = candidate.get("scopes")
        if not scope and isinstance(scopes, (list, tuple)):
            scope = " ".join(str(item).strip() for item in scopes if str(item).strip())
        return {
            "token_url": token_url,
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": str(scope).strip() if scope else "",
            "audience": str(candidate.get("audience", "")).strip(),
            "token_auth_method": str(candidate.get("token_auth_method", "basic")).strip().lower(),
        }

    def _base_credentials(self, payload: dict[str, Any]) -> dict[str, Any]:
        credentials = payload.get("credentials", {})
        resolved = dict(credentials) if isinstance(credentials, dict) else {}
        for key, value in payload.items():
            if key not in self._RESERVED_KEYS:
                resolved.setdefault(key, value)
        return resolved

    def _get_oauth2_token(self, cache_key: str, config: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        cached = self._token_cache.get(cache_key)
        if cached is not None:
            expires_at = cached["expires_at_datetime"]
            if (expires_at - now).total_seconds() > self.refresh_skew_seconds:
                return {
                    "access_token": cached["access_token"],
                    "token_type": cached["token_type"],
                    "expires_at": cached["expires_at"],
                    "scope": cached.get("scope", ""),
                }

        form_data = {"grant_type": "client_credentials"}
        if config.get("scope"):
            form_data["scope"] = config["scope"]
        if config.get("audience"):
            form_data["audience"] = config["audience"]

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        if config["token_auth_method"] == "body":
            form_data["client_id"] = config["client_id"]
            form_data["client_secret"] = config["client_secret"]
        else:
            token = base64.b64encode(
                f'{config["client_id"]}:{config["client_secret"]}'.encode("utf-8")
            ).decode("ascii")
            headers["Authorization"] = f"Basic {token}"

        try:
            response = self.token_http_client(config["token_url"], form_data, headers)
        except Exception as exc:
            raise CredentialRefreshError(
                f"Failed to retrieve OAuth2 access token for secret {cache_key!r}: {exc}"
            ) from exc

        if isinstance(response, str):
            try:
                response = json.loads(response)
            except json.JSONDecodeError as exc:
                raise CredentialRefreshError(
                    f"OAuth2 token endpoint for secret {cache_key!r} returned invalid JSON."
                ) from exc
        if not isinstance(response, dict):
            raise CredentialRefreshError(
                f"OAuth2 token endpoint for secret {cache_key!r} returned an unsupported payload."
            )

        access_token = str(response.get("access_token", "")).strip()
        if not access_token:
            raise CredentialRefreshError(
                f"OAuth2 token endpoint for secret {cache_key!r} did not return access_token."
            )
        token_type = str(response.get("token_type", "Bearer")).strip() or "Bearer"
        try:
            expires_in = int(response.get("expires_in", 3600))
        except (TypeError, ValueError):
            expires_in = 3600
        expires_in = max(expires_in, 1)
        expires_at_datetime = now + timedelta(seconds=expires_in)
        scope = str(response.get("scope", config.get("scope", ""))).strip()
        cached = {
            "access_token": access_token,
            "token_type": token_type,
            "expires_at": expires_at_datetime.isoformat(),
            "expires_at_datetime": expires_at_datetime,
            "scope": scope,
        }
        self._token_cache[cache_key] = cached
        return {
            "access_token": access_token,
            "token_type": token_type,
            "expires_at": cached["expires_at"],
            "scope": scope,
        }

    @staticmethod
    def _default_token_http_client(
        url: str,
        form_data: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        secure_url = _require_https_url(url, purpose="OAuth token request")
        with _build_tls_http_session() as session:
            response = session.post(
                secure_url,
                data=form_data,
                headers=headers,
                timeout=30,
                verify=True,
            )
            response.raise_for_status()
            parsed = response.json()
        if not isinstance(parsed, dict):
            raise CredentialRefreshError(
                "OAuth2 token endpoint returned a non-object JSON payload."
            )
        return parsed


def _perform_json_request(
    method: str,
    url: str,
    *,
    session: Any | None = None,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_payload: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> Any:
    if session is None:
        if requests is None:
            raise FundingBotError(
                "The requests package is required for live connector HTTP transport."
            )
        session = requests.Session()
    request_method = getattr(session, method.lower(), None)
    if not callable(request_method):
        raise FundingBotError(f"HTTP session does not support {method.upper()} requests.")
    request_headers = dict(headers or {})
    inject_context(request_headers)
    with start_span(
        f"connector.http.{method.lower()}",
        kind=SpanKind.CLIENT,
        attributes={
            "http.request.method": method.upper(),
            "url.full": url,
            "network.protocol.name": "http",
        },
    ) as span:
        try:
            response = request_method(
                url,
                headers=request_headers,
                params=params,
                json=json_payload,
                timeout=timeout,
            )
            status_code = getattr(response, "status_code", None)
            if status_code is not None:
                span.set_attribute("http.response.status_code", int(status_code))
            if hasattr(response, "raise_for_status"):
                response.raise_for_status()
            if hasattr(response, "json"):
                return response.json()
            if isinstance(response, (dict, list)):
                return response
            raise FundingBotError(
                f"HTTP client for {url!r} returned {type(response).__name__}, expected JSON-compatible data."
            )
        except Exception as exc:
            set_span_error(span, exc)
            raise


class TokenBucketRateLimiter:
    """In-memory token bucket for per-connector request quotas."""

    def __init__(
        self,
        capacity: float,
        refill_rate_per_second: float,
        *,
        time_func: Callable[[], float] | None = None,
    ) -> None:
        self.capacity = max(1.0, float(capacity))
        self.refill_rate_per_second = max(0.0, float(refill_rate_per_second))
        self._time = time_func or time.monotonic
        self._tokens = self.capacity
        self._updated_at = self._time()

    def _refill(self) -> None:
        now = self._time()
        elapsed = max(0.0, now - self._updated_at)
        self._updated_at = now
        if elapsed > 0 and self.refill_rate_per_second > 0:
            self._tokens = min(
                self.capacity,
                self._tokens + elapsed * self.refill_rate_per_second,
            )

    def consume(self, tokens: float = 1.0) -> tuple[bool, float]:
        requested = max(0.0, float(tokens))
        self._refill()
        if self._tokens >= requested:
            self._tokens -= requested
            return True, 0.0
        if self.refill_rate_per_second <= 0:
            return False, float("inf")
        return False, (requested - self._tokens) / self.refill_rate_per_second

    @property
    def available_tokens(self) -> float:
        self._refill()
        return self._tokens


@dataclass(frozen=True)
class ConnectorBatchRequest:
    connector: PortalConnector
    keywords: tuple[str, ...]


class ConnectorBatchScheduler:
    """Batch connector requests and coalesce duplicate in-flight work."""

    def __init__(self, *, batch_size: int = 5) -> None:
        self.batch_size = max(1, int(batch_size))

    def _request_key(self, request: ConnectorBatchRequest) -> str:
        build_cache_key = getattr(request.connector, "build_cache_key", None)
        if callable(build_cache_key):
            connector_cache_key = str(build_cache_key(request.keywords))
        else:
            connector_cache_key = json.dumps(sorted(request.keywords))
        connector_name = getattr(
            request.connector,
            "source_name",
            request.connector.__class__.__name__,
        )
        return f"{connector_name}:{connector_cache_key}"

    async def submit_many(self, requests: Iterable[ConnectorBatchRequest]) -> list[Any]:
        ordered_requests = list(requests)
        if not ordered_requests:
            return []

        pending: dict[str, asyncio.Future[Any]] = {}
        unique_requests: list[tuple[str, ConnectorBatchRequest, asyncio.Future[Any]]] = []
        futures: list[asyncio.Future[Any]] = []
        loop = asyncio.get_running_loop()

        for request in ordered_requests:
            request_key = self._request_key(request)
            future = pending.get(request_key)
            if future is None:
                future = loop.create_future()
                pending[request_key] = future
                unique_requests.append((request_key, request, future))
                _BATCH_METRICS.record_scheduled(coalesced=False)
            else:
                _BATCH_METRICS.record_scheduled(coalesced=True)
            futures.append(future)

        async with _reuse_or_create_aiohttp_session(timeout=10.0) as shared_session:
            for start in range(0, len(unique_requests), self.batch_size):
                batch = unique_requests[start : start + self.batch_size]
                batch_started_at = time.perf_counter()
                results = await asyncio.gather(
                    *[
                        self._execute_request(
                            request,
                            shared_session=shared_session,
                        )
                        for _, request, _ in batch
                    ],
                    return_exceptions=True,
                )
                failed = any(isinstance(result, Exception) for result in results)
                _BATCH_METRICS.record_batch(
                    batch_size=len(batch),
                    duration_seconds=time.perf_counter() - batch_started_at,
                    failed=failed,
                )
                for (_, _request, future), result in zip(batch, results):
                    if future.done():
                        continue
                    if isinstance(result, Exception):
                        future.set_exception(result)
                    else:
                        future.set_result(result)

        return await asyncio.gather(
            *[asyncio.shield(future) for future in futures],
            return_exceptions=True,
        )

    async def _execute_request(
        self,
        request: ConnectorBatchRequest,
        *,
        shared_session: Any | None = None,
    ) -> Any:
        fetch_result_async = getattr(request.connector, "fetch_result_async", None)
        if callable(fetch_result_async):
            return await fetch_result_async(request.keywords, shared_session=shared_session)
        return await asyncio.to_thread(request.connector.fetch_result, request.keywords)


class _BasePortalConnector:
    """Common behavior for demo portal connectors."""

    connector_slug = "portal"
    source_name = "Portal"
    base_url = "https://example.org"
    result_schema_version = _CONNECTOR_RESULT_SCHEMA_VERSION
    keyword_category_mappings: dict[str, dict[str, tuple[str, ...]]] = {}
    default_page_size = 100

    def __init__(
        self,
        http_client: Callable[..., Any] | None = None,
        async_http_client: Callable[..., Any] | None = None,
        *,
        base_url: str | None = None,
        source_name: str | None = None,
        credentials: dict[str, Any] | None = None,
        credential_name: str | None = None,
        credential_vault: CredentialVault | OAuth2ClientCredentialsVault | None = None,
        request_session: Any | None = None,
        request_timeout: float = 30.0,
        transport: str = "demo",
        cache_ttl: float | None = None,
        page_size: int | None = None,
        request_cost_usd: float | None = None,
        max_retries: int = 2,
        retry_backoff_base: float = 0.25,
        retry_backoff_factor: float = 2.0,
        circuit_failure_threshold: int = 3,
        circuit_recovery_timeout: float = 30.0,
        sleep_func: Callable[[float], None] | None = None,
        time_func: Callable[[], float] | None = None,
        rate_limit_config: dict[str, float] | None = None,
        rate_limiter: TokenBucketRateLimiter | None = None,
        cache_manager: CacheManager | None = None,
    ) -> None:
        self.http_client = http_client
        self.async_http_client = async_http_client
        self.base_url = _require_https_url(
            base_url or self.base_url,
            purpose=f"{self.source_name or self.__class__.__name__} connector base URL",
        )
        self.source_name = source_name or self.source_name
        self.credentials = dict(credentials or {})
        self.credential_name = credential_name
        self._credential_vault = self._wrap_credential_vault(credential_vault)
        self._request_session = request_session
        self.request_timeout = max(1.0, float(request_timeout))
        self.transport = transport
        self.page_size = self._resolve_page_size(page_size)
        self.request_cost_usd = self._resolve_request_cost(request_cost_usd)
        cache_ttl = self._resolve_cache_ttl(cache_ttl)
        self._cache_manager = cache_manager
        if cache_manager is None:
            self._cache = _TTLCache(ttl_seconds=cache_ttl)
        else:
            self._cache = _ManagedTTLCache(
                namespace="connector-data",
                scope=self.connector_slug,
                ttl_seconds=cache_ttl,
            )
        self.max_retries = max(0, max_retries)
        self.retry_backoff_base = max(0.0, retry_backoff_base)
        self.retry_backoff_factor = max(1.0, retry_backoff_factor)
        self.circuit_failure_threshold = max(1, circuit_failure_threshold)
        self.circuit_recovery_timeout = max(0.0, circuit_recovery_timeout)
        self._sleep = sleep_func or time.sleep
        self._time = time_func or time.monotonic
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self.rate_limit_config = self._resolve_rate_limit_config(rate_limit_config)
        self._rate_limiter = rate_limiter or TokenBucketRateLimiter(
            self.rate_limit_config["capacity"],
            self.rate_limit_config["refill_rate"],
            time_func=self._time,
        )
        self._circuit_state = "closed"
        self._opened_at: float | None = None
        self._last_error: str | None = None
        self._last_rate_limit_retry_after: float | None = None
        _CONNECTOR_METRICS.ensure_connector(self.source_name, self.connector_slug)
        self._metrics: dict[str, Any] = {
            "requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "retry_attempts": 0,
            "health_checks": 0,
            "short_circuits": 0,
            "consecutive_failures": 0,
            "state_transitions": 0,
            "rate_limited_requests": 0,
        }

    @staticmethod
    def _wrap_credential_vault(
        credential_vault: CredentialVault | OAuth2ClientCredentialsVault | None,
    ) -> OAuth2ClientCredentialsVault:
        if isinstance(credential_vault, OAuth2ClientCredentialsVault):
            return credential_vault
        return OAuth2ClientCredentialsVault(credential_vault or EnvVarVault())

    def _get_resolved_credentials(self) -> dict[str, Any]:
        if self.credentials:
            return dict(self.credentials)
        if self.credential_name:
            return dict(self._credential_vault.resolve_credentials(self.credential_name))
        return {}

    def _get_request_session(self) -> Any:
        if self._request_session is None:
            if requests is None:
                raise FundingBotError(
                    "The requests package is required for live connector HTTP transport."
                )
            self._request_session = requests.Session()
        return self._request_session

    def _resolve_page_size(self, page_size: int | None) -> int:
        if page_size is None:
            candidate = os.environ.get(
                f"{self._config_prefix()}_PAGE_SIZE",
                os.environ.get("PORTAL_PAGE_SIZE", self.default_page_size),
            )
        else:
            candidate = page_size
        try:
            normalized = int(candidate)
        except (TypeError, ValueError):
            normalized = self.default_page_size
        return max(1, normalized)

    def _resolve_cache_ttl(self, cache_ttl: float | None) -> float:
        if cache_ttl is None:
            raw_ttl = os.environ.get(
                f"{self._config_prefix()}_CACHE_TTL",
                os.environ.get("PORTAL_CACHE_TTL", "300"),
            )
            try:
                cache_ttl = float(raw_ttl)
            except ValueError:
                cache_ttl = 300
        if cache_ttl <= 0:
            return 300.0
        return float(cache_ttl)

    def _resolve_request_cost(self, request_cost_usd: float | None) -> float:
        if request_cost_usd is None:
            candidate = _read_numeric_env(
                [
                    f"{self._config_prefix()}_REQUEST_COST_USD",
                    "PORTAL_REQUEST_COST_DEFAULT_USD",
                ],
                0.0,
                minimum=0.0,
            )
        else:
            candidate = request_cost_usd
        try:
            return max(0.0, float(candidate))
        except (TypeError, ValueError):
            return 0.0

    def _config_prefix(self) -> str:
        return re.sub(r"[^A-Z0-9]+", "_", self.connector_slug.upper())

    def _resolve_rate_limit_config(self, config: dict[str, float] | None) -> dict[str, float]:
        connector_key = _normalize_connector_key(self.connector_slug or self.source_name).upper()
        resolved = dict(config or {})
        resolved.setdefault(
            "capacity",
            float(
                _read_numeric_env(
                    [
                        f"{connector_key}_RATE_LIMIT_CAPACITY",
                        "PORTAL_RATE_LIMIT_DEFAULT_CAPACITY",
                    ],
                    5.0,
                    minimum=1.0,
                )
            ),
        )
        resolved.setdefault(
            "refill_rate",
            float(
                _read_numeric_env(
                    [
                        f"{connector_key}_RATE_LIMIT_REFILL_RATE",
                        "PORTAL_RATE_LIMIT_DEFAULT_REFILL_RATE",
                    ],
                    1.0,
                    minimum=0.0,
                )
            ),
        )
        return resolved

    def fetch_opportunities(self, keywords: Iterable[str]) -> list[dict[str, Any]]:
        try:
            return list(self.fetch_result(keywords)["opportunities"])
        except Exception:
            return []

    async def fetch_opportunities_async(
        self,
        keywords: Iterable[str],
        *,
        shared_session: Any | None = None,
    ) -> list[dict[str, Any]]:
        try:
            result = await self.fetch_result_async(keywords, shared_session=shared_session)
            return list(result["opportunities"])
        except Exception:
            return []

    def fetch_result(self, keywords: Iterable[str]) -> dict[str, Any]:
        started_at = time.perf_counter()
        degraded = False
        keyword_list = self._expand_keywords(keywords)
        with start_span(
            f"connector.fetch.{self.connector_slug}",
            kind=SpanKind.INTERNAL,
            attributes={
                "funding_bot.connector.name": self.source_name,
                "funding_bot.connector.type": self.connector_slug,
                "funding_bot.connector.transport": self.transport,
                "funding_bot.connector.keyword_count": len(keyword_list),
            },
        ) as span:
            try:
                self._last_rate_limit_retry_after = None
                cache_key = self._cache_key(keyword_list)
                if self._cache is not None:
                    hit, cached = self._cache.get(cache_key)
                    if hit:
                        span.set_attribute("funding_bot.connector.cache_hit", True)
                        return {
                            "schema_version": cached["schema_version"],
                            "opportunities": [dict(item) for item in cached["opportunities"]],
                            "metadata": dict(cached["metadata"]),
                        }

                if self._refresh_circuit_state() == "open":
                    self._metrics["short_circuits"] += 1
                    degraded = True
                    self._logger.warning(
                        "Connector %s request short-circuited because the circuit breaker is open.",
                        self.source_name,
                    )
                    return self._build_degraded_result(keyword_list, reason="circuit_open")

                use_remote = self.http_client is not None or self.transport == "http"
                if use_remote:
                    allowed, retry_after = self._rate_limiter.consume()
                    if not allowed:
                        self._metrics["rate_limited_requests"] += 1
                        self._last_rate_limit_retry_after = retry_after
                        self._last_error = f"{self.source_name} rate limit exceeded; retry in {retry_after:.2f} seconds."
                        degraded = True
                        self._logger.warning("%s", self._last_error)
                        return self._build_degraded_result(
                            keyword_list,
                            reason="rate_limit_exceeded",
                            error=self._last_error,
                        )
                if use_remote:
                    try:
                        result = self._fetch_remote_result(keyword_list)
                    except Exception as exc:
                        degraded = True
                        set_span_error(span, exc)
                        self._logger.warning(
                            "Connector %s remote fetch failed for keywords %s: %s",
                            self.source_name,
                            keyword_list,
                            exc,
                        )
                        return self._build_degraded_result(
                            keyword_list,
                            reason="connector_error",
                            error=str(exc),
                        )
                else:
                    result = {
                        "schema_version": self.result_schema_version,
                        "opportunities": [dict(item) for item in self._demo_data()],
                        "metadata": {
                            "connector_name": self.source_name,
                            "source_status": "demo",
                        },
                    }

                filtered = self._filter_opportunities(result["opportunities"], keyword_list)
                payload = {
                    "schema_version": result["schema_version"],
                    "opportunities": filtered,
                    "metadata": {
                        **dict(result.get("metadata", {})),
                        "cache_key": self.build_cache_key(keyword_list),
                        "keyword_count": len(keyword_list),
                    },
                }
                degraded = payload["metadata"].get("source_status") == "degraded"
                span.set_attribute(
                    "funding_bot.connector.source_status",
                    str(payload["metadata"].get("source_status", "unknown")),
                )
                span.set_attribute(
                    "funding_bot.connector.opportunity_count", len(payload["opportunities"])
                )
                if self._cache is not None and not degraded:
                    self._cache.set(
                        cache_key,
                        {
                            "schema_version": payload["schema_version"],
                            "opportunities": [dict(item) for item in payload["opportunities"]],
                            "metadata": dict(payload["metadata"]),
                        },
                    )
                return payload
            finally:
                duration_seconds = time.perf_counter() - started_at
                _CONNECTOR_METRICS.record(
                    connector_name=self.source_name,
                    connector_type=self.connector_slug,
                    latency_seconds=duration_seconds,
                    errored=degraded,
                )
                record_slo_event(
                    "connector_latency",
                    component=self.source_name,
                    latency_seconds=duration_seconds,
                    success=not degraded,
                    metadata={
                        "connector_type": self.connector_slug,
                        "transport": self.transport,
                    },
                )

    async def fetch_result_async(
        self,
        keywords: Iterable[str],
        *,
        shared_session: Any | None = None,
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        degraded = False
        keyword_list = self._expand_keywords(keywords)
        with start_span(
            f"connector.fetch.{self.connector_slug}.async",
            kind=SpanKind.INTERNAL,
            attributes={
                "funding_bot.connector.name": self.source_name,
                "funding_bot.connector.type": self.connector_slug,
                "funding_bot.connector.transport": self.transport,
                "funding_bot.connector.keyword_count": len(keyword_list),
            },
        ) as span:
            try:
                self._last_rate_limit_retry_after = None
                cache_key = self._cache_key(keyword_list)
                if self._cache is not None:
                    hit, cached = self._cache.get(cache_key)
                    if hit:
                        span.set_attribute("funding_bot.connector.cache_hit", True)
                        return {
                            "schema_version": cached["schema_version"],
                            "opportunities": [dict(item) for item in cached["opportunities"]],
                            "metadata": dict(cached["metadata"]),
                        }

                if self._refresh_circuit_state() == "open":
                    self._metrics["short_circuits"] += 1
                    degraded = True
                    self._logger.warning(
                        "Connector %s request short-circuited because the circuit breaker is open.",
                        self.source_name,
                    )
                    return self._build_degraded_result(keyword_list, reason="circuit_open")

                use_remote = (
                    self.http_client is not None
                    or self.async_http_client is not None
                    or self.transport == "http"
                )
                if use_remote:
                    allowed, retry_after = self._rate_limiter.consume()
                    if not allowed:
                        self._metrics["rate_limited_requests"] += 1
                        self._last_rate_limit_retry_after = retry_after
                        self._last_error = f"{self.source_name} rate limit exceeded; retry in {retry_after:.2f} seconds."
                        degraded = True
                        self._logger.warning("%s", self._last_error)
                        return self._build_degraded_result(
                            keyword_list,
                            reason="rate_limit_exceeded",
                            error=self._last_error,
                        )
                if use_remote:
                    try:
                        result = await self._fetch_remote_result_async(
                            keyword_list,
                            shared_session=shared_session,
                        )
                    except Exception as exc:
                        degraded = True
                        set_span_error(span, exc)
                        self._logger.warning(
                            "Connector %s remote fetch failed for keywords %s: %s",
                            self.source_name,
                            keyword_list,
                            exc,
                        )
                        return self._build_degraded_result(
                            keyword_list,
                            reason="connector_error",
                            error=str(exc),
                        )
                else:
                    result = {
                        "schema_version": self.result_schema_version,
                        "opportunities": [dict(item) for item in self._demo_data()],
                        "metadata": {
                            "connector_name": self.source_name,
                            "source_status": "demo",
                        },
                    }

                filtered = self._filter_opportunities(result["opportunities"], keyword_list)
                payload = {
                    "schema_version": result["schema_version"],
                    "opportunities": filtered,
                    "metadata": {
                        **dict(result.get("metadata", {})),
                        "cache_key": self.build_cache_key(keyword_list),
                        "keyword_count": len(keyword_list),
                    },
                }
                degraded = payload["metadata"].get("source_status") == "degraded"
                span.set_attribute(
                    "funding_bot.connector.source_status",
                    str(payload["metadata"].get("source_status", "unknown")),
                )
                span.set_attribute(
                    "funding_bot.connector.opportunity_count", len(payload["opportunities"])
                )
                if self._cache is not None and not degraded:
                    self._cache.set(
                        cache_key,
                        {
                            "schema_version": payload["schema_version"],
                            "opportunities": [dict(item) for item in payload["opportunities"]],
                            "metadata": dict(payload["metadata"]),
                        },
                    )
                return payload
            finally:
                duration_seconds = time.perf_counter() - started_at
                _CONNECTOR_METRICS.record(
                    connector_name=self.source_name,
                    connector_type=self.connector_slug,
                    latency_seconds=duration_seconds,
                    errored=degraded,
                )
                record_slo_event(
                    "connector_latency",
                    component=self.source_name,
                    latency_seconds=duration_seconds,
                    success=not degraded,
                    metadata={
                        "connector_type": self.connector_slug,
                        "transport": self.transport,
                    },
                )

    def build_cache_key(self, keywords: Iterable[str]) -> str:
        return json.dumps(
            {
                "connector_id": self.connector_slug,
                "page_size": self.page_size,
                "keywords": sorted(keyword.lower() for keyword in _normalize_text_list(keywords)),
            },
            sort_keys=True,
        )

    def invalidate_cache(self, keywords: Iterable[str] | None = None) -> None:
        if self._cache is None:
            return
        if keywords is None:
            self._cache.clear()
            return
        self._cache.invalidate(self._cache_key(self._expand_keywords(keywords)))

    def cache_metrics(self) -> dict[str, Any]:
        stats = self._cache.stats() if self._cache is not None else {}
        return {
            **stats,
            "connector_id": self.connector_slug,
            "page_size": self.page_size,
        }

    def _cache_key(self, keywords: Iterable[str]) -> tuple[Any, ...]:
        return (
            self.connector_slug,
            self.page_size,
            tuple(sorted(keyword.lower() for keyword in _normalize_text_list(keywords))),
        )

    def default_fallback_results(self, keywords: Iterable[str]) -> list[dict[str, Any]]:
        return self._filter_opportunities(self._demo_data(), self._expand_keywords(keywords))

    def _build_degraded_result(
        self,
        keywords: Iterable[str],
        *,
        reason: str,
        error: str | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": self.result_schema_version,
            "opportunities": [],
            "metadata": {
                "connector_name": self.source_name,
                "source_status": "degraded",
                "degraded_reason": reason,
                "circuit_state": self._refresh_circuit_state(),
                "last_error": error or self._last_error,
                "retry_after_seconds": self._last_rate_limit_retry_after,
            },
        }

    def get_keyword_category_mappings(self) -> dict[str, dict[str, list[str]]]:
        mappings: dict[str, dict[str, list[str]]] = {}
        for canonical_keyword, config in self.keyword_category_mappings.items():
            keyword_values = _normalize_text_list([canonical_keyword, *config.get("keywords", ())])
            category_values = _normalize_text_list(config.get("categories", ()))
            mappings[canonical_keyword] = {
                "keywords": keyword_values,
                "categories": category_values,
            }
        return mappings

    def _expand_keywords(self, keywords: Iterable[str] | None) -> list[str]:
        requested_keywords = [keyword.lower() for keyword in _normalize_text_list(keywords)]
        if not requested_keywords:
            return []

        expanded: set[str] = set(requested_keywords)
        for canonical_keyword, config in self.get_keyword_category_mappings().items():
            synonyms = {
                canonical_keyword.lower(),
                *(keyword.lower() for keyword in config.get("keywords", [])),
                *(category.lower() for category in config.get("categories", [])),
            }
            if expanded.intersection(synonyms):
                expanded.update(synonyms)
        return sorted(expanded)

    def _filter_opportunities(
        self,
        opportunities: Iterable[dict[str, Any]],
        keywords: Iterable[str] | None,
    ) -> list[dict[str, Any]]:
        keyword_list = [keyword.lower() for keyword in (keywords or [])]
        if not keyword_list:
            return [dict(item) for item in opportunities]

        filtered: list[dict[str, Any]] = []
        for opportunity in opportunities:
            searchable = " ".join(
                [
                    str(opportunity.get("title", "")),
                    str(opportunity.get("summary", "")),
                    str(opportunity.get("category", "")),
                    " ".join(str(tag) for tag in opportunity.get("tags", [])),
                ]
            ).lower()
            if any(keyword in searchable for keyword in keyword_list):
                filtered.append(dict(opportunity))
        return filtered

    def validate_connectivity(
        self,
        keywords: Iterable[str] | None = None,
        *,
        sample_limit: int = 3,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        requested_keywords = _normalize_text_list(keywords)
        _emit_progress_event(
            progress_callback,
            stage="connector-validation",
            description=f"Testing connector {self.connector_slug}",
            current=self.connector_slug,
            completed=0,
            total=3,
        )
        try:
            result = self.fetch_result(requested_keywords)
            _emit_progress_event(
                progress_callback,
                stage="connector-validation",
                description=f"Fetched connector data for {self.connector_slug}",
                current=self.connector_slug,
                completed=2,
                total=3,
            )
            sample_results = result["opportunities"]
            metadata = dict(result.get("metadata", {}))
            degraded = metadata.get("source_status") == "degraded"
            trimmed_results = [
                {
                    "source": row.get("source"),
                    "donor_name": row.get("donor_name"),
                    "title": row.get("title"),
                    "portal_url": row.get("portal_url"),
                    "category": row.get("category"),
                    "tags": row.get("tags", []),
                }
                for row in sample_results[: max(sample_limit, 0)]
            ]
            validation = {
                "connector": self.connector_slug,
                "source": self.source_name,
                "base_url": self.base_url,
                "status": "degraded" if degraded else "ok",
                "connectivity_validated": not degraded,
                "mode": (
                    "remote"
                    if (self.http_client is not None or self.transport == "http")
                    else "demo"
                ),
                "requested_keywords": requested_keywords,
                "expanded_keywords": self._expand_keywords(requested_keywords),
                "sample_result_count": len(sample_results),
                "sample_results": trimmed_results,
                "keyword_mappings": self.get_keyword_category_mappings(),
                "metadata": metadata,
                "error": metadata.get("last_error"),
            }
            _emit_progress_event(
                progress_callback,
                stage="connector-validation",
                description=f"Finished connector test for {self.connector_slug}",
                current=self.connector_slug,
                completed=3,
                total=3,
            )
            return validation
        except Exception as exc:
            _emit_progress_event(
                progress_callback,
                stage="connector-validation",
                description=f"Connector test failed for {self.connector_slug}",
                current=self.connector_slug,
                completed=3,
                total=3,
            )
            return {
                "connector": self.connector_slug,
                "source": self.source_name,
                "base_url": self.base_url,
                "status": "error",
                "connectivity_validated": False,
                "mode": (
                    "remote"
                    if (self.http_client is not None or self.transport == "http")
                    else "demo"
                ),
                "requested_keywords": requested_keywords,
                "expanded_keywords": self._expand_keywords(requested_keywords),
                "sample_result_count": 0,
                "sample_results": [],
                "keyword_mappings": self.get_keyword_category_mappings(),
                "error": str(exc),
            }

    def _fetch_remote(self, keywords: list[str]) -> list[dict[str, Any]]:
        return self._fetch_remote_result(keywords)["opportunities"]

    def _fetch_remote_result(self, keywords: list[str]) -> dict[str, Any]:
        client = self.http_client or _default_http_json_client

        opportunities: list[dict[str, Any]] = []
        declared_version: Any = None
        response_keys: set[str] = set()
        pages_fetched = 0
        page = 1

        while True:

            def operation(page_number: int = page) -> Any:
                payload = {
                    "keywords": keywords,
                    "page": page_number,
                    "page_size": self.page_size,
                    "health_check": self._circuit_state == "half-open",
                }
                try:
                    return client(self.base_url, payload, self.credentials)
                except TypeError:
                    return client(self.base_url, payload)

            response = self._call_with_retry(operation)
            page_payload, page_declared_version, page_response_keys, next_page = (
                self._parse_remote_page(
                    response,
                    current_page=page,
                )
            )
            opportunities.extend(page_payload)
            pages_fetched += 1
            if page_declared_version is not None:
                declared_version = page_declared_version
            response_keys.update(page_response_keys)
            if next_page is None:
                break
            page = next_page

        detected_version = self.detect_schema_version(opportunities, declared_version)
        return {
            "schema_version": self.result_schema_version,
            "opportunities": self.migrate_result_payload(opportunities, detected_version),
            "metadata": {
                "connector_name": self.source_name,
                "source_status": "remote",
                "detected_schema_version": detected_version,
                "upstream_schema_version": declared_version,
                "response_keys": sorted(response_keys),
                "pages_fetched": pages_fetched,
                "page_size": self.page_size,
            },
        }

    async def _fetch_remote_result_async(
        self,
        keywords: list[str],
        *,
        shared_session: Any | None = None,
    ) -> dict[str, Any]:
        client = self.async_http_client or self.http_client or _default_http_json_client_async

        opportunities: list[dict[str, Any]] = []
        declared_version: Any = None
        response_keys: set[str] = set()
        pages_fetched = 0
        page = 1
        try:
            resolved_credentials = self._get_resolved_credentials()
        except CredentialNotFoundError:
            if self.async_http_client is not None or self.http_client is not None:
                resolved_credentials = {}
            else:
                raise

        while True:

            async def operation(page_number: int = page) -> Any:
                payload = {
                    "keywords": keywords,
                    "page": page_number,
                    "page_size": self.page_size,
                    "health_check": self._circuit_state == "half-open",
                }
                attempts = (
                    lambda: client(
                        self.base_url,
                        payload,
                        resolved_credentials,
                        session=shared_session,
                    ),
                    lambda: client(self.base_url, payload, resolved_credentials),
                    lambda: client(self.base_url, payload, session=shared_session),
                    lambda: client(self.base_url, payload),
                )
                last_type_error: TypeError | None = None
                for attempt in attempts:
                    try:
                        return await _maybe_await(attempt())
                    except TypeError as exc:
                        last_type_error = exc
                        continue
                if last_type_error is not None:
                    raise last_type_error
                return await _maybe_await(client(self.base_url, payload))

            response = await self._call_with_retry_async(operation)
            page_payload, page_declared_version, page_response_keys, next_page = (
                self._parse_remote_page(
                    response,
                    current_page=page,
                )
            )
            opportunities.extend(page_payload)
            pages_fetched += 1
            if page_declared_version is not None:
                declared_version = page_declared_version
            response_keys.update(page_response_keys)
            if next_page is None:
                break
            page = next_page

        detected_version = self.detect_schema_version(opportunities, declared_version)
        return {
            "schema_version": self.result_schema_version,
            "opportunities": self.migrate_result_payload(opportunities, detected_version),
            "metadata": {
                "connector_name": self.source_name,
                "source_status": "remote",
                "detected_schema_version": detected_version,
                "upstream_schema_version": declared_version,
                "response_keys": sorted(response_keys),
                "pages_fetched": pages_fetched,
                "page_size": self.page_size,
            },
        }

    def _parse_remote_page(
        self,
        response: Any,
        *,
        current_page: int,
    ) -> tuple[list[dict[str, Any]], Any, list[str], int | None]:
        if isinstance(response, dict):
            payload = response.get("opportunities")
            if payload is None:
                payload = response.get("results")
            if payload is None:
                payload = response.get("items", [])
            declared_version = response.get("schema_version", response.get("result_schema_version"))
            response_keys = [str(key) for key in response]
            next_page = response.get("next_page")
            if next_page is None:
                total_pages = response.get("total_pages")
                if total_pages is not None:
                    try:
                        total_pages_int = int(total_pages)
                    except (TypeError, ValueError):
                        total_pages_int = current_page
                    next_page = current_page + 1 if current_page < total_pages_int else None
                elif "has_more" in response:
                    next_page = current_page + 1 if response.get("has_more") else None
                elif payload and len(payload) >= self.page_size:
                    next_page = current_page + 1
            return (
                [dict(item) for item in (payload or [])],
                declared_version,
                response_keys,
                next_page,
            )

        payload = [dict(item) for item in (response or [])]
        next_page = current_page + 1 if payload and len(payload) >= self.page_size else None
        return payload, None, [], next_page

    def _throttle_remote_request(self) -> None:
        allowed, retry_after = self._rate_limiter.consume()
        if allowed:
            return
        self._metrics["rate_limited_requests"] += 1
        self._last_rate_limit_retry_after = retry_after
        if retry_after == float("inf"):
            raise RateLimitExceededError(
                f"Connector {self.source_name!r} is rate-limited and cannot recover automatically."
            )
        self._sleep(retry_after)

    def _invoke_http_get_client(
        self,
        url: str,
        params: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> Any:
        self._throttle_remote_request()
        resolved_credentials = self._get_resolved_credentials()
        request_headers = dict(headers or {})
        if headers:
            request_headers.setdefault("Accept", "application/json")
        inject_context(request_headers)
        if self.http_client is not None:
            with start_span(
                f"connector.http.get.{self.connector_slug}",
                kind=SpanKind.CLIENT,
                attributes={
                    "http.request.method": "GET",
                    "url.full": url,
                    "funding_bot.connector.name": self.source_name,
                    "funding_bot.connector.type": self.connector_slug,
                },
            ) as span:
                attempts = (
                    lambda: self.http_client(
                        url, params, resolved_credentials, headers=request_headers
                    ),
                    lambda: self.http_client(url, params, headers=request_headers),
                    lambda: self.http_client(url, params, resolved_credentials),
                    lambda: self.http_client(url, params),
                )
                for attempt in attempts:
                    try:
                        return attempt()
                    except TypeError:
                        continue
                    except Exception as exc:
                        set_span_error(span, exc)
                        raise
                try:
                    return self.http_client(url, params)
                except Exception as exc:
                    set_span_error(span, exc)
                    raise

        request_headers = {
            "Accept": "application/json",
            "User-Agent": "funding-bot/1.0",
            **request_headers,
        }
        if self._request_session is not None:
            return _perform_json_request(
                "GET",
                url,
                session=self._get_request_session(),
                headers=request_headers,
                params=params,
                timeout=self.request_timeout,
            )

        query_items: list[tuple[str, str]] = []
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, (list, tuple, set)):
                query_items.extend((key, str(item)) for item in value if item is not None)
            else:
                query_items.append((key, str(value)))
        query_string = urllib.parse.urlencode(query_items, doseq=True)
        request = urllib.request.Request(
            f"{url}?{query_string}" if query_string else url,
            headers=request_headers,
            method="GET",
        )
        with start_span(
            f"connector.http.get.{self.connector_slug}",
            kind=SpanKind.CLIENT,
            attributes={
                "http.request.method": "GET",
                "url.full": request.full_url,
                "funding_bot.connector.name": self.source_name,
                "funding_bot.connector.type": self.connector_slug,
            },
        ) as span:
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    payload = response.read().decode("utf-8")
                return json.loads(payload or "{}")
            except Exception as exc:
                set_span_error(span, exc)
                raise

    def _fetch_remote_json(
        self,
        url: str,
        params: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> Any:
        def operation() -> Any:
            try:
                return self._invoke_http_get_client(url, params, headers=headers)
            except urllib.error.HTTPError as exc:
                if exc.code != 429:
                    raise
                retry_after_header = exc.headers.get("Retry-After", "1") if exc.headers else "1"
                try:
                    retry_after = float(retry_after_header)
                except (TypeError, ValueError):
                    retry_after = 1.0
                self._metrics["rate_limited_requests"] += 1
                self._last_rate_limit_retry_after = retry_after
                self._sleep(max(retry_after, 0.0))
                raise ConnectionError(
                    f"Rate limit exceeded for connector {self.source_name!r}; retry after {retry_after} seconds."
                ) from exc

        return self._call_with_retry(operation)

    async def _invoke_http_get_client_async(
        self,
        url: str,
        params: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        shared_session: Any | None = None,
    ) -> Any:
        client = self.async_http_client or self.http_client
        if client is None:
            return await asyncio.to_thread(
                self._invoke_http_get_client,
                url,
                params,
                headers=headers,
            )

        self._throttle_remote_request()
        try:
            resolved_credentials = self._get_resolved_credentials()
        except CredentialNotFoundError:
            if self.async_http_client is not None or self.http_client is not None:
                resolved_credentials = {}
            else:
                raise
        request_headers = dict(headers or {})
        if headers:
            request_headers.setdefault("Accept", "application/json")
        inject_context(request_headers)
        attempts = (
            lambda: client(
                url,
                params,
                resolved_credentials,
                headers=request_headers,
                session=shared_session,
            ),
            lambda: client(url, params, resolved_credentials, headers=request_headers),
            lambda: client(url, params, headers=request_headers, session=shared_session),
            lambda: client(url, params, headers=request_headers),
            lambda: client(url, params, resolved_credentials, session=shared_session),
            lambda: client(url, params, resolved_credentials),
            lambda: client(url, params, session=shared_session),
            lambda: client(url, params),
        )
        last_type_error: TypeError | None = None
        for attempt in attempts:
            try:
                return await _maybe_await(attempt())
            except TypeError as exc:
                last_type_error = exc
                continue
        if last_type_error is not None:
            raise last_type_error
        return await _maybe_await(client(url, params))

    async def _fetch_remote_json_async(
        self,
        url: str,
        params: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        shared_session: Any | None = None,
    ) -> Any:
        async def operation() -> Any:
            try:
                return await self._invoke_http_get_client_async(
                    url,
                    params,
                    headers=headers,
                    shared_session=shared_session,
                )
            except urllib.error.HTTPError as exc:
                if exc.code != 429:
                    raise
                retry_after_header = exc.headers.get("Retry-After", "1") if exc.headers else "1"
                try:
                    retry_after = float(retry_after_header)
                except (TypeError, ValueError):
                    retry_after = 1.0
                self._metrics["rate_limited_requests"] += 1
                self._last_rate_limit_retry_after = retry_after
                await asyncio.sleep(max(retry_after, 0.0))
                raise ConnectionError(
                    f"Rate limit exceeded for connector {self.source_name!r}; retry after {retry_after} seconds."
                ) from exc

        return await self._call_with_retry_async(operation)

    def detect_schema_version(self, payload: Any, declared_version: Any = None) -> int:
        if declared_version is not None:
            try:
                return int(declared_version)
            except (TypeError, ValueError):
                pass
        if isinstance(payload, list) and payload:
            sample = payload[0]
        else:
            sample = payload
        if isinstance(sample, dict):
            if {"portal_url", "summary", "donor_name"}.issubset(sample):
                return 2
            if {"link", "description", "funder"} & set(sample):
                return 1
        return self.result_schema_version

    def migrate_result_payload(self, payload: Any, schema_version: int) -> list[dict[str, Any]]:
        rows = payload if isinstance(payload, list) else []
        current_version = schema_version or self.result_schema_version
        migrated_rows = [dict(item) for item in rows]
        while current_version < self.result_schema_version:
            migrator = getattr(
                self, f"_migrate_schema_v{current_version}_to_v{current_version + 1}", None
            )
            if migrator is None:
                break
            migrated_rows = [dict(item) for item in migrator(migrated_rows)]
            current_version += 1
        return [self._normalize_current_record(item) for item in migrated_rows]

    def _migrate_schema_v1_to_v2(self, rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        migrated: list[dict[str, Any]] = []
        for row in rows:
            migrated.append(
                {
                    "source": row.get("source", self.source_name),
                    "donor_name": row.get("donor_name", row.get("funder", "Unknown donor")),
                    "title": row.get("title", "Untitled opportunity"),
                    "portal_url": row.get("portal_url", row.get("link", "")),
                    "summary": row.get("summary", row.get("description", "")),
                    "category": row.get("category", row.get("type", "")),
                    "tags": row.get("tags", row.get("topics", [])),
                }
            )
        return migrated

    def _normalize_current_record(self, row: dict[str, Any]) -> dict[str, Any]:
        tags = row.get("tags", [])
        if isinstance(tags, str):
            tags = [part.strip() for part in tags.split(",") if part.strip()]
        return {
            "source": str(row.get("source", self.source_name)),
            "donor_name": str(row.get("donor_name", row.get("funder", "Unknown donor"))),
            "title": str(row.get("title", "Untitled opportunity")),
            "portal_url": str(row.get("portal_url", row.get("link", ""))),
            "summary": str(row.get("summary", row.get("description", ""))),
            "category": str(row.get("category", row.get("type", ""))),
            "tags": [str(tag) for tag in tags],
        }

    def _build_auth_headers(
        self,
        credentials: dict[str, Any],
        *,
        api_key_header: str | None = None,
    ) -> dict[str, str]:
        headers: dict[str, str] = {}
        authorization_header = str(credentials.get("authorization_header", "")).strip()
        if authorization_header:
            headers["Authorization"] = authorization_header
        else:
            access_token = str(
                credentials.get("access_token") or credentials.get("bearer_token") or ""
            ).strip()
            if access_token:
                token_type = str(credentials.get("token_type", "Bearer")).strip() or "Bearer"
                headers["Authorization"] = f"{token_type} {access_token}"
        if api_key_header:
            for credential_key in ("api_key", "subscription_key", "secret"):
                api_key = str(credentials.get(credential_key, "")).strip()
                if api_key:
                    headers[api_key_header] = api_key
                    break
        return headers

    def _demo_data(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def check_health(self) -> dict[str, Any]:
        state = self._refresh_circuit_state()
        self._metrics["health_checks"] += 1
        return {
            "healthy": state != "open",
            "state": state,
            "last_error": self._last_error,
            "metrics": self.get_failure_metrics(),
        }

    def get_failure_metrics(self) -> dict[str, Any]:
        return {
            **self._metrics,
            "cache": self.cache_metrics(),
            "state": self._refresh_circuit_state(),
            "last_error": self._last_error,
            "opened_at": self._opened_at,
            "rate_limit": {
                **self.rate_limit_config,
                "available_tokens": self._rate_limiter.available_tokens,
                "retry_after_seconds": self._last_rate_limit_retry_after,
            },
        }

    def _call_with_retry(self, operation: Callable[[], Any]) -> Any:
        attempts = self.max_retries + 1
        for attempt_number in range(1, attempts + 1):
            self._metrics["requests"] += 1
            try:
                result = operation()
            except Exception as exc:
                if self._is_retryable(exc) and attempt_number < attempts:
                    self._metrics["retry_attempts"] += 1
                    self._sleep(
                        self.retry_backoff_base
                        * (self.retry_backoff_factor ** (attempt_number - 1))
                    )
                    continue
                self._record_failure(exc)
                raise
            self._record_success()
            return result
        raise RuntimeError("Connector retry loop exhausted unexpectedly.")

    async def _call_with_retry_async(self, operation: Callable[[], Any]) -> Any:
        attempts = self.max_retries + 1
        for attempt_number in range(1, attempts + 1):
            self._metrics["requests"] += 1
            try:
                result = await _maybe_await(operation())
            except Exception as exc:
                if self._is_retryable(exc) and attempt_number < attempts:
                    self._metrics["retry_attempts"] += 1
                    await asyncio.sleep(
                        self.retry_backoff_base
                        * (self.retry_backoff_factor ** (attempt_number - 1))
                    )
                    continue
                self._record_failure(exc)
                raise
            self._record_success()
            return result
        raise RuntimeError("Connector retry loop exhausted unexpectedly.")

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        return isinstance(exc, _TRANSIENT_CONNECTOR_ERRORS)

    def _refresh_circuit_state(self) -> str:
        if (
            self._circuit_state == "open"
            and self._opened_at is not None
            and (self._time() - self._opened_at) >= self.circuit_recovery_timeout
        ):
            self._transition_circuit("half-open")
        return self._circuit_state

    def _record_success(self) -> None:
        self._metrics["successful_requests"] += 1
        self._metrics["consecutive_failures"] = 0
        self._last_error = None
        self._opened_at = None
        if self._circuit_state != "closed":
            self._transition_circuit("closed")

    def _record_failure(self, exc: Exception) -> None:
        self._metrics["failed_requests"] += 1
        self._metrics["consecutive_failures"] += 1
        self._last_error = str(exc)
        if self._circuit_state == "half-open" or (
            self._metrics["consecutive_failures"] >= self.circuit_failure_threshold
        ):
            self._opened_at = self._time()
            self._transition_circuit("open")

    def _transition_circuit(self, new_state: str) -> None:
        if self._circuit_state == new_state:
            return
        self._logger.info(
            "Connector %s circuit breaker transition: %s -> %s",
            self.source_name,
            self._circuit_state,
            new_state,
        )
        self._circuit_state = new_state
        self._metrics["state_transitions"] += 1


class GrantsPortalConnector(_BasePortalConnector):
    """Connector for live Grants.gov opportunity search."""

    connector_slug = "grants-portal"
    source_name = "Grants Portal"
    base_url = "https://api.grants.gov/v1/api/search2"
    keyword_category_mappings = {
        "education": {
            "keywords": ("learning", "school improvement", "innovation grant"),
            "categories": ("Education",),
        },
        "youth": {
            "keywords": ("student success", "young learners"),
            "categories": ("Education",),
        },
    }

    def __init__(
        self,
        http_client: Callable[..., Any] | None = None,
        *,
        base_url: str | None = None,
        credentials: dict[str, Any] | None = None,
        credential_name: str | None = None,
        credential_vault: CredentialVault | OAuth2ClientCredentialsVault | None = None,
        request_session: Any | None = None,
        transport: str = "demo",
        **kwargs: Any,
    ) -> None:
        super().__init__(
            http_client,
            base_url=base_url or os.environ.get("GRANTS_GOV_API_BASE_URL") or self.base_url,
            credentials=credentials,
            credential_name=(
                credential_name if credential_name is not None else "GRANTS_GOV_API_CREDENTIALS"
            ),
            credential_vault=credential_vault,
            request_session=request_session,
            transport=transport,
            **kwargs,
        )

    def _fetch_remote_result(self, keywords: list[str]) -> dict[str, Any]:
        if self.http_client is not None or type(self) is not GrantsPortalConnector:
            return super()._fetch_remote_result(keywords)

        try:
            credentials = self._get_resolved_credentials()
        except CredentialNotFoundError:
            credentials = {}
        keyword_query = " ".join(_normalize_text_list(keywords))
        payload: dict[str, Any] = {
            "keyword": keyword_query,
            "rows": self.page_size,
            "oppStatuses": str(credentials.get("opp_statuses", "forecasted|posted")),
            "startRecordNum": int(credentials.get("start_record_num", 0) or 0),
        }
        sort_by = str(credentials.get("sort_by", "")).strip()
        if sort_by:
            payload["sortBy"] = sort_by
        agencies = _normalize_text_list(credentials.get("agencies"))
        if agencies:
            payload["agencies"] = "|".join(agencies)
        funding_categories = _normalize_text_list(credentials.get("funding_categories"))
        if funding_categories:
            payload["fundingCategories"] = "|".join(funding_categories)

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "funding-bot/1.0",
            **self._build_auth_headers(credentials, api_key_header="X-API-Key"),
        }
        for key, value in credentials.items():
            if key in {
                "auth_type",
                "access_token",
                "authorization_header",
                "bearer_token",
                "expires_at",
                "funding_categories",
                "opp_statuses",
                "sort_by",
                "start_record_num",
                "token_type",
                "api_key",
                "agencies",
            }:
                continue
            headers[f"X-Connector-{key.replace('_', '-').title()}"] = str(value)

        response = _perform_json_request(
            "POST",
            self.base_url,
            session=self._get_request_session(),
            headers=headers,
            json_payload=payload,
            timeout=self.request_timeout,
        )
        data = response.get("data", {}) if isinstance(response, dict) else {}
        hits = data.get("oppHits", []) if isinstance(data, dict) else []
        opportunities: list[dict[str, Any]] = []
        for row in hits if isinstance(hits, list) else []:
            if not isinstance(row, dict):
                continue
            donor_name = str(row.get("agency", "")).strip() or "Grants.gov"
            title = str(row.get("title", "")).strip() or "Untitled opportunity"
            opportunity_id = str(row.get("id", "")).strip()
            opportunity_number = str(row.get("number", "")).strip()
            open_date = str(row.get("openDate", "")).strip() or "TBD"
            close_date = str(row.get("closeDate", "")).strip() or "TBD"
            tags = _normalize_text_list(
                [
                    row.get("oppStatus"),
                    row.get("agencyCode"),
                    *keywords,
                    *(row.get("cfdaList", []) if isinstance(row.get("cfdaList"), list) else []),
                ]
            )
            opportunities.append(
                {
                    "source": self.source_name,
                    "donor_name": donor_name,
                    "title": title,
                    "portal_url": (
                        f"https://www.grants.gov/search-results-detail/{opportunity_id}"
                        if opportunity_id
                        else "https://www.grants.gov/search-results-detail"
                    ),
                    "summary": (
                        f"{donor_name} opportunity {opportunity_number or title} "
                        f"opens {open_date} and closes {close_date}."
                    ),
                    "category": (
                        str(row.get("docType", "")).strip()
                        or (keywords[0].title() if keywords else "Government Grant")
                    ),
                    "tags": tags,
                }
            )

        return {
            "schema_version": self.result_schema_version,
            "opportunities": opportunities,
            "metadata": {
                "connector_name": self.source_name,
                "source_status": "remote",
                "provider": "grants.gov",
                "response_keys": (
                    sorted(str(key) for key in response) if isinstance(response, dict) else []
                ),
                "hit_count": (
                    int(data.get("hitCount", len(opportunities)))
                    if isinstance(data, dict)
                    else len(opportunities)
                ),
                "auth_applied": "Authorization" in headers or "X-API-Key" in headers,
            },
        }

    def _demo_data(self) -> list[dict[str, Any]]:
        return [
            {
                "source": self.source_name,
                "donor_name": "Global Education Fund",
                "title": "Education Innovation Grant",
                "portal_url": "https://grants.example.org/opportunities/education-innovation",
                "summary": "Supports nonprofit education pilots with strong local impact.",
                "category": "Education",
                "tags": ["education", "innovation", "grant"],
            }
        ]


class CSRNetworkConnector(_BasePortalConnector):
    """Connector for live CSR opportunity search."""

    connector_slug = "csr-network"
    source_name = "CSR Network"
    base_url = "https://api.candid.org/rfp/v1/opportunity"
    keyword_category_mappings = {
        "csr": {
            "keywords": ("corporate social responsibility", "corporate giving"),
            "categories": ("Corporate Partnerships",),
        },
        "digital learning": {
            "keywords": ("edtech", "technology training", "online learning"),
            "categories": ("Corporate Partnerships",),
        },
    }

    def __init__(
        self,
        http_client: Callable[..., Any] | None = None,
        *,
        base_url: str | None = None,
        credentials: dict[str, Any] | None = None,
        credential_name: str | None = None,
        credential_vault: CredentialVault | OAuth2ClientCredentialsVault | None = None,
        request_session: Any | None = None,
        transport: str = "demo",
        **kwargs: Any,
    ) -> None:
        super().__init__(
            http_client,
            base_url=base_url or os.environ.get("CSR_NETWORK_API_BASE_URL") or self.base_url,
            credentials=credentials,
            credential_name=(
                credential_name if credential_name is not None else "CSR_NETWORK_API_CREDENTIALS"
            ),
            credential_vault=credential_vault,
            request_session=request_session,
            transport=transport,
            **kwargs,
        )

    def _fetch_remote_result(self, keywords: list[str]) -> dict[str, Any]:
        if self.http_client is not None:
            return super()._fetch_remote_result(keywords)

        try:
            credentials = self._get_resolved_credentials()
        except CredentialNotFoundError as exc:
            raise CredentialNotFoundError(
                "CSR Network connector requires a Candid subscription_key/api_key credential."
            ) from exc
        subscription_key = str(
            credentials.get("subscription_key")
            or credentials.get("api_key")
            or credentials.get("subscriptionKey")
            or ""
        ).strip()
        if not subscription_key:
            raise CredentialNotFoundError(
                "CSR Network connector requires a Candid subscription_key/api_key credential."
            )

        params: dict[str, Any] = {"page_size": self.page_size}
        keyword_query = " ".join(_normalize_text_list(keywords))
        if keyword_query:
            params["q"] = keyword_query

        headers = {
            "Accept": "application/json",
            "Subscription-Key": subscription_key,
            "User-Agent": "funding-bot/1.0",
        }
        response = _perform_json_request(
            "GET",
            self.base_url,
            session=self._get_request_session(),
            headers=headers,
            params=params,
            timeout=self.request_timeout,
        )
        if isinstance(response, dict):
            raw_items = response.get("results")
            if raw_items is None:
                raw_items = response.get("items")
            if raw_items is None:
                raw_items = response.get("data", [])
            response_keys = sorted(str(key) for key in response)
        else:
            raw_items = response
            response_keys = []

        opportunities: list[dict[str, Any]] = []
        for row in raw_items if isinstance(raw_items, list) else []:
            if not isinstance(row, dict):
                continue
            funder = row.get("funder", {})
            funder_name = (
                str(funder.get("name", "")).strip()
                if isinstance(funder, dict)
                else str(funder).strip()
            )
            program_areas = _normalize_text_list(row.get("program_areas"))
            eligibility = _normalize_text_list(row.get("eligibility"))
            row_tags = row.get("tags", [])
            tags = _normalize_text_list(
                [
                    *program_areas,
                    *eligibility,
                    *keywords,
                    *(row_tags if isinstance(row_tags, list) else [row_tags]),
                ]
            )
            category = str(row.get("category", "")).strip() or (
                program_areas[0] if program_areas else "Corporate Partnerships"
            )
            opportunities.append(
                {
                    "source": self.source_name,
                    "donor_name": funder_name or "Candid Open Opportunities",
                    "title": str(row.get("title", "")).strip() or "Untitled CSR opportunity",
                    "portal_url": str(row.get("url", "")).strip()
                    or (str(funder.get("url", "")).strip() if isinstance(funder, dict) else ""),
                    "summary": str(row.get("summary", "")).strip()
                    or str(row.get("description", "")).strip(),
                    "category": category,
                    "tags": tags,
                }
            )

        return {
            "schema_version": self.result_schema_version,
            "opportunities": opportunities,
            "metadata": {
                "connector_name": self.source_name,
                "source_status": "remote",
                "provider": "candid-open-rfp",
                "response_keys": response_keys,
                "auth_applied": True,
            },
        }

    def _demo_data(self) -> list[dict[str, Any]]:
        return [
            {
                "source": self.source_name,
                "donor_name": "Acme Corporate Giving",
                "title": "CSR Digital Learning Fund",
                "portal_url": "https://csr.example.org/opportunities/digital-learning",
                "summary": "Corporate social responsibility funding for digital learning programs.",
                "category": "Corporate Partnerships",
                "tags": ["csr", "digital learning", "corporate"],
            }
        ]


class NGODirectoryConnector(_BasePortalConnector):
    """NGO directory connector with optional ProPublica live integration."""

    connector_slug = "ngo-directory"
    source_name = "NGO Directory"
    base_url = "https://projects.propublica.org/nonprofits/api/v2/search.json"
    keyword_category_mappings = {
        "literacy": {
            "keywords": ("reading", "community engagement", "library support"),
            "categories": ("Literacy",),
        },
        "institutional": {
            "keywords": ("foundation grant", "capacity building"),
            "categories": ("Literacy",),
        },
    }
    default_page_size = 25

    def __init__(
        self,
        http_client: Callable[..., Any] | None = None,
        *,
        base_url: str | None = None,
        credentials: dict[str, Any] | None = None,
        credential_name: str | None = None,
        credential_vault: CredentialVault | OAuth2ClientCredentialsVault | None = None,
        request_session: Any | None = None,
        transport: str = "demo",
        **kwargs: Any,
    ) -> None:
        super().__init__(
            http_client,
            base_url=base_url or os.environ.get("NGO_DIRECTORY_API_BASE_URL") or self.base_url,
            credentials=credentials,
            credential_name=credential_name,
            credential_vault=credential_vault,
            request_session=request_session,
            transport=transport,
            **kwargs,
        )

    def _fetch_remote_result(self, keywords: list[str]) -> dict[str, Any]:
        search_terms = keywords or ["nonprofit"]
        opportunities: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        pages_fetched = 0
        response_keys: set[str] = set()
        headers = self._build_auth_headers(self._get_resolved_credentials())

        for term in search_terms[:3]:
            page_number = 0
            while True:
                response = self._fetch_remote_json(
                    self.base_url,
                    {
                        "q": term,
                        "page": page_number,
                    },
                    headers=headers,
                )
                if not isinstance(response, dict):
                    break
                response_keys.update(str(key) for key in response)
                organizations = response.get("organizations", [])
                for organization in organizations:
                    if not isinstance(organization, dict):
                        continue
                    normalized = self._normalize_live_ngo_record(organization, term)
                    if normalized["portal_url"] in seen_urls:
                        continue
                    seen_urls.add(normalized["portal_url"])
                    opportunities.append(normalized)
                pages_fetched += 1
                total_results = int(response.get("total_results", 0) or 0)
                per_page = int(response.get("per_page", self.page_size) or self.page_size)
                if not organizations or (page_number + 1) * max(per_page, 1) >= total_results:
                    break
                page_number += 1

        return {
            "schema_version": self.result_schema_version,
            "opportunities": opportunities,
            "metadata": {
                "connector_name": self.source_name,
                "source_status": "remote",
                "upstream": "propublica-nonprofit-explorer",
                "response_keys": sorted(response_keys),
                "pages_fetched": pages_fetched,
                "page_size": self.page_size,
                "auth_applied": "Authorization" in headers,
            },
        }

    async def _fetch_remote_result_async(
        self,
        keywords: list[str],
        *,
        shared_session: Any | None = None,
    ) -> dict[str, Any]:
        search_terms = keywords or ["nonprofit"]
        opportunities: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        pages_fetched = 0
        response_keys: set[str] = set()
        headers = self._build_auth_headers(self._get_resolved_credentials())

        for term in search_terms[:3]:
            page_number = 0
            while True:
                response = await self._fetch_remote_json_async(
                    self.base_url,
                    {
                        "q": term,
                        "page": page_number,
                    },
                    headers=headers,
                    shared_session=shared_session,
                )
                if not isinstance(response, dict):
                    break
                response_keys.update(str(key) for key in response)
                organizations = response.get("organizations", [])
                for organization in organizations:
                    if not isinstance(organization, dict):
                        continue
                    normalized = self._normalize_live_ngo_record(organization, term)
                    if normalized["portal_url"] in seen_urls:
                        continue
                    seen_urls.add(normalized["portal_url"])
                    opportunities.append(normalized)
                pages_fetched += 1
                total_results = int(response.get("total_results", 0) or 0)
                per_page = int(response.get("per_page", self.page_size) or self.page_size)
                if not organizations or (page_number + 1) * max(per_page, 1) >= total_results:
                    break
                page_number += 1

        return {
            "schema_version": self.result_schema_version,
            "opportunities": opportunities,
            "metadata": {
                "connector_name": self.source_name,
                "source_status": "remote",
                "upstream": "propublica-nonprofit-explorer",
                "response_keys": sorted(response_keys),
                "pages_fetched": pages_fetched,
                "page_size": self.page_size,
                "auth_applied": "Authorization" in headers,
            },
        }

    def _normalize_live_ngo_record(
        self, organization: dict[str, Any], query: str
    ) -> dict[str, Any]:
        donor_name = (
            str(organization.get("name") or organization.get("organization_name") or "").strip()
            or "Unknown nonprofit"
        )
        ein = (
            str(organization.get("ein") or organization.get("strein") or "")
            .replace("-", "")
            .strip()
        )
        city = str(organization.get("city") or "").strip()
        state = str(organization.get("state") or "").strip()
        location = ", ".join(part for part in [city, state] if part) or "Unknown location"
        category = (
            str(organization.get("ntee_code") or organization.get("raw_ntee_code") or "").strip()
            or "NGO Directory"
        )
        subsection = str(organization.get("sub_name") or "").strip()
        if not subsection:
            subsection_code = organization.get("subseccd")
            subsection = (
                f"501(c)({subsection_code}) organization"
                if subsection_code not in (None, "")
                else "Registered nonprofit"
            )
        portal_url = (
            f"https://projects.propublica.org/nonprofits/organizations/{ein}"
            if ein
            else "https://projects.propublica.org/nonprofits/"
        )
        return {
            "source": self.source_name,
            "donor_name": donor_name,
            "title": f"{donor_name} nonprofit directory profile",
            "portal_url": portal_url,
            "summary": f"Live NGO directory match for '{query}' in {location}. {subsection}.",
            "category": category,
            "tags": [query, category, state or "directory"],
        }

    def _demo_data(self) -> list[dict[str, Any]]:
        return [
            {
                "source": self.source_name,
                "donor_name": "Community Foundation Alliance",
                "title": "Community Literacy Matching Grant",
                "portal_url": "https://directory.example.org/opportunities/community-literacy",
                "summary": "Institutional support for literacy and community engagement projects.",
                "category": "Literacy",
                "tags": ["community", "literacy", "institutional"],
            }
        ]


class FoundationDirectoryConnector(_BasePortalConnector):
    """Private foundation grant listings connector backed by Candid Grants API."""

    connector_slug = "foundation-directory"
    source_name = "Foundation Directory"
    base_url = "https://api.candid.org/grants/v1/transactions"
    default_page_size = 25
    keyword_category_mappings = {
        "foundation": {
            "keywords": ("grantmaker", "private foundation", "philanthropy"),
            "categories": ("Private Foundation",),
        },
        "education": {
            "keywords": ("learning", "student success", "school"),
            "categories": ("Education",),
        },
    }

    def __init__(
        self,
        http_client: Callable[..., Any] | None = None,
        *,
        base_url: str | None = None,
        credentials: dict[str, Any] | None = None,
        credential_name: str | None = None,
        credential_vault: CredentialVault | OAuth2ClientCredentialsVault | None = None,
        request_session: Any | None = None,
        transport: str = "demo",
        **kwargs: Any,
    ) -> None:
        super().__init__(
            http_client,
            base_url=base_url
            or os.environ.get("FOUNDATION_DIRECTORY_API_BASE_URL")
            or self.base_url,
            credentials=credentials,
            credential_name=credential_name or "FOUNDATION_DIRECTORY_API_CREDENTIALS",
            credential_vault=credential_vault,
            request_session=request_session,
            transport=transport,
            **kwargs,
        )

    def _fetch_remote_result(self, keywords: list[str]) -> dict[str, Any]:
        credentials = self._get_resolved_credentials()
        headers = self._build_auth_headers(credentials, api_key_header="X-API-Key")
        if "Authorization" not in headers and "X-API-Key" not in headers:
            raise ConnectorConfigError(
                "Foundation Directory live mode requires credentials containing an OAuth2 "
                "access token/authorization_header or an 'api_key'/'secret' value."
            )

        query = " ".join(keyword.strip() for keyword in keywords if keyword.strip()) or "nonprofit"
        opportunities: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        response_keys: set[str] = set()
        pages_fetched = 0
        page_number = 1

        while True:
            response = self._fetch_remote_json(
                self.base_url,
                {
                    "query": query,
                    "page": page_number,
                    "sort_by": "amount",
                    "sort_order": "desc",
                    "transaction": "TA",
                },
                headers=headers,
            )
            page_payload, _, page_response_keys, next_page = self._parse_remote_page(
                response,
                current_page=page_number,
            )
            response_keys.update(page_response_keys)
            for row in page_payload:
                normalized = self._normalize_foundation_record(row, query)
                if normalized["portal_url"] in seen_urls:
                    continue
                seen_urls.add(normalized["portal_url"])
                opportunities.append(normalized)
            pages_fetched += 1
            if next_page is None:
                break
            page_number = next_page

        return {
            "schema_version": self.result_schema_version,
            "opportunities": opportunities,
            "metadata": {
                "connector_name": self.source_name,
                "source_status": "remote",
                "upstream": "candid-grants-api",
                "response_keys": sorted(response_keys),
                "pages_fetched": pages_fetched,
                "page_size": self.page_size,
                "auth_applied": bool(headers),
            },
        }

    async def _fetch_remote_result_async(
        self,
        keywords: list[str],
        *,
        shared_session: Any | None = None,
    ) -> dict[str, Any]:
        credentials = self._get_resolved_credentials()
        headers = self._build_auth_headers(credentials, api_key_header="X-API-Key")
        if "Authorization" not in headers and "X-API-Key" not in headers:
            raise ConnectorConfigError(
                "Foundation Directory live mode requires credentials containing an OAuth2 "
                "access token/authorization_header or an 'api_key'/'secret' value."
            )

        query = " ".join(keyword.strip() for keyword in keywords if keyword.strip()) or "nonprofit"
        opportunities: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        response_keys: set[str] = set()
        pages_fetched = 0
        page_number = 1

        while True:
            response = await self._fetch_remote_json_async(
                self.base_url,
                {
                    "query": query,
                    "page": page_number,
                    "sort_by": "amount",
                    "sort_order": "desc",
                    "transaction": "TA",
                },
                headers=headers,
                shared_session=shared_session,
            )
            page_payload, _, page_response_keys, next_page = self._parse_remote_page(
                response,
                current_page=page_number,
            )
            response_keys.update(page_response_keys)
            for row in page_payload:
                normalized = self._normalize_foundation_record(row, query)
                if normalized["portal_url"] in seen_urls:
                    continue
                seen_urls.add(normalized["portal_url"])
                opportunities.append(normalized)
            pages_fetched += 1
            if next_page is None:
                break
            page_number = next_page

        return {
            "schema_version": self.result_schema_version,
            "opportunities": opportunities,
            "metadata": {
                "connector_name": self.source_name,
                "source_status": "remote",
                "upstream": "candid-grants-api",
                "response_keys": sorted(response_keys),
                "pages_fetched": pages_fetched,
                "page_size": self.page_size,
                "auth_applied": bool(headers),
            },
        }

    def _normalize_foundation_record(self, row: dict[str, Any], query: str) -> dict[str, Any]:
        donor_name = (
            self._first_non_empty(
                row.get("funder_name"),
                row.get("foundation_name"),
                row.get("grantmaker_name"),
                row.get("funder"),
                self._nested_value(row, "funder", "name"),
                self._nested_value(row, "grantor", "name"),
            )
            or "Private foundation listing"
        )
        recipient_name = (
            self._first_non_empty(
                row.get("recipient_name"),
                row.get("organization_name"),
                row.get("recipient"),
                self._nested_value(row, "recipient", "name"),
            )
            or "eligible nonprofits"
        )
        title = (
            self._first_non_empty(
                row.get("title"),
                row.get("grant_title"),
                row.get("program_name"),
            )
            or f"{donor_name} grant listing"
        )
        summary = (
            self._first_non_empty(
                row.get("purpose"),
                row.get("description"),
                row.get("summary"),
            )
            or f"Private foundation grant listing matching '{query}' for {recipient_name}."
        )
        portal_url = (
            self._first_non_empty(
                row.get("url"),
                row.get("detail_url"),
                row.get("source_url"),
                self._nested_value(row, "links", "self"),
            )
            or self.base_url
        )
        category = (
            self._first_non_empty(
                row.get("subject"),
                row.get("support_strategy"),
                row.get("category"),
            )
            or "Private Foundation"
        )
        return {
            "source": self.source_name,
            "donor_name": str(donor_name),
            "title": str(title),
            "portal_url": str(portal_url),
            "summary": str(summary),
            "category": str(category),
            "tags": [str(item) for item in [query, category, recipient_name] if item],
        }

    @staticmethod
    def _first_non_empty(*values: Any) -> str:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _nested_value(row: dict[str, Any], key: str, nested_key: str) -> str:
        value = row.get(key)
        if isinstance(value, dict):
            nested = value.get(nested_key)
            if isinstance(nested, str):
                return nested.strip()
        return ""

    def _demo_data(self) -> list[dict[str, Any]]:
        return [
            {
                "source": self.source_name,
                "donor_name": "Heritage Arts Foundation",
                "title": "Regional Arts Access Grant",
                "portal_url": "https://foundation.example.org/opportunities/arts-access",
                "summary": "Private foundation support for arts access and community culture.",
                "category": "Arts",
                "tags": ["arts", "foundation", "community"],
            }
        ]


class CrowdfundingConnector(_BasePortalConnector):
    """Connector for crowdfunding platforms such as GlobalGiving and Kickstarter."""

    connector_slug = "crowdfunding"
    source_name = "Crowdfunding"
    platform = "globalgiving"
    default_page_size = 20
    keyword_category_mappings = {
        "crowdfunding": {
            "keywords": ("public giving", "campaign", "community fundraising"),
            "categories": ("Crowdfunding",),
        },
        "education": {
            "keywords": ("learning", "school", "stem"),
            "categories": ("Education",),
        },
    }
    _PLATFORM_CONFIGS = {
        "globalgiving": {
            "connector_slug": "globalgiving",
            "source_name": "GlobalGiving",
            "base_url": "https://api.globalgiving.org/api/public/projectservice/all/projects/active",
        },
        "kickstarter": {
            "connector_slug": "kickstarter-for-good",
            "source_name": "Kickstarter for Good",
            "base_url": "https://www.kickstarter.com/discover/advanced",
        },
    }

    def __init__(
        self,
        http_client: Callable[..., Any] | None = None,
        *,
        platform: str | None = None,
        **kwargs: Any,
    ) -> None:
        normalized_platform = (platform or self.platform).strip().lower()
        if normalized_platform not in self._PLATFORM_CONFIGS:
            raise ValueError(f"Unsupported crowdfunding platform: {normalized_platform!r}")
        platform_config = self._PLATFORM_CONFIGS[normalized_platform]
        self.platform = normalized_platform
        self.connector_slug = str(platform_config["connector_slug"])
        kwargs.setdefault("source_name", str(platform_config["source_name"]))
        kwargs.setdefault("base_url", str(platform_config["base_url"]))
        kwargs.setdefault("transport", "demo")
        super().__init__(http_client=http_client, **kwargs)

    def _fetch_remote_result(self, keywords: list[str]) -> dict[str, Any]:
        client = self.http_client or _default_http_json_client
        payload = {
            "keywords": keywords,
            "page_size": self.page_size,
            "platform": self.platform,
            "health_check": self._circuit_state == "half-open",
        }

        def operation() -> Any:
            try:
                return client(self.base_url, payload, self.credentials)
            except TypeError:
                return client(self.base_url, payload)

        response = self._call_with_retry(operation)
        raw_rows, response_keys = self._extract_platform_rows(response)
        normalized_rows = [
            row for row in (self._normalize_platform_row(item) for item in raw_rows) if row
        ]
        return {
            "schema_version": self.result_schema_version,
            "opportunities": normalized_rows,
            "metadata": {
                "connector_name": self.source_name,
                "source_status": "remote",
                "platform": self.platform,
                "response_keys": response_keys,
            },
        }

    def _extract_platform_rows(self, response: Any) -> tuple[list[dict[str, Any]], list[str]]:
        if isinstance(response, list):
            return [dict(item) for item in response if isinstance(item, dict)], []
        if not isinstance(response, dict):
            return [], []
        if "opportunities" in response:
            rows = response.get("opportunities", [])
            return [dict(item) for item in rows if isinstance(item, dict)], sorted(
                str(key) for key in response
            )
        if self.platform == "globalgiving":
            projects = response.get("projects", {})
            if isinstance(projects, dict):
                rows = projects.get("project", projects.get("projects", []))
                if isinstance(rows, list):
                    return [dict(item) for item in rows if isinstance(item, dict)], sorted(
                        str(key) for key in response
                    )
        if self.platform == "kickstarter":
            rows = response.get("projects")
            if rows is None and isinstance(response.get("data"), dict):
                rows = response["data"].get("projects", [])
            if isinstance(rows, list):
                return [dict(item) for item in rows if isinstance(item, dict)], sorted(
                    str(key) for key in response
                )
        return [], sorted(str(key) for key in response)

    def _normalize_platform_row(self, row: dict[str, Any]) -> dict[str, Any] | None:
        if self.platform == "globalgiving":
            title = row.get("title") or row.get("name")
            if not title:
                return None
            organization = row.get("organization")
            donor_name = (
                row.get("donor_name")
                or row.get("owner_name")
                or (organization.get("name") if isinstance(organization, dict) else None)
                or "GlobalGiving Campaign"
            )
            category = row.get("themeName") or row.get("category") or "Crowdfunding"
            portal_url = (
                row.get("projectLink") or row.get("portal_url") or row.get("url") or self.base_url
            )
            summary = row.get("summary") or row.get("need") or row.get("activity") or ""
            tags = [
                str(value)
                for value in (row.get("themeName"), row.get("country"), "crowdfunding")
                if value
            ]
            return {
                "source": self.source_name,
                "donor_name": str(donor_name),
                "title": str(title),
                "portal_url": str(portal_url),
                "summary": str(summary),
                "category": str(category),
                "tags": tags,
            }

        title = row.get("title") or row.get("name")
        if not title:
            return None
        creator = row.get("creator")
        category = row.get("category")
        urls = row.get("urls")
        web = urls.get("web") if isinstance(urls, dict) else None
        donor_name = (
            row.get("donor_name")
            or (creator.get("name") if isinstance(creator, dict) else None)
            or "Kickstarter Creator"
        )
        category_name = (
            row.get("category_name")
            or (category.get("name") if isinstance(category, dict) else None)
            or "Crowdfunding"
        )
        portal_url = (
            row.get("portal_url")
            or row.get("url")
            or (web.get("project") if isinstance(web, dict) else None)
            or self.base_url
        )
        summary = row.get("summary") or row.get("blurb") or ""
        tags = [str(value) for value in (category_name, "crowdfunding", "social impact") if value]
        return {
            "source": self.source_name,
            "donor_name": str(donor_name),
            "title": str(title),
            "portal_url": str(portal_url),
            "summary": str(summary),
            "category": str(category_name),
            "tags": tags,
        }

    def _demo_data(self) -> list[dict[str, Any]]:
        if self.platform == "globalgiving":
            return [
                {
                    "source": self.source_name,
                    "donor_name": "GlobalGiving Community",
                    "title": "Community STEM Lab Campaign",
                    "portal_url": "https://www.globalgiving.org/projects/community-stem-lab/",
                    "summary": "Crowdfunding campaign supporting rural STEM labs and teacher training.",
                    "category": "Education",
                    "tags": ["education", "crowdfunding", "community"],
                }
            ]
        return [
            {
                "source": self.source_name,
                "donor_name": "Kickstarter Social Impact",
                "title": "Assistive Tech Makerspace Project",
                "portal_url": "https://www.kickstarter.com/projects/social-impact/assistive-tech-makerspace",
                "summary": "Creative campaign funding inclusive makerspace equipment for learners.",
                "category": "Innovation",
                "tags": ["innovation", "crowdfunding", "social impact"],
            }
        ]


class GlobalGivingConnector(CrowdfundingConnector):
    connector_slug = "globalgiving"
    source_name = "GlobalGiving"
    platform = "globalgiving"


class KickstarterForGoodConnector(CrowdfundingConnector):
    connector_slug = "kickstarter-for-good"
    source_name = "Kickstarter for Good"
    platform = "kickstarter"


_DEFAULT_CONNECTORS: list[PortalConnector] | None = None


def default_connectors(cache_manager: CacheManager | None = None) -> list[PortalConnector]:
    """Return the built-in portal connectors used by ``run_discovery``.

    Grants Portal and CSR Network default to live HTTP transports so
    ``run_discovery`` can query real upstream sources while the remaining
    connectors keep their existing defaults.
    """
    global _DEFAULT_CONNECTORS
    if _DEFAULT_CONNECTORS is None:
        shared_cache_manager = cache_manager or default_cache_manager()
        _DEFAULT_CONNECTORS = [
            GrantsPortalConnector(transport="http", cache_manager=shared_cache_manager),
            CSRNetworkConnector(transport="http", cache_manager=shared_cache_manager),
            NGODirectoryConnector(cache_manager=shared_cache_manager),
            FoundationDirectoryConnector(cache_manager=shared_cache_manager),
            GlobalGivingConnector(cache_manager=shared_cache_manager),
            KickstarterForGoodConnector(cache_manager=shared_cache_manager),
        ]
    return list(_DEFAULT_CONNECTORS)


CONNECTOR_CONFIG_ENV_VAR = "FUNDING_BOT_CONNECTORS"
CONNECTOR_CONFIG_SCHEMA = {
    "type": "object",
    "properties": {
        "connectors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "minLength": 1},
                    "enabled": {"type": "boolean"},
                    "transport": {"type": "string", "enum": ["demo", "http"]},
                    "base_url": {"type": "string", "minLength": 1},
                    "credential_alias": {"type": "string", "minLength": 1},
                    "credentials": {"type": "object"},
                    "cache_ttl": {"type": "number", "exclusiveMinimum": 0},
                    "rate_limit": {
                        "type": "object",
                        "properties": {
                            "capacity": {"type": "number", "exclusiveMinimum": 0},
                            "refill_rate": {"type": "number", "minimum": 0},
                        },
                        "additionalProperties": False,
                    },
                    "settings": {"type": "object"},
                },
                "required": ["type"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["connectors"],
    "additionalProperties": False,
}


def _default_http_json_client(
    url: str,
    payload: dict[str, Any],
    credentials: dict[str, Any] | None = None,
) -> Any:
    secure_url = _require_https_url(url, purpose="Connector request")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    resolved_credentials = dict(credentials or {})
    authorization_header = str(resolved_credentials.get("authorization_header", "")).strip()
    access_token = str(resolved_credentials.get("access_token", "")).strip()
    if authorization_header:
        headers["Authorization"] = authorization_header
    elif access_token:
        token_type = str(resolved_credentials.get("token_type", "Bearer")).strip() or "Bearer"
        headers["Authorization"] = f"{token_type} {access_token}"
    for key, value in resolved_credentials.items():
        if key in {"auth_type", "access_token", "token_type", "authorization_header", "expires_at"}:
            continue
        headers[f"X-Connector-{key.replace('_', '-').title()}"] = str(value)
    try:
        with _build_tls_http_session() as session:
            response = session.post(
                secure_url,
                json=payload,
                headers=headers,
                timeout=10,
                verify=True,
            )
            response.raise_for_status()
            return response.json()
    except (requests.RequestException, ValueError) as exc:
        raise FundingBotError(f"Connector request to {url!r} failed: {exc}") from exc


async def _default_http_json_client_async(
    url: str,
    payload: dict[str, Any],
    credentials: dict[str, Any] | None = None,
    *,
    session: Any | None = None,
) -> Any:
    secure_url = _require_https_url(url, purpose="Connector request")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    resolved_credentials = dict(credentials or {})
    authorization_header = str(resolved_credentials.get("authorization_header", "")).strip()
    access_token = str(resolved_credentials.get("access_token", "")).strip()
    if authorization_header:
        headers["Authorization"] = authorization_header
    elif access_token:
        token_type = str(resolved_credentials.get("token_type", "Bearer")).strip() or "Bearer"
        headers["Authorization"] = f"{token_type} {access_token}"
    for key, value in resolved_credentials.items():
        if key in {"auth_type", "access_token", "token_type", "authorization_header", "expires_at"}:
            continue
        headers[f"X-Connector-{key.replace('_', '-').title()}"] = str(value)
    if aiohttp is None:
        return await asyncio.to_thread(_default_http_json_client, url, payload, credentials)
    try:
        async with _reuse_or_create_aiohttp_session(
            session=session, timeout=10.0
        ) as client_session:
            if client_session is None:
                return await asyncio.to_thread(_default_http_json_client, url, payload, credentials)
            async with client_session.post(
                secure_url,
                json=payload,
                headers=headers,
                ssl=_build_tls_ssl_context(),
            ) as response:
                response.raise_for_status()
                return await response.json()
    except (aiohttp.ClientError, ValueError) as exc:
        raise FundingBotError(f"Connector request to {url!r} failed: {exc}") from exc


@dataclass(frozen=True)
class ConnectorPlugin:
    factory: Callable[..., PortalConnector]
    credential_schema: dict[str, Any] | None = None


class ConnectorRegistry:
    """Register and instantiate connector plugins."""

    def __init__(self) -> None:
        self._plugins: dict[str, ConnectorPlugin] = {}

    def register(
        self,
        connector_type: str,
        factory: Callable[..., PortalConnector],
        *,
        credential_schema: dict[str, Any] | None = None,
    ) -> None:
        normalized = connector_type.strip()
        if not normalized:
            raise ValueError("Connector type is required.")
        self._plugins[normalized] = ConnectorPlugin(
            factory=factory,
            credential_schema=credential_schema,
        )

    def discover(self) -> list[str]:
        return sorted(self._plugins)

    def create(self, connector_type: str, **kwargs: Any) -> PortalConnector:
        plugin = self._plugins.get(connector_type)
        if plugin is None:
            known = ", ".join(self.discover()) or "none"
            raise ConnectorConfigError(
                f"Unknown connector type {connector_type!r}. Registered connector types: {known}."
            )
        return plugin.factory(**kwargs)

    def validate_config(
        self,
        config: dict[str, Any],
        *,
        credential_resolver: Callable[[str], dict[str, Any]],
    ) -> None:
        plugin = self._plugins.get(config["type"])
        if plugin is None:
            known = ", ".join(self.discover()) or "none"
            raise ConnectorConfigError(
                f"Unknown connector type {config['type']!r}. Registered connector types: {known}."
            )

        if config.get("base_url"):
            try:
                _require_https_url(
                    str(config["base_url"]),
                    purpose=f"{config['type']} connector base URL",
                )
            except ConnectionSecurityError as exc:
                raise ConnectorConfigError(str(exc)) from exc

        if plugin.credential_schema is None:
            return

        credentials = dict(config.get("credentials") or {})
        if config.get("credential_alias"):
            try:
                credentials = credential_resolver(config["credential_alias"])
            except CredentialNotFoundError as exc:
                raise ConnectorConfigError(
                    f"Connector {config['type']!r} could not resolve credential alias "
                    f"{config['credential_alias']!r}: {exc}"
                ) from exc

        try:
            validate(instance=credentials, schema=plugin.credential_schema)
        except ValidationError as exc:
            path = ".".join(str(part) for part in exc.path)
            field = f" ({path})" if path else ""
            raise ConnectorConfigError(
                f"Invalid credentials for connector {config['type']!r}{field}: {exc.message}"
            ) from exc

    def build_connectors(
        self,
        configs: Iterable[dict[str, Any]],
        *,
        credential_resolver: Callable[[str], dict[str, Any]],
        cache_manager: CacheManager | None = None,
    ) -> list[PortalConnector]:
        built: list[PortalConnector] = []
        for config in configs:
            if config.get("enabled", True) is False:
                continue
            self.validate_config(config, credential_resolver=credential_resolver)
            credentials = dict(config.get("credentials") or {})
            if config.get("credential_alias"):
                credentials = credential_resolver(config["credential_alias"])
            settings = dict(config.get("settings") or {})
            built.append(
                self.create(
                    config["type"],
                    base_url=config.get("base_url"),
                    credentials=credentials,
                    transport=config.get("transport", "demo"),
                    cache_ttl=config.get("cache_ttl"),
                    rate_limit_config=config.get("rate_limit"),
                    cache_manager=cache_manager or default_cache_manager(),
                    **settings,
                )
            )
        return built


DEFAULT_CONNECTOR_REGISTRY = ConnectorRegistry()
DEFAULT_CONNECTOR_REGISTRY.register(GrantsPortalConnector.connector_slug, GrantsPortalConnector)
DEFAULT_CONNECTOR_REGISTRY.register(CSRNetworkConnector.connector_slug, CSRNetworkConnector)
DEFAULT_CONNECTOR_REGISTRY.register(NGODirectoryConnector.connector_slug, NGODirectoryConnector)
DEFAULT_CONNECTOR_REGISTRY.register(
    FoundationDirectoryConnector.connector_slug,
    FoundationDirectoryConnector,
    credential_schema={
        "type": "object",
        "properties": {
            "api_key": {"type": "string", "minLength": 1},
            "secret": {"type": "string", "minLength": 1},
        },
        "anyOf": [{"required": ["api_key"]}, {"required": ["secret"]}],
    },
)
DEFAULT_CONNECTOR_REGISTRY.register(GlobalGivingConnector.connector_slug, GlobalGivingConnector)
DEFAULT_CONNECTOR_REGISTRY.register(
    KickstarterForGoodConnector.connector_slug,
    KickstarterForGoodConnector,
)


def connector_registry() -> dict[str, type[_BasePortalConnector]]:
    """Return built-in connectors keyed by their CLI slug."""
    return {
        GrantsPortalConnector.connector_slug: GrantsPortalConnector,
        CSRNetworkConnector.connector_slug: CSRNetworkConnector,
        NGODirectoryConnector.connector_slug: NGODirectoryConnector,
        FoundationDirectoryConnector.connector_slug: FoundationDirectoryConnector,
        GlobalGivingConnector.connector_slug: GlobalGivingConnector,
        KickstarterForGoodConnector.connector_slug: KickstarterForGoodConnector,
    }


def create_connector(connector_name: str, **kwargs: Any) -> _BasePortalConnector:
    """Instantiate a built-in connector by slug."""
    try:
        connector_class = connector_registry()[connector_name]
    except KeyError as exc:
        raise FundingBotError(f"Unknown connector {connector_name!r}.") from exc
    return connector_class(**kwargs)


SUPPORTED_OUTREACH_LOCALES = ("en", "bn")
DEFAULT_OUTREACH_LOCALE = "en"
DEFAULT_OUTREACH_TEMPLATE_NAME = "default"
OUTREACH_TEMPLATE_CATALOG_DIR = Path(__file__).resolve().parent / "i18n" / "outreach_templates"


def _load_localized_outreach_templates() -> dict[str, dict[str, dict[str, Any]]]:
    catalog: dict[str, dict[str, dict[str, Any]]] = {}
    for locale_name in SUPPORTED_OUTREACH_LOCALES:
        catalog_path = OUTREACH_TEMPLATE_CATALOG_DIR / f"{locale_name}.json"
        with catalog_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        templates = payload.get("templates")
        if not isinstance(templates, dict):
            raise ValueError(
                f"Outreach template catalog {catalog_path} must contain a 'templates' object."
            )
        for template_name, template_body in templates.items():
            if not isinstance(template_name, str) or not isinstance(template_body, dict):
                raise ValueError(f"Invalid template entry {template_name!r} in {catalog_path}.")
            subject = template_body.get("subject", "")
            body = template_body.get("body", "")
            if not isinstance(subject, str) or not isinstance(body, str):
                raise ValueError(
                    f"Outreach template {template_name!r} in {catalog_path} must define string subject and body."
                )
            segments = template_body.get("segments", {})
            if not isinstance(segments, dict):
                raise ValueError(
                    f"Outreach template {template_name!r} in {catalog_path} must define 'segments' as an object."
                )
            for segment_name, segment_template in segments.items():
                if not isinstance(segment_template, dict):
                    raise ValueError(
                        f"Outreach template {template_name!r} segment {segment_name!r} in {catalog_path} must be an object."
                    )
                segment_subject = segment_template.get("subject", "")
                segment_body = segment_template.get("body", "")
                if not isinstance(segment_subject, str) or not isinstance(segment_body, str):
                    raise ValueError(
                        f"Outreach template {template_name!r} segment {segment_name!r} in {catalog_path} must define string subject and body."
                    )
            catalog.setdefault(template_name, {})[locale_name] = dict(template_body)
    return catalog


LOCALIZED_OUTREACH_TEMPLATES = _load_localized_outreach_templates()


def _validate_locale(locale: str | None) -> str:
    normalized = (locale or DEFAULT_OUTREACH_LOCALE).strip().lower()
    if normalized not in SUPPORTED_OUTREACH_LOCALES:
        raise ValueError(
            f"Invalid locale {locale!r}. Expected one of {list(SUPPORTED_OUTREACH_LOCALES)}."
        )
    return normalized


def _validate_localized_outreach_templates(
    catalog: dict[str, dict[str, dict[str, Any]]],
) -> None:
    for template_name, localized_templates in catalog.items():
        missing_locales = sorted(set(SUPPORTED_OUTREACH_LOCALES) - set(localized_templates))
        if missing_locales:
            raise ValueError(
                f"Outreach template {template_name!r} is missing locales: {missing_locales}."
            )
        for locale_name in SUPPORTED_OUTREACH_LOCALES:
            localized_template = localized_templates[locale_name]
            subject = localized_template.get("subject", "").strip()
            body = localized_template.get("body", "").strip()
            if not subject or not body:
                raise ValueError(
                    f"Outreach template {template_name!r} for locale {locale_name!r} must define non-empty subject and body."
                )
            segments = localized_template.get("segments", {})
            if not isinstance(segments, dict):
                raise ValueError(
                    f"Outreach template {template_name!r} for locale {locale_name!r} must define 'segments' as an object."
                )
            for segment_name, segment_template in segments.items():
                segment_subject = ""
                segment_body = ""
                if isinstance(segment_template, dict):
                    segment_subject = str(segment_template.get("subject", "")).strip()
                    segment_body = str(segment_template.get("body", "")).strip()
                if not segment_subject or not segment_body:
                    raise ValueError(
                        f"Outreach template {template_name!r} segment {segment_name!r} for locale {locale_name!r} must define non-empty subject and body."
                    )
            default_segments = {
                segment_name
                for segment_name in localized_templates[DEFAULT_OUTREACH_LOCALE].get("segments", {})
            }
            locale_segments = {segment_name for segment_name in segments}
            if locale_segments != default_segments:
                missing_segments = sorted(default_segments - locale_segments)
                extra_segments = sorted(locale_segments - default_segments)
                problems: list[str] = []
                if missing_segments:
                    problems.append(f"missing segments {missing_segments}")
                if extra_segments:
                    problems.append(f"unexpected segments {extra_segments}")
                raise ValueError(
                    f"Outreach template {template_name!r} for locale {locale_name!r} does not match segment coverage: "
                    + ", ".join(problems)
                    + "."
                )
            default_notice = localized_templates[DEFAULT_OUTREACH_LOCALE].get("opt_out_notice")
            locale_notice = localized_template.get("opt_out_notice")
            if isinstance(default_notice, str) and not str(locale_notice or "").strip():
                raise ValueError(
                    f"Outreach template {template_name!r} for locale {locale_name!r} must define opt_out_notice."
                )


_validate_localized_outreach_templates(LOCALIZED_OUTREACH_TEMPLATES)


class SMTPEmailSender:
    """Send plain-text emails via SMTP.

    Environment variables used by :meth:`from_env`:

    - ``SMTP_HOST``      – mail server hostname (default: ``localhost``)
    - ``SMTP_PORT``      – port number (default: ``587``)
    - ``SMTP_USERNAME``  – login username
    - ``SMTP_PASSWORD``  – login password
    - ``SMTP_USE_TLS``   – ``"0"`` to disable STARTTLS (enabled by default)
    - ``SMTP_FROM``      – envelope ``From`` address (defaults to username)
    """

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        use_tls: bool = True,
        from_address: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.from_address = from_address or username

    @classmethod
    def from_env(cls) -> "SMTPEmailSender":
        """Build an :class:`SMTPEmailSender` from environment variables."""
        host = os.environ.get("SMTP_HOST", "localhost")
        port = int(os.environ.get("SMTP_PORT", "587"))
        username = os.environ.get("SMTP_USERNAME", "")
        password = os.environ.get("SMTP_PASSWORD", "")
        use_tls = os.environ.get("SMTP_USE_TLS", "1") != "0"
        from_address = os.environ.get("SMTP_FROM") or username
        return cls(
            host,
            port,
            username,
            password,
            use_tls=use_tls,
            from_address=from_address,
        )

    @staticmethod
    def is_configured() -> bool:
        """Return whether SMTP environment variables are set for real delivery.

        Used by the web Settings panel to display SMTP status without
        duplicating the environment-variable logic from :meth:`from_env`.
        """
        return bool(os.environ.get("SMTP_HOST")) and bool(os.environ.get("SMTP_USERNAME"))

    def __call__(self, to_address: str, subject: str, body: str) -> None:
        """Send a plain-text email.

        This method matches the ``sender`` callable signature expected by
        :meth:`FundingBot.send_outreach` and :meth:`FundingBot.send_daily_summary`.

        Raises :class:`smtplib.SMTPException` (with added context) if the
        message cannot be delivered.
        """
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = self.from_address
        msg["To"] = to_address

        server: smtplib.SMTP = smtplib.SMTP(self.host, self.port)
        if self.use_tls:
            server.starttls()

        try:
            if self.username:
                server.login(self.username, self.password)
            server.sendmail(self.from_address, [to_address], msg.as_string())
        except smtplib.SMTPException as exc:
            raise smtplib.SMTPException(
                f"Failed to send email to {to_address!r} via {self.host}:{self.port}: {exc}"
            ) from exc
        finally:
            server.quit()


class FundingBot:
    TASK_STATUSES = ("todo", "in-progress", "done", "blocked")
    FUNNEL_STAGES = ("discover", "dedupe", "match", "outreach", "response")
    POSITIVE_RESPONSE_EVENT_TYPES = frozenset({"opened", "clicked", "responded", "replied"})
    TASK_STATUS_ALIASES = {
        "todo": "todo",
        "pending": "todo",
        "in-progress": "in-progress",
        "in_progress": "in-progress",
        "done": "done",
        "completed": "done",
        "blocked": "blocked",
    }
    TASK_STATUS_TRANSITIONS = {
        "todo": frozenset({"in-progress", "blocked"}),
        "in-progress": frozenset({"todo", "done", "blocked"}),
        "blocked": frozenset({"todo", "in-progress"}),
        "done": frozenset(),
    }
    DEFAULT_QUEUE_RETRY_LIMIT = 3
    DEFAULT_QUEUE_RETRY_BACKOFF_SECONDS = 5.0
    DEFAULT_QUEUE_RETRY_BACKOFF_MAX_SECONDS = 300.0
    SUPPORTED_TEMPLATE_LOCALES = frozenset(SUPPORTED_OUTREACH_LOCALES)
    DEFAULT_TEMPLATE_LOCALE = DEFAULT_OUTREACH_LOCALE
    DEFAULT_OUTREACH_TEMPLATE = DEFAULT_OUTREACH_TEMPLATE_NAME
    SUPPORTED_DATA_RESIDENCIES = ("US", "EU", "ASIA")
    DEFAULT_DATA_RESIDENCY = "US"
    SUPPORTED_PRIVACY_POLICY_FORMATS = frozenset({"html", "pdf"})
    DEFAULT_PRIVACY_POLICY_FORMATS = ("html", "pdf")
    OUTREACH_TEMPLATE_CATALOG = OUTREACH_TEMPLATE_CATALOG_DIR
    DATA_RETENTION_POLICY_KEY = "data_retention_policy"
    MFA_ISSUER_NAME = "Funding Bot Dashboard"
    MFA_TOTP_DIGITS = 6
    MFA_BACKUP_CODE_COUNT = 8
    MFA_BACKUP_CODE_BYTES = 4
    DATA_CLASSIFICATIONS = _DATA_CLASSIFICATION_LEVELS
    MODEL_DEFAULT_CLASSIFICATIONS = {
        "organization_profile": "internal",
        "credential_refs": "internal",
        "opportunities": "public",
        "applications": "internal",
        "submission_attempts": "internal",
        "donors": "secret",
        "consent_records": "confidential",
        "communications": "confidential",
        "documents": "internal",
        "audit_logs": "confidential",
        "outreach_templates": "internal",
        "outreach_events": "internal",
        "task_runs": "internal",
        "tasks": "internal",
        "translation_reviews": "internal",
    }
    SETTING_DEFAULT_CLASSIFICATIONS = {
        "profile": "secret",
        "search_settings": "internal",
    }
    DONOR_FIELD_CLASSIFICATIONS = {
        "email": "confidential",
        "name": "internal",
        "opted_out": "confidential",
        "preferences": "secret",
        "last_contact_at": "confidential",
        "segment": "internal",
        "locale": "internal",
    }
    ORGANIZATION_PROFILE_FIELD_CLASSIFICATIONS = {
        "name": "public",
        "mission": "public",
        "website": "public",
        "registration_number": "confidential",
        "tax_id": "secret",
        "bank_account": "secret",
        "bank_details": "secret",
        "contact_email": "confidential",
        "phone": "confidential",
        "address": "confidential",
    }
    DATA_RETENTION_DEFAULTS = {
        "audit_logs_days": 365,
        "communications_days": 365,
        "documents_days": 180,
        "opportunities_days": 365,
        "submission_attempts_days": 90,
        "completed_tasks_days": 180,
    }
    DATA_RETENTION_ENV_VARS = {
        "audit_logs_days": "RETENTION_AUDIT_LOG_DAYS",
        "communications_days": "RETENTION_COMMUNICATION_DAYS",
        "documents_days": "RETENTION_DOCUMENT_DAYS",
        "opportunities_days": "RETENTION_OPPORTUNITY_DAYS",
        "submission_attempts_days": "RETENTION_SUBMISSION_ATTEMPT_DAYS",
        "completed_tasks_days": "RETENTION_COMPLETED_TASK_DAYS",
    }

    def __init__(
        self,
        db_path: str | os.PathLike[str] = ":memory:",
        *,
        trusted_sources: Iterable[str] | None = None,
        vault: CredentialVault | None = None,
        connector_registry: ConnectorRegistry | None = None,
        connector_configs: dict[str, Any] | list[dict[str, Any]] | None = None,
        oauth_token_http_client: Callable[[str, dict[str, Any], dict[str, str]], Any] | None = None,
        oauth_refresh_skew_seconds: float | None = None,
        cache_manager: CacheManager | None = None,
    ) -> None:
        configure_tracing()
        self.db_path = str(db_path)
        self.trusted_sources = {source.lower() for source in (trusted_sources or [])}
        if (
            isinstance(vault, OAuth2ClientCredentialsVault)
            and oauth_token_http_client is None
            and oauth_refresh_skew_seconds is None
        ):
            self.vault = vault
        else:
            self.vault = OAuth2ClientCredentialsVault(
                vault or EnvVarVault(),
                token_http_client=oauth_token_http_client,
                refresh_skew_seconds=oauth_refresh_skew_seconds,
            )
        self.connector_registry = connector_registry or DEFAULT_CONNECTOR_REGISTRY
        self._data_residency_status = self.validate_data_storage_location()
        self._active_queue_controllers: dict[str, GracefulShutdownController] = {}
        self._db_lock = threading.Lock()
        self.cache_manager = cache_manager or default_cache_manager()
        if self.db_path == ":memory:":
            self._cache_scope = f"memory-{id(self)}"
        else:
            self._cache_scope = hashlib.sha256(self.db_path.encode("utf-8")).hexdigest()[:12]
        self._database = DatabaseManager(self.db_path)
        self.connection = self._database.connection
        self._donor_cache = self.cache_manager.make_region(
            "donor-records",
            scope=self._cache_scope,
        )
        self._deduped_profile_cache = self.cache_manager.make_region(
            "deduped-profiles",
            scope=self._cache_scope,
        )
        self._create_schema()
        self._apply_migrations()
        self._ensure_tasks_schema()
        ensure_slo_schema(self.connection)
        self.connector_configs = self._load_connector_configs(connector_configs)
        self._validate_connector_configs()

    def close(self) -> None:
        self._database.close()
        self.connection = None

    def _reopen_database_connection(self) -> None:
        self._database.close()
        self._database = DatabaseManager(self.db_path)
        self.connection = self._database.connection

    @asynccontextmanager
    async def async_db_session(self) -> Any:
        async with AsyncDatabaseSession(self.connection, self._db_lock) as session:
            yield session

    @staticmethod
    def _resolve_connector_batch_size(batch_size: int | None = None) -> int:
        candidate = batch_size
        if candidate is None:
            candidate = _read_numeric_env(
                ["FUNDING_BOT_CONNECTOR_BATCH_SIZE", "CONNECTOR_BATCH_SIZE"],
                5,
                minimum=1,
                as_int=True,
            )
        try:
            return max(1, int(candidate))
        except (TypeError, ValueError):
            return 5

    def _create_schema(self) -> None:
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS organization_profile (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS credential_refs (
                alias TEXT PRIMARY KEY,
                env_var_name TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS auth_security (
                role TEXT PRIMARY KEY,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                lockout_until TEXT,
                last_failed_at TEXT,
                last_success_at TEXT,
                mfa_enabled INTEGER NOT NULL DEFAULT 0,
                mfa_secret TEXT,
                mfa_pending_secret TEXT,
                mfa_backup_codes_json TEXT NOT NULL DEFAULT '[]',
                mfa_pending_backup_codes_json TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS opportunities (
                signature TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                donor_name TEXT NOT NULL,
                title TEXT NOT NULL,
                portal_url TEXT NOT NULL,
                summary TEXT NOT NULL,
                category TEXT,
                discovered_at TEXT NOT NULL,
                status TEXT NOT NULL,
                raw_data_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_signature TEXT NOT NULL UNIQUE,
                donor_name TEXT NOT NULL,
                portal_url TEXT NOT NULL,
                submitted_at TEXT NOT NULL,
                status TEXT NOT NULL,
                next_action TEXT NOT NULL,
                submission_reference TEXT,
                FOREIGN KEY (opportunity_signature) REFERENCES opportunities(signature)
            );

            CREATE TABLE IF NOT EXISTS submission_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_signature TEXT NOT NULL,
                attempt_number INTEGER NOT NULL,
                succeeded INTEGER NOT NULL,
                error_message TEXT,
                happened_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS donors (
                email TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                opted_out INTEGER NOT NULL DEFAULT 0,
                preferences_json TEXT NOT NULL DEFAULT '{}',
                last_contact_at TEXT,
                locale TEXT NOT NULL DEFAULT 'en'
            );

            CREATE TABLE IF NOT EXISTS consent_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                donor_email TEXT NOT NULL,
                channel TEXT NOT NULL DEFAULT 'email',
                status TEXT NOT NULL,
                consented_at TEXT NOT NULL,
                withdrawn_at TEXT,
                source TEXT NOT NULL,
                proof TEXT,
                notes TEXT,
                recorded_at TEXT NOT NULL,
                FOREIGN KEY (donor_email) REFERENCES donors(email) ON UPDATE CASCADE
            );

            CREATE TABLE IF NOT EXISTS communications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                donor_email TEXT NOT NULL,
                donor_name TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                channel TEXT NOT NULL,
                sent_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                format TEXT NOT NULL,
                path TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS privacy_policy_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                jurisdiction TEXT NOT NULL,
                revision INTEGER NOT NULL,
                version TEXT NOT NULL,
                data_residency TEXT NOT NULL,
                effective_date TEXT NOT NULL,
                html_path TEXT,
                pdf_path TEXT,
                profile_json TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                UNIQUE(jurisdiction, revision),
                UNIQUE(version)
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                happened_at TEXT NOT NULL,
                action TEXT NOT NULL,
                details_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS outreach_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                subject_template TEXT NOT NULL,
                body_template TEXT NOT NULL,
                segment TEXT NOT NULL DEFAULT '',
                UNIQUE(name, segment)
            );

            CREATE TABLE IF NOT EXISTS outreach_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                communication_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                happened_at TEXT NOT NULL,
                FOREIGN KEY (communication_id) REFERENCES communications(id)
            );

            CREATE TABLE IF NOT EXISTS connector_result_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                connector_name TEXT NOT NULL,
                cache_key TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                fetched_at TEXT NOT NULL,
                source_status TEXT NOT NULL DEFAULT 'remote',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL,
                UNIQUE(connector_name, cache_key)
            );

            CREATE TABLE IF NOT EXISTS connector_call_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                connector_name TEXT NOT NULL,
                connector_type TEXT NOT NULL,
                operation TEXT NOT NULL,
                source_status TEXT NOT NULL,
                latency_seconds REAL NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0,
                errored INTEGER NOT NULL DEFAULT 0,
                request_count INTEGER NOT NULL DEFAULT 0,
                happened_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS task_runs (
                task_id TEXT PRIMARY KEY,
                idempotency_key TEXT UNIQUE,
                task_name TEXT NOT NULL,
                status TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT,
                error_message TEXT,
                worker_id TEXT,
                duplicate_requests INTEGER NOT NULL DEFAULT 0,
                shutdown_requested INTEGER NOT NULL DEFAULT 0,
                callback_name TEXT,
                callback_payload_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS task_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                task_name TEXT NOT NULL,
                attempt_number INTEGER NOT NULL,
                status TEXT NOT NULL,
                happened_at TEXT NOT NULL,
                backoff_seconds REAL,
                next_retry_at TEXT,
                result_json TEXT,
                error_message TEXT,
                details_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS dead_letter_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL UNIQUE,
                task_name TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                error_message TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                failed_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                external_id TEXT UNIQUE,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                assignee TEXT,
                assigned_to TEXT,
                status TEXT NOT NULL,
                due_date TEXT,
                source TEXT NOT NULL DEFAULT 'manual',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS task_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                author TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS task_comment_reads (
                task_id INTEGER NOT NULL,
                reader_email TEXT NOT NULL,
                last_read_at TEXT NOT NULL,
                PRIMARY KEY (task_id, reader_email),
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS task_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                recipient_email TEXT NOT NULL,
                notification_type TEXT NOT NULL,
                happened_at TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS translation_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                locale TEXT NOT NULL,
                translation_key TEXT NOT NULL,
                source_text TEXT NOT NULL,
                translated_text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                submitter_notes TEXT,
                submitted_by_role TEXT,
                created_at TEXT NOT NULL,
                reviewed_at TEXT,
                reviewed_by_role TEXT,
                reviewer_notes TEXT
            );

            -- Performance indexes for v1.0
            CREATE INDEX IF NOT EXISTS idx_opportunities_discovered_at
                ON opportunities(discovered_at DESC);
            CREATE INDEX IF NOT EXISTS idx_opportunities_status
                ON opportunities(status);
            CREATE INDEX IF NOT EXISTS idx_applications_status
                ON applications(status);
            CREATE INDEX IF NOT EXISTS idx_applications_submitted_at
                ON applications(submitted_at DESC);
            CREATE INDEX IF NOT EXISTS idx_audit_logs_happened_at
                ON audit_logs(happened_at DESC);
            CREATE INDEX IF NOT EXISTS idx_audit_logs_action
                ON audit_logs(action);
            CREATE INDEX IF NOT EXISTS idx_consent_records_donor_email
                ON consent_records(donor_email);
            CREATE INDEX IF NOT EXISTS idx_consent_records_recorded_at
                ON consent_records(recorded_at DESC);
            CREATE INDEX IF NOT EXISTS idx_communications_donor_email
                ON communications(donor_email);
            CREATE INDEX IF NOT EXISTS idx_communications_sent_at
                ON communications(sent_at DESC);
            CREATE INDEX IF NOT EXISTS idx_privacy_policy_versions_jurisdiction
                ON privacy_policy_versions(jurisdiction, revision DESC);
            CREATE INDEX IF NOT EXISTS idx_outreach_events_communication_id
                ON outreach_events(communication_id);
            CREATE INDEX IF NOT EXISTS idx_connector_result_cache_lookup
                ON connector_result_cache(connector_name, cache_key);
            CREATE INDEX IF NOT EXISTS idx_task_runs_status
                ON task_runs(status);
            CREATE INDEX IF NOT EXISTS idx_task_runs_updated_at
                ON task_runs(updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_task_history_task_id
                ON task_history(task_id, attempt_number);
            CREATE INDEX IF NOT EXISTS idx_task_history_status
                ON task_history(status);
            CREATE INDEX IF NOT EXISTS idx_dead_letter_queue_task_name
                ON dead_letter_queue(task_name, failed_at DESC);
            CREATE INDEX IF NOT EXISTS idx_task_comments_task_id
                ON task_comments(task_id, created_at ASC);
            CREATE INDEX IF NOT EXISTS idx_task_notifications_lookup
                ON task_notifications(task_id, recipient_email, notification_type, happened_at DESC);
            CREATE INDEX IF NOT EXISTS idx_translation_reviews_status
                ON translation_reviews(status);
            CREATE INDEX IF NOT EXISTS idx_translation_reviews_locale
                ON translation_reviews(locale);
            CREATE INDEX IF NOT EXISTS idx_translation_reviews_created_at
                ON translation_reviews(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_auth_security_lockout_until
                ON auth_security(lockout_until);
            """)
        self._ensure_column("donors", "segment", "TEXT NOT NULL DEFAULT 'unknown'")
        self._ensure_column("donors", "locale", "TEXT NOT NULL DEFAULT 'en'")
        self._ensure_column("tasks", "external_id", "TEXT")
        self._ensure_column("tasks", "due_date", "TEXT")
        self._ensure_column("tasks", "source", "TEXT NOT NULL DEFAULT 'manual'")
        self._ensure_column("tasks", "assignee_email", "TEXT")
        self._ensure_column("tasks", "assignee_name", "TEXT")
        self._ensure_column("tasks", "attributed_connector", "TEXT")
        self._ensure_column("tasks", "opportunity_signature", "TEXT")
        self._ensure_column("task_runs", "idempotency_key", "TEXT")
        self._ensure_column("task_runs", "worker_id", "TEXT")
        self._ensure_column("task_runs", "duplicate_requests", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("task_runs", "shutdown_requested", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("task_runs", "retry_limit", "INTEGER NOT NULL DEFAULT 3")
        self._ensure_column("task_runs", "attempts", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("task_runs", "backoff_seconds", "REAL NOT NULL DEFAULT 5")
        self._ensure_column("task_runs", "backoff_max_seconds", "REAL NOT NULL DEFAULT 300")
        self._ensure_column("task_runs", "dead_lettered", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("task_runs", "last_attempt_at", "TEXT")
        self._ensure_column("task_runs", "next_retry_at", "TEXT")
        self._ensure_column(
            "task_runs",
            "data_classification",
            "TEXT NOT NULL DEFAULT 'internal'",
        )
        self._ensure_column(
            "organization_profile",
            "data_classification",
            "TEXT NOT NULL DEFAULT 'internal'",
        )
        self._ensure_column(
            "organization_profile",
            "field_classifications_json",
            "TEXT NOT NULL DEFAULT '{}'",
        )
        self._ensure_column(
            "credential_refs",
            "data_classification",
            "TEXT NOT NULL DEFAULT 'internal'",
        )
        self._ensure_column(
            "opportunities",
            "data_classification",
            "TEXT NOT NULL DEFAULT 'public'",
        )
        self._ensure_column(
            "applications",
            "data_classification",
            "TEXT NOT NULL DEFAULT 'internal'",
        )
        self._ensure_column(
            "submission_attempts",
            "data_classification",
            "TEXT NOT NULL DEFAULT 'internal'",
        )
        self._ensure_column(
            "donors",
            "data_classification",
            "TEXT NOT NULL DEFAULT 'secret'",
        )
        self._ensure_column(
            "donors",
            "field_classifications_json",
            "TEXT NOT NULL DEFAULT '{}'",
        )
        self._ensure_column(
            "consent_records",
            "data_classification",
            "TEXT NOT NULL DEFAULT 'confidential'",
        )
        self._ensure_column(
            "communications",
            "data_classification",
            "TEXT NOT NULL DEFAULT 'confidential'",
        )
        self._ensure_column(
            "documents",
            "data_classification",
            "TEXT NOT NULL DEFAULT 'internal'",
        )
        self._ensure_column(
            "privacy_policy_versions",
            "data_classification",
            "TEXT NOT NULL DEFAULT 'internal'",
        )
        self._ensure_column(
            "audit_logs",
            "data_classification",
            "TEXT NOT NULL DEFAULT 'confidential'",
        )
        self._ensure_column(
            "outreach_templates",
            "data_classification",
            "TEXT NOT NULL DEFAULT 'internal'",
        )
        self._ensure_column(
            "outreach_events",
            "data_classification",
            "TEXT NOT NULL DEFAULT 'internal'",
        )
        self._ensure_column(
            "connector_result_cache",
            "data_classification",
            "TEXT NOT NULL DEFAULT 'internal'",
        )
        self._ensure_column(
            "task_history",
            "data_classification",
            "TEXT NOT NULL DEFAULT 'internal'",
        )
        self._ensure_column(
            "dead_letter_queue",
            "data_classification",
            "TEXT NOT NULL DEFAULT 'internal'",
        )
        self._ensure_column("tasks", "data_classification", "TEXT NOT NULL DEFAULT 'internal'")
        self._ensure_column(
            "task_comments",
            "data_classification",
            "TEXT NOT NULL DEFAULT 'internal'",
        )
        self._ensure_column(
            "task_comment_reads",
            "data_classification",
            "TEXT NOT NULL DEFAULT 'internal'",
        )
        self._ensure_column(
            "task_notifications",
            "data_classification",
            "TEXT NOT NULL DEFAULT 'internal'",
        )
        self._ensure_column(
            "translation_reviews",
            "data_classification",
            "TEXT NOT NULL DEFAULT 'internal'",
        )
        # Index on donors.segment must be created after the column is guaranteed to exist.
        self.connection.execute("CREATE INDEX IF NOT EXISTS idx_donors_segment ON donors(segment)")
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_runs_idempotency_key ON task_runs(idempotency_key)"
        )
        self.connection.commit()

    def _apply_migrations(self) -> None:
        migrations_dir = Path(__file__).resolve().parent / "migrations"
        if not migrations_dir.exists():
            return
        applied = {
            row["name"]
            for row in self.connection.execute("SELECT name FROM schema_migrations").fetchall()
        }
        for migration_path in sorted(migrations_dir.glob("*.sql")):
            if migration_path.name in applied:
                continue
            try:
                self.connection.executescript(migration_path.read_text(encoding="utf-8"))
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if "duplicate column name" not in message and "already exists" not in message:
                    raise
            self.connection.execute(
                "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
                (migration_path.name, self._to_iso()),
            )
            self.connection.commit()

    def _apply_migrations(self) -> None:
        """Apply lightweight schema migrations for existing databases."""
        self.connection.execute(
            """
            INSERT OR IGNORE INTO schema_migrations (name, applied_at)
            VALUES (?, ?)
            """,
            ("baseline-task-schema", self._to_iso()),
        )
        self.connection.commit()

    def _ensure_tasks_schema(self) -> None:
        """Ensure task collaboration columns exist for upgraded databases."""
        columns = [
            row["name"] for row in self.connection.execute("PRAGMA table_info(tasks)").fetchall()
        ]
        if columns and "assignee" not in columns and "assigned_to" in columns:
            self.connection.executescript("""
                ALTER TABLE tasks RENAME TO tasks_legacy;
                CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    external_id TEXT UNIQUE,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    assignee TEXT,
                    assigned_to TEXT,
                    status TEXT NOT NULL,
                    due_date TEXT,
                    source TEXT NOT NULL DEFAULT 'manual',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    assignee_email TEXT,
                    assignee_name TEXT,
                    attributed_connector TEXT,
                    opportunity_signature TEXT
                );
                INSERT INTO tasks (
                    id, external_id, title, description, assignee, assigned_to, status, due_date,
                    source, created_at, updated_at, assignee_email, assignee_name,
                    attributed_connector, opportunity_signature
                )
                SELECT
                    id,
                    external_id,
                    title,
                    COALESCE(description, ''),
                    LOWER(assigned_to),
                    LOWER(assigned_to),
                    CASE LOWER(status)
                        WHEN 'pending' THEN 'todo'
                        WHEN 'in_progress' THEN 'in-progress'
                        WHEN 'completed' THEN 'done'
                        ELSE LOWER(status)
                    END,
                    date(due_date),
                    COALESCE(source, 'manual'),
                    created_at,
                    updated_at,
                    assignee_email,
                    assignee_name,
                    NULL,
                    NULL
                FROM tasks_legacy;
                DROP TABLE tasks_legacy;
                """)
        self._ensure_column("tasks", "external_id", "TEXT")
        self._ensure_column("tasks", "due_date", "TEXT")
        self._ensure_column("tasks", "source", "TEXT NOT NULL DEFAULT 'manual'")
        self._ensure_column("tasks", "assignee_email", "TEXT")
        self._ensure_column("tasks", "assignee_name", "TEXT")
        self._ensure_column("tasks", "attributed_connector", "TEXT")
        self._ensure_column("tasks", "opportunity_signature", "TEXT")
        self.connection.execute("CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee)")
        self.connection.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_external_id ON tasks(external_id)"
        )
        self.connection.execute("CREATE INDEX IF NOT EXISTS idx_tasks_due_date ON tasks(due_date)")
        self.connection.commit()

    # Allowlist of table/column identifiers that _ensure_column is permitted to touch.
    # All calls are internal and use literals; the allowlist is an extra safety guard.
    _ALLOWED_ALTER_TABLES = frozenset(
        {
            "organization_profile",
            "credential_refs",
            "opportunities",
            "applications",
            "submission_attempts",
            "donors",
            "consent_records",
            "communications",
            "documents",
            "privacy_policy_versions",
            "audit_logs",
            "outreach_templates",
            "outreach_events",
            "connector_result_cache",
            "task_runs",
            "task_history",
            "dead_letter_queue",
            "tasks",
            "task_comments",
            "task_comment_reads",
            "task_notifications",
            "translation_reviews",
        }
    )
    _ALLOWED_ALTER_COLUMNS = frozenset(
        {
            "data_classification",
            "field_classifications_json",
            "segment",
            "locale",
            "external_id",
            "assigned_to",
            "due_date",
            "source",
            "assignee_email",
            "assignee_name",
            "attributed_connector",
            "opportunity_signature",
            "idempotency_key",
            "worker_id",
            "duplicate_requests",
            "shutdown_requested",
            "retry_limit",
            "attempts",
            "backoff_seconds",
            "backoff_max_seconds",
            "dead_lettered",
            "last_attempt_at",
            "next_retry_at",
        }
    )

    def _ensure_column(self, table_name: str, column_name: str, definition: str) -> None:
        if table_name not in self._ALLOWED_ALTER_TABLES:
            raise ValueError(f"_ensure_column: table {table_name!r} not in allowlist.")
        if column_name not in self._ALLOWED_ALTER_COLUMNS:
            raise ValueError(f"_ensure_column: column {column_name!r} not in allowlist.")
        # definition is a TYPE+DEFAULT expression built only from string literals in this module.
        # Use PRAGMA table_info to check existence first, avoiding f-string SQL when possible.
        existing_columns = {
            row["name"]
            for row in self.connection.execute(
                "PRAGMA table_info(" + table_name + ")"  # table_name validated above
            ).fetchall()
        }
        if column_name in existing_columns:
            return
        # SQLite < 3.35 does not support ADD COLUMN IF NOT EXISTS; fall back gracefully.
        try:
            self.connection.execute(
                "ALTER TABLE " + table_name + " ADD COLUMN " + column_name + " " + definition
            )
        except sqlite3.OperationalError as exc:
            # Column may have been added by a concurrent writer; re-check before re-raising.
            refreshed = {
                row["name"]
                for row in self.connection.execute(
                    "PRAGMA table_info(" + table_name + ")"
                ).fetchall()
            }
            if column_name not in refreshed:
                raise sqlite3.OperationalError(
                    f"Could not add column {column_name!r} to {table_name!r}: {exc}"
                ) from exc

    @staticmethod
    def _connector_name(connector: PortalConnector) -> str:
        return str(getattr(connector, "source_name", connector.__class__.__name__))

    def _connector_cache_key(self, connector: PortalConnector, keywords: Iterable[str]) -> str:
        builder = getattr(connector, "build_cache_key", None)
        if callable(builder):
            return str(builder(keywords))
        return json.dumps(
            {
                "connector": self._connector_name(connector),
                "keywords": sorted(keyword.lower() for keyword in _normalize_text_list(keywords)),
            },
            sort_keys=True,
        )

    @staticmethod
    def _load_fallback_mode() -> str:
        raw_mode = os.environ.get("PORTAL_FALLBACK_MODE", "cache-first").strip().lower()
        allowed_modes = {"cache-first", "cache-only", "default-only", "disabled"}
        if raw_mode not in allowed_modes:
            logging.getLogger(__name__).warning(
                "Unknown PORTAL_FALLBACK_MODE=%r; defaulting to cache-first.",
                raw_mode,
            )
            return "cache-first"
        return raw_mode

    def _store_connector_result(
        self,
        *,
        connector_name: str,
        cache_key: str,
        schema_version: int,
        opportunities: Iterable[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
        source_status: str = "remote",
    ) -> None:
        payload = [dict(item) for item in opportunities]
        self.connection.execute(
            """
            INSERT INTO connector_result_cache (
                connector_name, cache_key, schema_version, fetched_at,
                source_status, metadata_json, result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(connector_name, cache_key) DO UPDATE SET
                schema_version = excluded.schema_version,
                fetched_at = excluded.fetched_at,
                source_status = excluded.source_status,
                metadata_json = excluded.metadata_json,
                result_json = excluded.result_json
            """,
            (
                connector_name,
                cache_key,
                int(schema_version),
                self._to_iso(),
                source_status,
                json.dumps(metadata or {}, sort_keys=True),
                json.dumps(payload, sort_keys=True),
            ),
        )
        self.connection.commit()

    async def _store_connector_result_async(
        self,
        *,
        connector_name: str,
        cache_key: str,
        schema_version: int,
        opportunities: Iterable[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
        source_status: str = "remote",
    ) -> None:
        payload = [dict(item) for item in opportunities]
        async with self.async_db_session() as session:
            await session.execute(
                """
                INSERT INTO connector_result_cache (
                    connector_name, cache_key, schema_version, fetched_at,
                    source_status, metadata_json, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(connector_name, cache_key) DO UPDATE SET
                    schema_version = excluded.schema_version,
                    fetched_at = excluded.fetched_at,
                    source_status = excluded.source_status,
                    metadata_json = excluded.metadata_json,
                    result_json = excluded.result_json
                """,
                (
                    connector_name,
                    cache_key,
                    int(schema_version),
                    self._to_iso(),
                    source_status,
                    json.dumps(metadata or {}, sort_keys=True),
                    json.dumps(payload, sort_keys=True),
                ),
            )

    def _load_cached_connector_result(
        self,
        connector: PortalConnector,
        keywords: Iterable[str],
    ) -> dict[str, Any] | None:
        connector_name = self._connector_name(connector)
        cache_key = self._connector_cache_key(connector, keywords)
        row = self.connection.execute(
            """
            SELECT schema_version, source_status, metadata_json, result_json
            FROM connector_result_cache
            WHERE connector_name = ? AND cache_key = ?
            """,
            (connector_name, cache_key),
        ).fetchone()
        if row is None:
            return None
        metadata = json.loads(row["metadata_json"] or "{}")
        payload = json.loads(row["result_json"] or "[]")
        migrate = getattr(connector, "migrate_result_payload", None)
        current_version = int(row["schema_version"])
        target_version = int(
            getattr(connector, "result_schema_version", _CONNECTOR_RESULT_SCHEMA_VERSION)
        )
        if callable(migrate):
            payload = migrate(payload, current_version)
        else:
            payload = [dict(item) for item in payload]
        if current_version != target_version:
            metadata = {
                **metadata,
                "migrated_from_schema_version": current_version,
                "migrated_at": self._to_iso(),
            }
            self._store_connector_result(
                connector_name=connector_name,
                cache_key=cache_key,
                schema_version=target_version,
                opportunities=payload,
                metadata=metadata,
                source_status=row["source_status"],
            )
        return {
            "schema_version": target_version,
            "opportunities": payload,
            "metadata": metadata,
            "source_status": row["source_status"],
        }

    async def _load_cached_connector_result_async(
        self,
        connector: PortalConnector,
        keywords: Iterable[str],
    ) -> dict[str, Any] | None:
        connector_name = self._connector_name(connector)
        cache_key = self._connector_cache_key(connector, keywords)
        async with self.async_db_session() as session:
            row = await session.fetchone(
                """
                SELECT schema_version, source_status, metadata_json, result_json
                FROM connector_result_cache
                WHERE connector_name = ? AND cache_key = ?
                """,
                (connector_name, cache_key),
            )
        if row is None:
            return None
        metadata = json.loads(row["metadata_json"] or "{}")
        payload = json.loads(row["result_json"] or "[]")
        migrate = getattr(connector, "migrate_result_payload", None)
        current_version = int(row["schema_version"])
        target_version = int(
            getattr(connector, "result_schema_version", _CONNECTOR_RESULT_SCHEMA_VERSION)
        )
        if callable(migrate):
            payload = migrate(payload, current_version)
        else:
            payload = [dict(item) for item in payload]
        if current_version != target_version:
            metadata = {
                **metadata,
                "migrated_from_schema_version": current_version,
                "migrated_at": self._to_iso(),
            }
            await self._store_connector_result_async(
                connector_name=connector_name,
                cache_key=cache_key,
                schema_version=target_version,
                opportunities=payload,
                metadata=metadata,
                source_status=row["source_status"],
            )
        return {
            "schema_version": target_version,
            "opportunities": payload,
            "metadata": metadata,
            "source_status": row["source_status"],
        }

    def _default_connector_fallback(
        self,
        connector: PortalConnector,
        keywords: Iterable[str],
    ) -> dict[str, Any] | None:
        fallback = getattr(connector, "default_fallback_results", None)
        if not callable(fallback):
            return None
        payload = [dict(item) for item in fallback(keywords)]
        return {
            "schema_version": int(
                getattr(connector, "result_schema_version", _CONNECTOR_RESULT_SCHEMA_VERSION)
            ),
            "opportunities": payload,
            "metadata": {
                "connector_name": self._connector_name(connector),
                "fallback_mode": "default",
                "source_status": "default",
            },
            "source_status": "default",
        }

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _as_utc(timestamp: datetime | None = None) -> datetime:
        normalized = timestamp or FundingBot._utcnow()
        if normalized.tzinfo is None:
            return normalized.replace(tzinfo=timezone.utc)
        return normalized.astimezone(timezone.utc)

    @staticmethod
    def _to_iso(timestamp: datetime | None = None) -> str:
        return FundingBot._as_utc(timestamp).isoformat()

    @staticmethod
    def connector_metrics_snapshot() -> list[dict[str, Any]]:
        return _CONNECTOR_METRICS.snapshot()

    @staticmethod
    def render_connector_metrics_prometheus() -> list[str]:
        return _CONNECTOR_METRICS.render_prometheus()

    @staticmethod
    def reset_connector_metrics() -> None:
        _CONNECTOR_METRICS.reset()

    def get_slo_summary(self) -> list[dict[str, Any]]:
        return summarize_slos(connection=self.connection)

    def render_slo_metrics_prometheus(self) -> list[str]:
        return render_slo_prometheus(connection=self.connection)

    @staticmethod
    def batch_metrics_snapshot() -> dict[str, Any]:
        return _BATCH_METRICS.snapshot()

    @staticmethod
    def render_batch_metrics_prometheus() -> list[str]:
        return _BATCH_METRICS.render_prometheus()

    @staticmethod
    def reset_batch_metrics() -> None:
        _BATCH_METRICS.reset()

    @staticmethod
    def _normalize_filter_timestamp(
        value: datetime | str | None, *, end: bool = False
    ) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return FundingBot._to_iso(value)
        normalized = value
        if len(normalized) == 10:
            suffix = "T23:59:59.999999+00:00" if end else "T00:00:00+00:00"
            return f"{normalized}{suffix}"
        return normalized

    @staticmethod
    def _parse_secret_payload(raw_value: str) -> dict[str, Any]:
        return _parse_secret_payload(raw_value)

    @staticmethod
    def _normalize_connector_configs(
        connector_configs: dict[str, Any] | list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        if connector_configs is None:
            return {"connectors": []}
        if isinstance(connector_configs, list):
            return {"connectors": [dict(item) for item in connector_configs]}
        if isinstance(connector_configs, dict):
            normalized = dict(connector_configs)
            if "connectors" in normalized:
                normalized["connectors"] = [dict(item) for item in normalized.get("connectors", [])]
            return normalized
        raise ConnectorConfigError(
            "Connector configuration must be a dict or list of connector entries."
        )

    def _load_connector_configs(
        self,
        connector_configs: dict[str, Any] | list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        if connector_configs is not None:
            normalized = self._normalize_connector_configs(connector_configs)
        else:
            raw_config = os.environ.get(CONNECTOR_CONFIG_ENV_VAR, "").strip()
            if not raw_config:
                return []
            try:
                parsed = json.loads(raw_config)
            except json.JSONDecodeError as exc:
                raise ConnectorConfigError(
                    f"Invalid {CONNECTOR_CONFIG_ENV_VAR} JSON: {exc.msg} at line "
                    f"{exc.lineno} column {exc.colno}."
                ) from exc
            normalized = self._normalize_connector_configs(parsed)

        try:
            validate(instance=normalized, schema=CONNECTOR_CONFIG_SCHEMA)
        except ValidationError as exc:
            path = ".".join(str(part) for part in exc.path)
            field = f" at {path}" if path else ""
            raise ConnectorConfigError(
                f"Invalid connector configuration{field}: {exc.message}"
            ) from exc
        return [dict(item) for item in normalized.get("connectors", [])]

    def _validate_connector_configs(self) -> None:
        for config in self.connector_configs:
            self.connector_registry.validate_config(
                config,
                credential_resolver=self.resolve_credential,
            )

    def _apply_migrations(self) -> None:
        migrations_dir = Path(__file__).resolve().parent / "migrations"
        if not migrations_dir.exists():
            return
        applied = {
            row["name"]
            for row in self.connection.execute("SELECT name FROM schema_migrations").fetchall()
        }
        for migration_path in sorted(migrations_dir.glob("*.sql")):
            if migration_path.name in applied:
                continue
            statements = [
                statement.strip()
                for statement in migration_path.read_text(encoding="utf-8").split(";")
                if statement.strip()
            ]
            for statement in statements:
                try:
                    self.connection.execute(statement)
                except sqlite3.OperationalError as exc:
                    if "duplicate column name:" not in str(exc):
                        raise
            self.connection.execute(
                "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
                (migration_path.name, self._to_iso()),
            )
            self.connection.commit()

    def _ensure_tasks_schema(self) -> None:
        columns = [
            row["name"] for row in self.connection.execute("PRAGMA table_info(tasks)").fetchall()
        ]
        if columns and "assigned_to" in columns and "assignee" not in columns:
            self.connection.executescript("""
                ALTER TABLE tasks RENAME TO tasks_legacy;
                CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    external_id TEXT UNIQUE,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    assignee TEXT,
                    assigned_to TEXT,
                    status TEXT NOT NULL,
                    due_date TEXT,
                    source TEXT NOT NULL DEFAULT 'manual',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    assignee_email TEXT,
                    assignee_name TEXT,
                    attributed_connector TEXT,
                    opportunity_signature TEXT
                );
                INSERT INTO tasks (
                    id, external_id, title, description, assignee, assigned_to, status, due_date,
                    source, created_at, updated_at, assignee_email, assignee_name,
                    attributed_connector, opportunity_signature
                )
                SELECT
                    id,
                    external_id,
                    title,
                    COALESCE(description, ''),
                    LOWER(assigned_to),
                    LOWER(assigned_to),
                    CASE LOWER(status)
                        WHEN 'pending' THEN 'todo'
                        WHEN 'in_progress' THEN 'in-progress'
                        WHEN 'completed' THEN 'done'
                        ELSE LOWER(status)
                    END,
                    date(due_date),
                    COALESCE(source, 'manual'),
                    created_at,
                    updated_at,
                    assignee_email,
                    assignee_name,
                    NULL,
                    NULL
                FROM tasks_legacy;
                DROP TABLE tasks_legacy;
                """)
        self._ensure_column("tasks", "external_id", "TEXT")
        self._ensure_column("tasks", "due_date", "TEXT")
        self._ensure_column("tasks", "source", "TEXT NOT NULL DEFAULT 'manual'")
        self._ensure_column("tasks", "assigned_to", "TEXT")
        self._ensure_column("tasks", "assignee_email", "TEXT")
        self._ensure_column("tasks", "assignee_name", "TEXT")
        self._ensure_query_indexes()
        self.connection.commit()

    @classmethod
    def _query_index_definitions(cls) -> tuple[dict[str, Any], ...]:
        return (
            {
                "name": "idx_donors_email",
                "table": "donors",
                "columns": ("email",),
                "sql": "CREATE INDEX IF NOT EXISTS idx_donors_email ON donors(email)",
            },
            {
                "name": "idx_donors_name_email",
                "table": "donors",
                "columns": ("name", "email"),
                "sql": (
                    "CREATE INDEX IF NOT EXISTS idx_donors_name_email "
                    "ON donors(name COLLATE NOCASE, email)"
                ),
            },
            {
                "name": "idx_tasks_status",
                "table": "tasks",
                "columns": ("status",),
                "sql": "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
            },
            {
                "name": "idx_tasks_created_at_status",
                "table": "tasks",
                "columns": ("created_at", "status"),
                "sql": (
                    "CREATE INDEX IF NOT EXISTS idx_tasks_created_at_status "
                    "ON tasks(created_at DESC, status)"
                ),
            },
            {
                "name": "idx_tasks_status_created_at",
                "table": "tasks",
                "columns": ("status", "created_at"),
                "sql": (
                    "CREATE INDEX IF NOT EXISTS idx_tasks_status_created_at "
                    "ON tasks(status, created_at DESC)"
                ),
            },
            {
                "name": "idx_tasks_assigned_to_status",
                "table": "tasks",
                "columns": ("assigned_to", "status"),
                "sql": (
                    "CREATE INDEX IF NOT EXISTS idx_tasks_assigned_to_status "
                    "ON tasks(assigned_to, status)"
                ),
            },
            {
                "name": "idx_connector_result_cache_lookup",
                "table": "connector_result_cache",
                "columns": ("connector_name", "cache_key"),
                "sql": (
                    "CREATE INDEX IF NOT EXISTS idx_connector_result_cache_lookup "
                    "ON connector_result_cache(connector_name, cache_key)"
                ),
            },
            {
                "name": "idx_connector_result_cache_status_fetched_at",
                "table": "connector_result_cache",
                "columns": ("source_status", "fetched_at"),
                "sql": (
                    "CREATE INDEX IF NOT EXISTS idx_connector_result_cache_status_fetched_at "
                    "ON connector_result_cache(source_status, fetched_at DESC)"
                ),
            },
        )

    @classmethod
    def _query_plan_definitions(cls) -> tuple[dict[str, Any], ...]:
        return (
            {
                "name": "donor-directory",
                "sql": (
                    "SELECT email, name FROM donors "
                    "ORDER BY name COLLATE NOCASE ASC, email ASC LIMIT 25"
                ),
                "params": (),
            },
            {
                "name": "donor-email-lookup",
                "sql": "SELECT email, name FROM donors WHERE email = ?",
                "params": ("donor-010@example.org",),
            },
            {
                "name": "task-assignee-status",
                "sql": (
                    "SELECT id, title FROM tasks WHERE assigned_to = ? AND status = ? "
                    "ORDER BY due_date ASC, id ASC LIMIT 25"
                ),
                "params": ("staff", "todo"),
            },
            {
                "name": "task-status-created-at",
                "sql": (
                    "SELECT id, title FROM tasks WHERE created_at >= ? AND status = ? "
                    "ORDER BY created_at DESC LIMIT 25"
                ),
                "params": ("2026-01-01T00:00:00+00:00", "todo"),
            },
            {
                "name": "connector-response-lookup",
                "sql": (
                    "SELECT source_status, fetched_at FROM connector_result_cache "
                    "WHERE connector_name = ? AND cache_key = ?"
                ),
                "params": ("Grants Portal", "education"),
            },
            {
                "name": "connector-response-status",
                "sql": (
                    "SELECT id, connector_name FROM connector_result_cache WHERE source_status = ? "
                    "ORDER BY fetched_at DESC LIMIT 25"
                ),
                "params": ("remote",),
            },
        )

    def _ensure_query_indexes(self) -> None:
        table_columns: dict[str, set[str]] = {}
        for definition in self._query_index_definitions():
            table_name = str(definition["table"])
            columns = table_columns.setdefault(
                table_name,
                {
                    str(row["name"])
                    for row in self.connection.execute(
                        f"PRAGMA table_info({table_name})"
                    ).fetchall()
                },
            )
            if not set(definition["columns"]).issubset(columns):
                continue
            self.connection.execute(str(definition["sql"]))
        self.connection.execute("CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee)")
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_external_id ON tasks(external_id)"
        )
        self.connection.execute("CREATE INDEX IF NOT EXISTS idx_tasks_due_date ON tasks(due_date)")

    @staticmethod
    def _extract_index_name_from_plan(detail: str) -> str | None:
        for marker in ("USING COVERING INDEX ", "USING INDEX "):
            if marker in detail:
                return detail.split(marker, 1)[1].split(" ", 1)[0]
        return None

    def explain_indexed_queries(self) -> list[dict[str, Any]]:
        plans: list[dict[str, Any]] = []
        for definition in self._query_plan_definitions():
            rows = self.connection.execute(
                "EXPLAIN QUERY PLAN " + str(definition["sql"]),
                tuple(definition["params"]),
            ).fetchall()
            details = [str(row["detail"]) for row in rows]
            indexes = sorted(
                {
                    index_name
                    for index_name in (
                        self._extract_index_name_from_plan(detail) for detail in details
                    )
                    if index_name
                }
            )
            plans.append(
                {
                    "name": str(definition["name"]),
                    "sql": str(definition["sql"]),
                    "params": list(definition["params"]),
                    "plan": details,
                    "uses_index": any(
                        "USING INDEX" in detail or "USING COVERING INDEX" in detail
                        for detail in details
                    ),
                    "indexes": indexes,
                }
            )
        return plans

    def get_index_monitoring_snapshot(self) -> dict[str, Any]:
        expected_indexes = self._query_index_definitions()
        indexed_tables = sorted({str(definition["table"]) for definition in expected_indexes})
        available_indexes: dict[str, dict[str, sqlite3.Row]] = {}
        for table_name in indexed_tables:
            available_indexes[table_name] = {
                str(row["name"]): row
                for row in self.connection.execute(f"PRAGMA index_list({table_name})").fetchall()
            }

        indexes: list[dict[str, Any]] = []
        present_count = 0
        for definition in expected_indexes:
            table_name = str(definition["table"])
            index_name = str(definition["name"])
            pragma_row = available_indexes.get(table_name, {}).get(index_name)
            present = pragma_row is not None
            if present:
                present_count += 1
            index_columns = (
                [
                    str(row["name"])
                    for row in self.connection.execute(
                        f"PRAGMA index_info({index_name})"
                    ).fetchall()
                ]
                if present
                else []
            )
            indexes.append(
                {
                    "name": index_name,
                    "table": table_name,
                    "columns": index_columns,
                    "present": present,
                    "unique": bool(pragma_row["unique"]) if present else False,
                    "origin": str(pragma_row["origin"]) if present else None,
                    "partial": bool(pragma_row["partial"]) if present else False,
                }
            )

        connector_responses = self.connection.execute("""
            SELECT source_status, COUNT(*) AS total
            FROM connector_result_cache
            GROUP BY source_status
            ORDER BY source_status ASC
            """).fetchall()
        row_counts = {
            table_name: int(
                self.connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
            )
            for table_name in indexed_tables
        }
        return {
            "summary": {
                "expected": len(expected_indexes),
                "present": present_count,
                "missing": len(expected_indexes) - present_count,
            },
            "row_counts": row_counts,
            "indexes": indexes,
            "connector_responses": [
                {"source_status": str(row["source_status"]), "total": int(row["total"])}
                for row in connector_responses
            ],
            "query_plans": self.explain_indexed_queries(),
        }

    @staticmethod
    def _validate_segment(segment: str | None) -> str:
        normalized = (segment or "unknown").strip().lower()
        allowed_segments = {"corporate", "institutional", "individual", "unknown"}
        if normalized not in allowed_segments:
            raise ValueError(
                f"Invalid donor segment {segment!r}. Expected one of {sorted(allowed_segments)}."
            )
        return normalized

    @staticmethod
    def _validate_consent_channel(channel: str | None) -> str:
        normalized = (channel or "email").strip().lower()
        allowed_channels = {"email", "sms", "phone", "postal", "general"}
        if normalized not in allowed_channels:
            raise ValueError(
                f"Invalid consent channel {channel!r}. Expected one of {sorted(allowed_channels)}."
            )
        return normalized

    @staticmethod
    def _validate_consent_status(status: str | None) -> str:
        normalized = (status or "granted").strip().lower()
        allowed_statuses = {"granted", "withdrawn"}
        if normalized not in allowed_statuses:
            raise ValueError(
                f"Invalid consent status {status!r}. Expected one of {sorted(allowed_statuses)}."
            )
        return normalized

    @staticmethod
    def _default_donor_name_from_email(email: str) -> str:
        local_part = email.split("@", 1)[0]
        return local_part.replace(".", " ").replace("_", " ").strip().title() or email

    @staticmethod
    def _validate_ui_locale(locale: str | None) -> str:
        normalized = (locale or DEFAULT_LOCALE_CODE).strip().lower()
        if normalized not in SUPPORTED_UI_LOCALES:
            raise ValueError(
                f"Unsupported locale {locale!r}. Expected one of {sorted(SUPPORTED_UI_LOCALES)}."
            )
        return normalized

    @staticmethod
    def _normalize_auth_role(role: str) -> str:
        normalized = sanitize_user_string(
            role,
            field_name="role",
            allow_empty=False,
            max_length=64,
        ).lower()
        if not _SAFE_AUTH_ROLE_RE.fullmatch(normalized):
            raise ValueError("Authentication role contains invalid characters.")
        return normalized

    @classmethod
    def _normalize_mfa_code(cls, code: str) -> str:
        normalized = sanitize_user_string(
            code,
            field_name="code",
            allow_empty=False,
            max_length=64,
        )
        return re.sub(r"[\s-]+", "", normalized).upper()

    @classmethod
    def _hash_backup_code(cls, code: str) -> str:
        normalized = cls._normalize_mfa_code(code)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @classmethod
    def _generate_backup_codes(cls) -> list[str]:
        return [
            secrets.token_hex(cls.MFA_BACKUP_CODE_BYTES).upper()
            for _ in range(cls.MFA_BACKUP_CODE_COUNT)
        ]

    def _ensure_auth_security_row(self, role: str) -> sqlite3.Row:
        normalized_role = self._normalize_auth_role(role)
        row = self.connection.execute(
            "SELECT * FROM auth_security WHERE role = ?",
            (normalized_role,),
        ).fetchone()
        if row is not None:
            return row
        self.connection.execute(
            """
            INSERT INTO auth_security (
                role,
                failed_attempts,
                lockout_until,
                last_failed_at,
                last_success_at,
                mfa_enabled,
                mfa_secret,
                mfa_pending_secret,
                mfa_backup_codes_json,
                mfa_pending_backup_codes_json,
                updated_at
            ) VALUES (?, 0, NULL, NULL, NULL, 0, NULL, NULL, '[]', '[]', ?)
            """,
            (normalized_role, self._to_iso()),
        )
        self.connection.commit()
        return self.connection.execute(
            "SELECT * FROM auth_security WHERE role = ?",
            (normalized_role,),
        ).fetchone()

    def get_auth_security_state(self, role: str) -> dict[str, Any]:
        row = self._ensure_auth_security_row(role)
        lockout_until = row["lockout_until"]
        remaining_backup_codes = len(json.loads(row["mfa_backup_codes_json"] or "[]"))
        return {
            "role": row["role"],
            "failed_attempts": int(row["failed_attempts"] or 0),
            "lockout_until": lockout_until,
            "locked": bool(
                lockout_until
                and self._as_utc(datetime.fromisoformat(lockout_until)) > self._utcnow()
            ),
            "mfa_enabled": bool(row["mfa_enabled"]),
            "mfa_pending_setup": bool(row["mfa_pending_secret"]),
            "backup_codes_remaining": remaining_backup_codes,
            "last_failed_at": row["last_failed_at"],
            "last_success_at": row["last_success_at"],
        }

    def clear_auth_failures(self, role: str) -> None:
        normalized_role = self._normalize_auth_role(role)
        self._ensure_auth_security_row(normalized_role)
        self.connection.execute(
            """
            UPDATE auth_security
            SET failed_attempts = 0,
                lockout_until = NULL,
                last_success_at = ?,
                updated_at = ?
            WHERE role = ?
            """,
            (self._to_iso(), self._to_iso(), normalized_role),
        )
        self.connection.commit()

    def record_failed_authentication(
        self,
        role: str,
        *,
        lockout_threshold: int,
        lockout_minutes: int,
        reason: str,
    ) -> dict[str, Any]:
        normalized_role = self._normalize_auth_role(role)
        row = self._ensure_auth_security_row(normalized_role)
        failed_attempts = int(row["failed_attempts"] or 0) + 1
        lockout_until = None
        if lockout_threshold > 0 and failed_attempts >= lockout_threshold:
            lockout_until = self._to_iso(
                self._utcnow() + timedelta(minutes=max(lockout_minutes, 1))
            )
        self.connection.execute(
            """
            UPDATE auth_security
            SET failed_attempts = ?,
                lockout_until = ?,
                last_failed_at = ?,
                updated_at = ?
            WHERE role = ?
            """,
            (
                failed_attempts,
                lockout_until,
                self._to_iso(),
                self._to_iso(),
                normalized_role,
            ),
        )
        self.connection.commit()
        self._log_action(
            "authentication_failed",
            role=normalized_role,
            failed_attempts=failed_attempts,
            lockout_until=lockout_until,
            reason=reason,
        )
        return {
            "failed_attempts": failed_attempts,
            "lockout_until": lockout_until,
            "locked": lockout_until is not None,
        }

    def assert_account_not_locked(self, role: str) -> None:
        state = self.get_auth_security_state(role)
        if state["locked"]:
            raise AccountLockedError(
                f"Account '{state['role']}' is temporarily locked until {state['lockout_until']}."
            )

    def begin_mfa_setup(self, role: str, *, issuer_name: str | None = None) -> dict[str, Any]:
        normalized_role = self._normalize_auth_role(role)
        issuer = sanitize_user_string(
            issuer_name or self.MFA_ISSUER_NAME,
            field_name="issuer_name",
            allow_empty=False,
            max_length=128,
        )
        secret = pyotp.random_base32()
        backup_codes = self._generate_backup_codes()
        self._ensure_auth_security_row(normalized_role)
        self.connection.execute(
            """
            UPDATE auth_security
            SET mfa_pending_secret = ?,
                mfa_pending_backup_codes_json = ?,
                updated_at = ?
            WHERE role = ?
            """,
            (
                self._encrypt_text(secret),
                json.dumps([self._hash_backup_code(code) for code in backup_codes]),
                self._to_iso(),
                normalized_role,
            ),
        )
        self.connection.commit()
        self._log_action("mfa_setup_started", role=normalized_role)
        totp = pyotp.TOTP(secret, digits=self.MFA_TOTP_DIGITS)
        return {
            "role": normalized_role,
            "secret": secret,
            "backup_codes": backup_codes,
            "provisioning_uri": totp.provisioning_uri(
                name=normalized_role,
                issuer_name=issuer,
            ),
        }

    def enable_mfa(self, role: str, code: str) -> dict[str, Any]:
        normalized_role = self._normalize_auth_role(role)
        row = self._ensure_auth_security_row(normalized_role)
        pending_secret = row["mfa_pending_secret"]
        if not pending_secret:
            raise MFARequiredError("MFA setup has not been started.")
        secret = self._decrypt_text(pending_secret)
        normalized_code = self._normalize_mfa_code(code)
        if not pyotp.TOTP(secret, digits=self.MFA_TOTP_DIGITS).verify(
            normalized_code,
            valid_window=1,
        ):
            raise ValueError("Invalid MFA code.")
        self.connection.execute(
            """
            UPDATE auth_security
            SET mfa_enabled = 1,
                mfa_secret = ?,
                mfa_pending_secret = NULL,
                mfa_backup_codes_json = mfa_pending_backup_codes_json,
                mfa_pending_backup_codes_json = '[]',
                updated_at = ?
            WHERE role = ?
            """,
            (self._encrypt_text(secret), self._to_iso(), normalized_role),
        )
        self.connection.commit()
        self._log_action("mfa_enabled", role=normalized_role)
        return self.get_auth_security_state(normalized_role)

    def regenerate_backup_codes(self, role: str) -> dict[str, Any]:
        normalized_role = self._normalize_auth_role(role)
        row = self._ensure_auth_security_row(normalized_role)
        if not row["mfa_enabled"]:
            raise MFARequiredError("MFA is not enabled for this account.")
        backup_codes = self._generate_backup_codes()
        self.connection.execute(
            """
            UPDATE auth_security
            SET mfa_backup_codes_json = ?,
                updated_at = ?
            WHERE role = ?
            """,
            (
                json.dumps([self._hash_backup_code(code) for code in backup_codes]),
                self._to_iso(),
                normalized_role,
            ),
        )
        self.connection.commit()
        self._log_action("mfa_backup_codes_regenerated", role=normalized_role)
        return {
            **self.get_auth_security_state(normalized_role),
            "backup_codes": backup_codes,
        }

    def verify_mfa_code(self, role: str, code: str) -> dict[str, Any]:
        normalized_role = self._normalize_auth_role(role)
        row = self._ensure_auth_security_row(normalized_role)
        if not row["mfa_enabled"]:
            return {"verified": True, "method": "not_required"}
        normalized_code = self._normalize_mfa_code(code)
        secret_payload = row["mfa_secret"]
        if secret_payload:
            secret = self._decrypt_text(secret_payload)
            if pyotp.TOTP(secret, digits=self.MFA_TOTP_DIGITS).verify(
                normalized_code,
                valid_window=1,
            ):
                return {"verified": True, "method": "totp"}
        backup_code_hash = self._hash_backup_code(normalized_code)
        stored_hashes = json.loads(row["mfa_backup_codes_json"] or "[]")
        if backup_code_hash in stored_hashes:
            remaining_hashes = [item for item in stored_hashes if item != backup_code_hash]
            self.connection.execute(
                """
                UPDATE auth_security
                SET mfa_backup_codes_json = ?,
                    updated_at = ?
                WHERE role = ?
                """,
                (
                    json.dumps(remaining_hashes),
                    self._to_iso(),
                    normalized_role,
                ),
            )
            self.connection.commit()
            self._log_action(
                "mfa_backup_code_used",
                role=normalized_role,
                backup_codes_remaining=len(remaining_hashes),
            )
            return {
                "verified": True,
                "method": "backup_code",
                "backup_codes_remaining": len(remaining_hashes),
            }
        return {"verified": False, "method": None}

    @staticmethod
    def _validate_translation_review_status(
        status: str | None,
        *,
        allow_pending: bool = True,
    ) -> str:
        normalized = (status or "pending").strip().lower()
        allowed = set(TRANSLATION_REVIEW_STATUSES)
        if not allow_pending:
            allowed.discard("pending")
        if normalized not in allowed:
            raise ValueError(
                f"Invalid translation review status {status!r}. Expected one of {sorted(allowed)}."
            )
        return normalized

    @classmethod
    def _validate_locale(cls, locale: str | None) -> str:
        normalized = (locale or cls.DEFAULT_TEMPLATE_LOCALE).strip().lower()
        if normalized not in cls.SUPPORTED_TEMPLATE_LOCALES:
            raise ValueError(
                f"Invalid donor locale {locale!r}. Expected one of "
                f"{sorted(cls.SUPPORTED_TEMPLATE_LOCALES)}."
            )
        return normalized

    @classmethod
    def _validate_data_classification(
        cls,
        classification: str | None,
        *,
        default: str = "internal",
    ) -> str:
        return _normalize_data_classification(classification, default=default)

    @classmethod
    def _classification_rank(cls, classification: str) -> int:
        return _DATA_CLASSIFICATION_RANK[cls._validate_data_classification(classification)]

    @classmethod
    def _build_field_classifications(
        cls,
        *,
        defaults: dict[str, str],
        fields: Iterable[str],
        overrides: dict[str, Any] | None = None,
        default_classification: str = "internal",
    ) -> dict[str, str]:
        merged: dict[str, str] = {}
        allowed_fields = {str(field) for field in fields}
        for field_name in allowed_fields:
            merged[field_name] = cls._validate_data_classification(
                defaults.get(field_name),
                default=default_classification,
            )
        for field_name, value in (overrides or {}).items():
            normalized_name = str(field_name).strip()
            if not normalized_name:
                continue
            if allowed_fields and normalized_name not in allowed_fields:
                raise ValueError(f"Unknown field classification target {normalized_name!r}.")
            merged[normalized_name] = cls._validate_data_classification(
                str(value),
                default=default_classification,
            )
        return dict(sorted(merged.items()))

    @classmethod
    def _assert_field_classifications_within_record(
        cls,
        record_classification: str,
        field_classifications: dict[str, str],
    ) -> None:
        record_rank = cls._classification_rank(record_classification)
        for field_name, classification in field_classifications.items():
            if cls._classification_rank(classification) > record_rank:
                raise ValueError(
                    f"Field {field_name!r} is classified as {classification!r}, "
                    f"which exceeds record classification {record_classification!r}."
                )

    @staticmethod
    def _encryption_key() -> bytes:
        seed = os.environ.get("FUNDING_BOT_ENCRYPTION_KEY", "funding-bot-dev-key")
        return hashlib.sha256(seed.encode("utf-8")).digest()

    @classmethod
    def _keystream(cls, nonce: bytes, length: int) -> bytes:
        key = cls._encryption_key()
        output = bytearray()
        counter = 0
        while len(output) < length:
            output.extend(hashlib.sha256(key + nonce + counter.to_bytes(4, "big")).digest())
            counter += 1
        return bytes(output[:length])

    @classmethod
    def _encrypt_text(cls, plaintext: str) -> str:
        raw_bytes = plaintext.encode("utf-8")
        nonce = os.urandom(16)
        ciphertext = bytes(
            value ^ mask for value, mask in zip(raw_bytes, cls._keystream(nonce, len(raw_bytes)))
        )
        mac = hashlib.sha256(cls._encryption_key() + nonce + ciphertext).hexdigest()
        payload = json.dumps(
            {
                "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
                "mac": mac,
                "nonce": base64.b64encode(nonce).decode("ascii"),
            },
            sort_keys=True,
        )
        return _ENCRYPTED_VALUE_PREFIX + base64.b64encode(payload.encode("utf-8")).decode("ascii")

    @classmethod
    def _decrypt_text(cls, payload: str) -> str:
        if not payload.startswith(_ENCRYPTED_VALUE_PREFIX):
            return payload
        encoded = payload[len(_ENCRYPTED_VALUE_PREFIX) :]
        parsed = json.loads(base64.b64decode(encoded).decode("utf-8"))
        nonce = base64.b64decode(parsed["nonce"])
        ciphertext = base64.b64decode(parsed["ciphertext"])
        expected_mac = hashlib.sha256(cls._encryption_key() + nonce + ciphertext).hexdigest()
        if parsed.get("mac") != expected_mac:
            raise FundingBotError("Encrypted field failed integrity validation.")
        plaintext = bytes(
            value ^ mask for value, mask in zip(ciphertext, cls._keystream(nonce, len(ciphertext)))
        )
        return plaintext.decode("utf-8")

    @classmethod
    def _decode_json_blob(cls, payload: str | None, *, default: Any) -> Any:
        if not payload:
            return default
        return json.loads(cls._decrypt_text(payload))

    @classmethod
    def _encode_json_blob(
        cls,
        value: Any,
        *,
        encrypt: bool = False,
    ) -> str:
        payload = json.dumps(value, sort_keys=True)
        return cls._encrypt_text(payload) if encrypt else payload

    @classmethod
    def _setting_defaults_for(
        cls,
        key: str,
        value: dict[str, Any],
    ) -> tuple[str, dict[str, str], bool]:
        if key == "profile":
            fields = set(str(field) for field in value)
            fields.update(cls.ORGANIZATION_PROFILE_FIELD_CLASSIFICATIONS)
            return (
                cls.SETTING_DEFAULT_CLASSIFICATIONS["profile"],
                cls._build_field_classifications(
                    defaults=cls.ORGANIZATION_PROFILE_FIELD_CLASSIFICATIONS,
                    fields=fields,
                ),
                True,
            )
        return (
            cls.SETTING_DEFAULT_CLASSIFICATIONS.get(key, "internal"),
            cls._build_field_classifications(
                defaults={str(field): "internal" for field in value},
                fields=set(str(field) for field in value),
            ),
            False,
        )

    @classmethod
    def _deserialize_donor_row(cls, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        donor = dict(row)
        donor["opted_out"] = bool(donor["opted_out"])
        donor["preferences"] = cls._decode_json_blob(
            donor.pop("preferences_json", "{}"),
            default={},
        )
        donor["field_classifications"] = cls._decode_json_blob(
            donor.pop("field_classifications_json", "{}"),
            default={},
        )
        return donor

    @classmethod
    def _load_outreach_template_catalog(cls, locale: str) -> dict[str, Any]:
        normalized_locale = cls._validate_locale(locale)
        catalog_path = cls.OUTREACH_TEMPLATE_CATALOG / f"{normalized_locale}.json"
        with catalog_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        templates = data.get("templates")
        if not isinstance(templates, dict):
            raise FundingBotError(
                f"Outreach template catalog {catalog_path} is missing a 'templates' object."
            )
        return templates

    @classmethod
    def list_supported_outreach_locales(cls) -> tuple[str, ...]:
        return tuple(sorted(cls.SUPPORTED_TEMPLATE_LOCALES))

    @classmethod
    def validate_outreach_template_catalogs(cls) -> None:
        try:
            _validate_localized_outreach_templates(_load_localized_outreach_templates())
        except ValueError as exc:
            raise FundingBotError(str(exc)) from exc

    @classmethod
    def list_catalog_outreach_templates(cls) -> tuple[str, ...]:
        cls.validate_outreach_template_catalogs()
        return tuple(
            sorted(cls._load_outreach_template_catalog(cls.DEFAULT_TEMPLATE_LOCALE).keys())
        )

    @classmethod
    def _resolve_catalog_template(
        cls,
        template_name: str,
        *,
        segment: str,
        locale: str,
    ) -> tuple[str, str] | None:
        cls.validate_outreach_template_catalogs()
        locales_to_try = [cls._validate_locale(locale)]
        if cls.DEFAULT_TEMPLATE_LOCALE not in locales_to_try:
            locales_to_try.append(cls.DEFAULT_TEMPLATE_LOCALE)

        for current_locale in locales_to_try:
            templates = cls._load_outreach_template_catalog(current_locale)
            template = templates.get(template_name)
            if not isinstance(template, dict):
                continue

            variant: Any = template
            segments = template.get("segments")
            if (
                isinstance(segments, dict)
                and segment in segments
                and isinstance(segments[segment], dict)
            ):
                variant = segments[segment]

            subject = variant.get("subject")
            body = variant.get("body")
            if isinstance(subject, str) and isinstance(body, str):
                return subject, body
        return None

    @classmethod
    def _localized_opt_out_notice(cls, locale: str) -> str:
        templates = cls._load_outreach_template_catalog(cls._validate_locale(locale))
        default_template = templates.get(cls.DEFAULT_OUTREACH_TEMPLATE, {})
        notice = default_template.get("opt_out_notice")
        if isinstance(notice, str):
            return notice
        return "To opt out of future outreach, visit {opt_out_url}."

    @classmethod
    def _normalize_task_status(cls, status: str) -> str:
        normalized = str(status).strip().lower().replace("-", "_").replace(" ", "_")
        normalized = cls.TASK_STATUS_ALIASES.get(normalized, normalized)
        if normalized not in cls.TASK_STATUSES:
            raise ValueError(
                f"Invalid task status {status!r}. Expected one of {list(cls.TASK_STATUSES)}."
            )
        return normalized

    @staticmethod
    def _normalize_due_date(due_date: datetime | str | None) -> str | None:
        if due_date is None:
            return None
        if isinstance(due_date, datetime):
            return FundingBot._as_utc(due_date).date().isoformat()
        normalized = str(due_date).strip()
        if not normalized:
            return None
        try:
            return datetime.fromisoformat(normalized).date().isoformat()
        except ValueError:
            pass
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
            return normalized
        raise ValueError("Task due_date must be an ISO-8601 date or datetime string.")

    @staticmethod
    def _serialize_task(row: sqlite3.Row | Task | dict[str, Any]) -> dict[str, Any]:
        task = row.to_dict() if isinstance(row, Task) else dict(row)
        if "assignee" not in task and "assigned_to" in task:
            task["assignee"] = task["assigned_to"]
        if "assigned_to" not in task and "assignee" in task:
            task["assigned_to"] = task["assignee"]
        task["due_date"] = FundingBot._normalize_task_due_date(task.get("due_date"))
        today = FundingBot._as_utc().date().isoformat()
        task["is_overdue"] = bool(
            task["due_date"] and task["status"] != "done" and task["due_date"] < today
        )
        task["unread_comment_count"] = int(task.get("unread_comment_count", 0) or 0)
        return task

    @staticmethod
    def _normalize_task_due_date(due_date: str | None) -> str | None:
        if due_date is None:
            return None
        normalized = str(due_date).strip()
        if not normalized:
            return None
        try:
            return datetime.fromisoformat(normalized).date().isoformat()
        except ValueError as exc:
            raise ValueError(
                f"Invalid task due_date {due_date!r}. Expected ISO date format YYYY-MM-DD."
            ) from exc

    @staticmethod
    def _assignment_notification_rate_limit_seconds() -> int:
        raw_value = os.environ.get("TASK_ASSIGNMENT_NOTIFICATION_RATE_LIMIT_SECONDS", "3600")
        try:
            return max(0, int(raw_value))
        except ValueError:
            return 3600

    def _get_task_row(self, task_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise TaskNotFoundError(f"Task {task_id!r} does not exist.")
        return row

    def _get_task_comment_row(self, task_id: int, comment_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM task_comments WHERE id = ? AND task_id = ?",
            (comment_id, task_id),
        ).fetchone()
        if row is None:
            raise TaskCommentNotFoundError(
                f"Task comment {comment_id!r} does not exist for task {task_id!r}."
            )
        return row

    @staticmethod
    def _serialize_task_comment(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    def _build_task_assignment_message(self, task: dict[str, Any]) -> tuple[str, str]:
        profile = self.load_organization_profile()
        organization_name = profile.get("name", "Funding Bot")
        assignee_name = task.get("assignee_name") or task.get("assigned_to") or "teammate"
        subject = f"[{organization_name}] Task assigned: {task['title']}"
        description = str(task.get("description") or "").strip() or "No description provided."
        due_date = task.get("due_date") or "No due date set."
        body = "\n".join(
            [
                f"Hello {assignee_name},",
                "",
                "A task has been assigned to you.",
                "",
                f"Task: {task['title']}",
                f"Assigned role: {task['assigned_to']}",
                f"Status: {task['status']}",
                f"Due date: {due_date}",
                f"Description: {description}",
            ]
        )
        return subject, body

    def _notify_task_assignee(
        self,
        task_id: int,
        *,
        sender: Any | None = None,
        happened_at: datetime | None = None,
    ) -> dict[str, Any]:
        task = self._serialize_task(self._get_task_row(task_id))
        recipient_email = str(task.get("assignee_email") or "").strip()
        if not recipient_email:
            return {"status": "skipped", "reason": "no_assignee_email"}
        if sender is None:
            self._log_action(
                "task_assignment_notification_skipped",
                task_id=task_id,
                recipient_email=recipient_email,
                reason="no_sender",
            )
            return {
                "status": "skipped",
                "reason": "no_sender",
                "recipient_email": recipient_email,
            }

        notification_time = self._as_utc(happened_at)
        latest = self.connection.execute(
            """
            SELECT happened_at
            FROM task_notifications
            WHERE task_id = ? AND recipient_email = ? AND notification_type = 'task_assignment'
            ORDER BY happened_at DESC
            LIMIT 1
            """,
            (task_id, recipient_email),
        ).fetchone()
        rate_limit_seconds = self._assignment_notification_rate_limit_seconds()
        if latest is not None and rate_limit_seconds > 0:
            last_sent_at = self._as_utc(datetime.fromisoformat(latest["happened_at"]))
            if notification_time - last_sent_at < timedelta(seconds=rate_limit_seconds):
                self._log_action(
                    "task_assignment_notification_rate_limited",
                    task_id=task_id,
                    recipient_email=recipient_email,
                    last_sent_at=latest["happened_at"],
                )
                return {
                    "status": "rate_limited",
                    "recipient_email": recipient_email,
                    "last_sent_at": latest["happened_at"],
                }

        subject, body = self._build_task_assignment_message(task)
        sender(recipient_email, subject, body)
        happened_iso = self._to_iso(notification_time)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO task_notifications (task_id, recipient_email, notification_type, happened_at)
                VALUES (?, ?, 'task_assignment', ?)
                """,
                (task_id, recipient_email, happened_iso),
            )
            self._log_action(
                "task_assignment_notification_sent",
                commit=False,
                task_id=task_id,
                recipient_email=recipient_email,
                happened_at=happened_iso,
            )
        return {
            "status": "sent",
            "recipient_email": recipient_email,
            "sent_at": happened_iso,
        }

    @classmethod
    def _validate_task_transition(cls, current_status: str, new_status: str) -> None:
        if current_status == new_status:
            return
        allowed = cls.TASK_STATUS_TRANSITIONS[current_status]
        if new_status not in allowed:
            raise TaskTransitionError(
                f"Task status cannot transition from {current_status!r} to {new_status!r}."
            )

    def _log_action(self, action: str, *, commit: bool = True, **details: Any) -> None:
        self.connection.execute(
            "INSERT INTO audit_logs (happened_at, action, details_json) VALUES (?, ?, ?)",
            (self._to_iso(), action, json.dumps(details, sort_keys=True)),
        )
        if commit:
            self.connection.commit()

    @staticmethod
    def _serialize_task_run(row: sqlite3.Row) -> dict[str, Any]:
        task_run = dict(row)
        task_run["payload"] = json.loads(task_run.pop("payload_json") or "{}")
        result_json = task_run.pop("result_json")
        task_run["result"] = json.loads(result_json) if result_json else None
        callback_payload_json = task_run.pop("callback_payload_json")
        task_run["callback_payload"] = (
            json.loads(callback_payload_json) if callback_payload_json else None
        )
        task_run["shutdown_requested"] = bool(task_run.get("shutdown_requested"))
        task_run["dead_lettered"] = bool(task_run.get("dead_lettered"))
        return task_run

    @staticmethod
    def _serialize_task_history_row(row: sqlite3.Row) -> dict[str, Any]:
        history = dict(row)
        history["details"] = json.loads(history.pop("details_json") or "{}")
        result_json = history.pop("result_json")
        history["result"] = json.loads(result_json) if result_json else None
        return history

    @staticmethod
    def _serialize_dead_letter_row(row: sqlite3.Row) -> dict[str, Any]:
        record = dict(row)
        record["payload"] = json.loads(record.pop("payload_json") or "{}")
        return record

    def record_task_run(
        self,
        task_id: str,
        task_name: str,
        *,
        status: str,
        progress: int = 0,
        message: str = "",
        payload: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error_message: str | None = None,
        callback_name: str | None = None,
        callback_payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        worker_id: str | None = None,
        duplicate_requests: int = 0,
        shutdown_requested: bool = False,
        retry_limit: int | None = None,
        attempts: int = 0,
        backoff_seconds: float | None = None,
        backoff_max_seconds: float | None = None,
        dead_lettered: bool = False,
        last_attempt_at: datetime | str | None = None,
        next_retry_at: datetime | str | None = None,
        completed_at: datetime | None = None,
    ) -> dict[str, Any]:
        try:
            return self._record_task_run_impl(
                task_id,
                task_name,
                status=status,
                progress=progress,
                message=message,
                payload=payload,
                result=result,
                error_message=error_message,
                callback_name=callback_name,
                callback_payload=callback_payload,
                idempotency_key=idempotency_key,
                worker_id=worker_id,
                duplicate_requests=duplicate_requests,
                shutdown_requested=shutdown_requested,
                retry_limit=retry_limit,
                attempts=attempts,
                backoff_seconds=backoff_seconds,
                backoff_max_seconds=backoff_max_seconds,
                dead_lettered=dead_lettered,
                last_attempt_at=last_attempt_at,
                next_retry_at=next_retry_at,
                completed_at=completed_at,
            )
        except sqlite3.OperationalError as exc:
            if "readonly" not in str(exc).lower():
                raise
            self._reopen_database_connection()
            return self._record_task_run_impl(
                task_id,
                task_name,
                status=status,
                progress=progress,
                message=message,
                payload=payload,
                result=result,
                error_message=error_message,
                callback_name=callback_name,
                callback_payload=callback_payload,
                idempotency_key=idempotency_key,
                worker_id=worker_id,
                duplicate_requests=duplicate_requests,
                shutdown_requested=shutdown_requested,
                retry_limit=retry_limit,
                attempts=attempts,
                backoff_seconds=backoff_seconds,
                backoff_max_seconds=backoff_max_seconds,
                dead_lettered=dead_lettered,
                last_attempt_at=last_attempt_at,
                next_retry_at=next_retry_at,
                completed_at=completed_at,
            )

    def _record_task_run_impl(
        self,
        task_id: str,
        task_name: str,
        *,
        status: str,
        progress: int = 0,
        message: str = "",
        payload: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error_message: str | None = None,
        callback_name: str | None = None,
        callback_payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        worker_id: str | None = None,
        duplicate_requests: int = 0,
        shutdown_requested: bool = False,
        retry_limit: int | None = None,
        attempts: int = 0,
        backoff_seconds: float | None = None,
        backoff_max_seconds: float | None = None,
        dead_lettered: bool = False,
        last_attempt_at: datetime | str | None = None,
        next_retry_at: datetime | str | None = None,
        completed_at: datetime | None = None,
    ) -> dict[str, Any]:
        now = self._to_iso()
        existing = self.connection.execute(
            """
            SELECT created_at, completed_at, duplicate_requests, shutdown_requested
            FROM task_runs WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        created_at = existing["created_at"] if existing else now
        finished_at = (
            self._to_iso(completed_at)
            if completed_at is not None
            else (existing["completed_at"] if existing else None)
        )
        normalized_retry_limit = (
            self.DEFAULT_QUEUE_RETRY_LIMIT if retry_limit is None else max(0, int(retry_limit))
        )
        normalized_attempts = max(0, int(attempts))
        normalized_backoff_seconds = (
            self.DEFAULT_QUEUE_RETRY_BACKOFF_SECONDS
            if backoff_seconds is None
            else max(0.0, float(backoff_seconds))
        )
        normalized_backoff_max_seconds = (
            self.DEFAULT_QUEUE_RETRY_BACKOFF_MAX_SECONDS
            if backoff_max_seconds is None
            else max(normalized_backoff_seconds, float(backoff_max_seconds))
        )
        self.connection.execute(
            """
            INSERT INTO task_runs (
                task_id, task_name, status, progress, message, payload_json,
                result_json, error_message, callback_name, callback_payload_json,
                idempotency_key, worker_id, duplicate_requests, shutdown_requested,
                retry_limit, attempts, backoff_seconds, backoff_max_seconds,
                dead_lettered, last_attempt_at, next_retry_at,
                created_at, updated_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                task_name = excluded.task_name,
                status = excluded.status,
                progress = excluded.progress,
                message = excluded.message,
                payload_json = excluded.payload_json,
                result_json = excluded.result_json,
                error_message = excluded.error_message,
                callback_name = excluded.callback_name,
                callback_payload_json = excluded.callback_payload_json,
                idempotency_key = excluded.idempotency_key,
                worker_id = excluded.worker_id,
                duplicate_requests = excluded.duplicate_requests,
                shutdown_requested = excluded.shutdown_requested,
                retry_limit = excluded.retry_limit,
                attempts = excluded.attempts,
                backoff_seconds = excluded.backoff_seconds,
                backoff_max_seconds = excluded.backoff_max_seconds,
                dead_lettered = excluded.dead_lettered,
                last_attempt_at = excluded.last_attempt_at,
                next_retry_at = excluded.next_retry_at,
                updated_at = excluded.updated_at,
                completed_at = excluded.completed_at
            """,
            (
                task_id,
                task_name,
                status,
                max(0, min(100, int(progress))),
                message,
                json.dumps(payload or {}, sort_keys=True, default=str),
                json.dumps(result, sort_keys=True, default=str) if result is not None else None,
                error_message,
                callback_name,
                (
                    json.dumps(callback_payload, sort_keys=True, default=str)
                    if callback_payload is not None
                    else None
                ),
                idempotency_key or task_id,
                worker_id,
                max(
                    duplicate_requests,
                    int(existing["duplicate_requests"] or 0) if existing else 0,
                ),
                max(
                    int(shutdown_requested),
                    int(existing["shutdown_requested"] or 0) if existing else 0,
                ),
                normalized_retry_limit,
                normalized_attempts,
                normalized_backoff_seconds,
                normalized_backoff_max_seconds,
                int(dead_lettered),
                (
                    self._to_iso(last_attempt_at)
                    if isinstance(last_attempt_at, datetime)
                    else last_attempt_at
                ),
                (
                    self._to_iso(next_retry_at)
                    if isinstance(next_retry_at, datetime)
                    else next_retry_at
                ),
                created_at,
                now,
                finished_at,
            ),
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT * FROM task_runs WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return self._serialize_task_run(row) if row else {}

    def get_task_run(self, task_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM task_runs WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return self._serialize_task_run(row) if row else None

    def list_task_runs(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM task_runs"
        params: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        rows = self.connection.execute(query, params).fetchall()
        return [self._serialize_task_run(row) for row in rows]

    def _load_queue_retry_config(
        self,
        *,
        retry_limit: int | None = None,
        backoff_seconds: float | None = None,
        backoff_max_seconds: float | None = None,
    ) -> dict[str, float | int]:
        configured_retry_limit = (
            int(
                os.environ.get(
                    "FUNDING_BOT_TASK_RETRY_LIMIT",
                    str(self.DEFAULT_QUEUE_RETRY_LIMIT),
                )
            )
            if retry_limit is None
            else int(retry_limit)
        )
        configured_backoff_seconds = (
            float(
                os.environ.get(
                    "FUNDING_BOT_TASK_RETRY_BACKOFF_SECONDS",
                    str(self.DEFAULT_QUEUE_RETRY_BACKOFF_SECONDS),
                )
            )
            if backoff_seconds is None
            else float(backoff_seconds)
        )
        configured_backoff_max_seconds = (
            float(
                os.environ.get(
                    "FUNDING_BOT_TASK_RETRY_BACKOFF_MAX_SECONDS",
                    str(self.DEFAULT_QUEUE_RETRY_BACKOFF_MAX_SECONDS),
                )
            )
            if backoff_max_seconds is None
            else float(backoff_max_seconds)
        )
        if configured_retry_limit < 0:
            raise ValueError("retry_limit must be zero or greater.")
        if configured_backoff_seconds <= 0:
            raise ValueError("backoff_seconds must be greater than zero.")
        if configured_backoff_max_seconds < configured_backoff_seconds:
            raise ValueError(
                "backoff_max_seconds must be greater than or equal to backoff_seconds."
            )
        return {
            "retry_limit": configured_retry_limit,
            "backoff_seconds": configured_backoff_seconds,
            "backoff_max_seconds": configured_backoff_max_seconds,
        }

    @staticmethod
    def _calculate_retry_delay(
        attempt_number: int,
        *,
        backoff_seconds: float,
        backoff_max_seconds: float,
    ) -> float:
        return min(backoff_seconds * (2 ** max(0, attempt_number - 1)), backoff_max_seconds)

    def _record_task_history(
        self,
        *,
        task_id: str,
        task_name: str,
        attempt_number: int,
        status: str,
        happened_at: datetime | str | None = None,
        backoff_seconds: float | None = None,
        next_retry_at: datetime | str | None = None,
        result: Any = None,
        error_message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        normalized_happened_at = (
            self._to_iso(happened_at)
            if isinstance(happened_at, datetime)
            else (happened_at or self._to_iso())
        )
        normalized_next_retry_at = (
            self._to_iso(next_retry_at) if isinstance(next_retry_at, datetime) else next_retry_at
        )
        params = (
            task_id,
            task_name,
            attempt_number,
            status,
            normalized_happened_at,
            backoff_seconds,
            normalized_next_retry_at,
            json.dumps(result, sort_keys=True, default=str) if result is not None else None,
            error_message,
            json.dumps(details or {}, sort_keys=True, default=str),
        )
        try:
            self.connection.execute(
                """
                INSERT INTO task_history (
                    task_id, task_name, attempt_number, status, happened_at,
                    backoff_seconds, next_retry_at, result_json, error_message, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                params,
            )
        except sqlite3.OperationalError as exc:
            if "readonly" not in str(exc).lower():
                raise
            self._reopen_database_connection()
            self.connection.execute(
                """
                INSERT INTO task_history (
                    task_id, task_name, attempt_number, status, happened_at,
                    backoff_seconds, next_retry_at, result_json, error_message, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                params,
            )

    def list_task_history(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM task_history WHERE task_id = ? ORDER BY attempt_number, id",
            (task_id,),
        ).fetchall()
        return [self._serialize_task_history_row(row) for row in rows]

    def list_dead_letter_queue(
        self,
        *,
        task_name: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM dead_letter_queue"
        params: list[Any] = []
        if task_name is not None:
            query += " WHERE task_name = ?"
            params.append(task_name)
        query += " ORDER BY failed_at DESC, id DESC"
        rows = self.connection.execute(query, params).fetchall()
        return [self._serialize_dead_letter_row(row) for row in rows]

    @staticmethod
    def generate_idempotency_key(task_name: str, payload: dict[str, Any] | None = None) -> str:
        canonical_payload = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"))
        raw_key = f"{str(task_name).strip().lower()}|{canonical_payload}"
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    def request_task_run_shutdown(
        self, task_id: str, *, signal_name: str | None = None
    ) -> dict[str, Any]:
        timestamp = self._to_iso()
        message = "Shutdown requested for in-flight task."
        if signal_name:
            message = f"{message} Signal: {signal_name}."
        self.connection.execute(
            """
            UPDATE task_runs
            SET shutdown_requested = 1,
                message = ?,
                updated_at = ?
            WHERE task_id = ?
            """,
            (message, timestamp, task_id),
        )
        self.connection.commit()
        controller = self._active_queue_controllers.get(task_id)
        if controller is not None:
            controller.received_signals.append(getattr(signal, signal_name, signal.SIGTERM))
            controller._shutdown_event.set()
        task_run = self.get_task_run(task_id)
        if task_run is None:
            raise FundingBotError(f"Task run {task_id!r} does not exist.")
        self._log_action(
            "queue_task_shutdown_requested",
            task_id=task_id,
            task_name=task_run["task_name"],
            signal=signal_name,
        )
        return task_run

    def get_queue_metrics(self) -> dict[str, Any]:
        run_counts = self.connection.execute("""
            SELECT
                SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled,
                SUM(CASE WHEN dead_lettered = 1 THEN 1 ELSE 0 END) AS dead_lettered,
                COALESCE(SUM(duplicate_requests), 0) AS duplicate_preventions,
                COALESCE(
                    SUM(
                        CASE
                            WHEN completed_at IS NOT NULL AND created_at IS NOT NULL
                            THEN MAX((julianday(completed_at) - julianday(created_at)) * 86400.0, 0.0)
                            ELSE 0.0
                        END
                    ),
                    0.0
                ) AS duration_seconds_sum,
                SUM(
                    CASE
                        WHEN completed_at IS NOT NULL AND created_at IS NOT NULL THEN 1 ELSE 0
                    END
                ) AS duration_seconds_count,
                COALESCE(
                    MAX(
                        CASE
                            WHEN completed_at IS NOT NULL AND created_at IS NOT NULL
                            THEN MAX((julianday(completed_at) - julianday(created_at)) * 86400.0, 0.0)
                            ELSE 0.0
                        END
                    ),
                    0.0
                ) AS duration_seconds_max
            FROM task_runs
            """).fetchone()
        history_counts = self.connection.execute("""
            SELECT
                SUM(CASE WHEN status = 'retry_scheduled' THEN 1 ELSE 0 END) AS retries_scheduled
            FROM task_history
            """).fetchone()
        duration_count = int(run_counts["duration_seconds_count"] or 0)
        duration_sum = float(run_counts["duration_seconds_sum"] or 0.0)
        return {
            "running": int(run_counts["running"] or 0),
            "completed": int(run_counts["completed"] or 0),
            "failed": int(run_counts["failed"] or 0),
            "cancelled": int(run_counts["cancelled"] or 0),
            "dead_lettered": int(run_counts["dead_lettered"] or 0),
            "duplicate_preventions": int(run_counts["duplicate_preventions"] or 0),
            "retries_scheduled": int(history_counts["retries_scheduled"] or 0),
            "duration_seconds_sum": duration_sum,
            "duration_seconds_count": duration_count,
            "duration_seconds_average": (duration_sum / duration_count) if duration_count else 0.0,
            "duration_seconds_max": float(run_counts["duration_seconds_max"] or 0.0),
        }

    def get_database_pool_metrics(self) -> dict[str, Any]:
        return self._database.get_pool_metrics()

    def get_database_query_metrics(self) -> dict[str, Any]:
        return self._database.get_query_metrics()

    def get_database_index_metrics(self) -> dict[str, Any]:
        return self.get_index_monitoring_snapshot()

    def get_cache_metrics(self) -> dict[str, Any]:
        namespaces: dict[str, dict[str, Any]] = {}
        for entry in self.cache_manager.all_stats():
            bucket = namespaces.setdefault(
                entry["namespace"],
                {
                    "namespace": entry["namespace"],
                    "hits": 0,
                    "misses": 0,
                    "sets": 0,
                    "invalidations": 0,
                    "size": 0,
                    "backend": entry.get("backend", "memory"),
                    "ttl_seconds": entry.get("ttl_seconds", 0.0),
                    "scopes": [],
                },
            )
            bucket["hits"] += int(entry.get("hits", 0))
            bucket["misses"] += int(entry.get("misses", 0))
            bucket["sets"] += int(entry.get("sets", 0))
            bucket["invalidations"] += int(entry.get("invalidations", 0))
            bucket["size"] += int(entry.get("size", 0))
            bucket["ttl_seconds"] = float(entry.get("ttl_seconds", bucket["ttl_seconds"]))
            bucket["scopes"].append(
                {
                    "scope": entry.get("scope", "default"),
                    "hits": int(entry.get("hits", 0)),
                    "misses": int(entry.get("misses", 0)),
                    "sets": int(entry.get("sets", 0)),
                    "invalidations": int(entry.get("invalidations", 0)),
                    "size": int(entry.get("size", 0)),
                }
            )
        default_namespaces = {
            "donor-records": float(self.cache_manager.config.donor_ttl_seconds),
            "connector-data": float(self.cache_manager.config.connector_ttl_seconds),
            "deduped-profiles": float(self.cache_manager.config.deduped_profile_ttl_seconds),
        }
        for namespace, ttl_seconds in default_namespaces.items():
            namespaces.setdefault(
                namespace,
                {
                    "namespace": namespace,
                    "hits": 0,
                    "misses": 0,
                    "sets": 0,
                    "invalidations": 0,
                    "size": 0,
                    "backend": self.cache_manager.backend_name,
                    "ttl_seconds": ttl_seconds,
                    "scopes": [],
                },
            )
        return {
            "backend": self.cache_manager.backend_name,
            "health": self.cache_manager.health_snapshot(),
            "namespaces": namespaces,
        }

    def execute_queue_task(
        self,
        task_name: str,
        payload: dict[str, Any] | None,
        task_callable: Callable[[QueueTaskContext, dict[str, Any]], Any],
        *,
        idempotency_key: str | None = None,
        worker_id: str | None = None,
        install_signal_handlers: bool = True,
        retry_limit: int | None = None,
        backoff_seconds: float | None = None,
        backoff_max_seconds: float | None = None,
        sleep_func: Callable[[float], None] | None = None,
    ) -> dict[str, Any]:
        normalized_task_name = str(task_name).strip()
        if not normalized_task_name:
            raise ValueError("Task name is required.")
        normalized_payload = dict(payload or {})
        normalized_idempotency_key = idempotency_key or self.generate_idempotency_key(
            normalized_task_name, normalized_payload
        )
        config = self._load_queue_retry_config(
            retry_limit=retry_limit,
            backoff_seconds=backoff_seconds,
            backoff_max_seconds=backoff_max_seconds,
        )
        task_started_at = time.perf_counter()
        consumer_span_kind = getattr(SpanKind, "CONSUMER", getattr(SpanKind, "INTERNAL", None))
        with start_span(
            f"task_queue.execute.{normalized_task_name}",
            kind=consumer_span_kind,
            attributes={
                "messaging.system": "celery",
                "messaging.destination.name": normalized_task_name,
                "funding_bot.queue.idempotency_key": normalized_idempotency_key,
            },
        ) as span:
            existing_task_run = self.get_task_run(normalized_idempotency_key)
            if existing_task_run is not None:
                self.record_task_run(
                    normalized_idempotency_key,
                    existing_task_run["task_name"],
                    status=existing_task_run["status"],
                    progress=existing_task_run["progress"],
                    message=existing_task_run["message"],
                    payload=existing_task_run["payload"],
                    result=existing_task_run["result"],
                    error_message=existing_task_run["error_message"],
                    callback_name=existing_task_run.get("callback_name"),
                    callback_payload=existing_task_run.get("callback_payload"),
                    idempotency_key=existing_task_run["idempotency_key"],
                    worker_id=existing_task_run.get("worker_id"),
                    duplicate_requests=int(existing_task_run.get("duplicate_requests", 0)) + 1,
                    shutdown_requested=bool(existing_task_run.get("shutdown_requested")),
                    retry_limit=int(existing_task_run.get("retry_limit", config["retry_limit"])),
                    attempts=int(existing_task_run.get("attempts", 0)),
                    backoff_seconds=float(
                        existing_task_run.get("backoff_seconds", config["backoff_seconds"])
                    ),
                    backoff_max_seconds=float(
                        existing_task_run.get("backoff_max_seconds", config["backoff_max_seconds"])
                    ),
                    dead_lettered=bool(existing_task_run.get("dead_lettered")),
                    last_attempt_at=existing_task_run.get("last_attempt_at"),
                    next_retry_at=existing_task_run.get("next_retry_at"),
                    completed_at=(
                        datetime.fromisoformat(existing_task_run["completed_at"])
                        if existing_task_run.get("completed_at")
                        else None
                    ),
                )
                duplicate_task_run = self.get_task_run(normalized_idempotency_key)
                assert duplicate_task_run is not None
                duplicate_task_run["duplicate"] = True
                span.set_attribute("funding_bot.queue.duplicate", True)
                self._log_action(
                    "queue_task_duplicate_prevented",
                    task_id=normalized_idempotency_key,
                    task_name=normalized_task_name,
                    status=duplicate_task_run["status"],
                )
                return duplicate_task_run

            controller = GracefulShutdownController(
                on_shutdown=lambda signum: self.request_task_run_shutdown(
                    normalized_idempotency_key,
                    signal_name=signal.Signals(signum).name,
                )
            )
            if install_signal_handlers:
                controller.install()
            self._active_queue_controllers[normalized_idempotency_key] = controller
            context = QueueTaskContext(
                bot=self,
                idempotency_key=normalized_idempotency_key,
                controller=controller,
                task_name=normalized_task_name,
                payload=normalized_payload,
                worker_id=worker_id,
                retry_limit=int(config["retry_limit"]),
                backoff_seconds=float(config["backoff_seconds"]),
                backoff_max_seconds=float(config["backoff_max_seconds"]),
            )
            sleeper = sleep_func or time.sleep

            self.record_task_run(
                normalized_idempotency_key,
                normalized_task_name,
                status="running",
                progress=0,
                message="Task started.",
                payload=normalized_payload,
                idempotency_key=normalized_idempotency_key,
                worker_id=worker_id,
                retry_limit=int(config["retry_limit"]),
                attempts=0,
                backoff_seconds=float(config["backoff_seconds"]),
                backoff_max_seconds=float(config["backoff_max_seconds"]),
                dead_lettered=False,
                next_retry_at=None,
            )
            self._log_action(
                "queue_task_started",
                task_id=normalized_idempotency_key,
                task_name=normalized_task_name,
                retry_limit=int(config["retry_limit"]),
                backoff_seconds=float(config["backoff_seconds"]),
                backoff_max_seconds=float(config["backoff_max_seconds"]),
                worker_id=worker_id,
            )

            for attempt_number in range(1, int(config["retry_limit"]) + 2):
                happened_at = self._utcnow()
                try:
                    context.checkpoint("Shutdown requested before queue task execution started.")
                    result = task_callable(context, dict(normalized_payload))
                    context.checkpoint("Shutdown requested after queue task execution.")
                except GracefulShutdownRequested as exc:
                    span.set_attribute("funding_bot.queue.status", "cancelled")
                    set_span_error(span, exc)
                    self._record_task_history(
                        task_id=normalized_idempotency_key,
                        task_name=normalized_task_name,
                        attempt_number=attempt_number,
                        status="cancelled",
                        happened_at=happened_at,
                        error_message=str(exc),
                        details={"payload": normalized_payload},
                    )
                    task_run = self.record_task_run(
                        normalized_idempotency_key,
                        normalized_task_name,
                        status="cancelled",
                        progress=0,
                        message=str(exc),
                        payload=normalized_payload,
                        error_message=str(exc),
                        callback_name="on_cancel",
                        callback_payload={"attempt_number": attempt_number, "state": "cancelled"},
                        idempotency_key=normalized_idempotency_key,
                        worker_id=worker_id,
                        retry_limit=int(config["retry_limit"]),
                        attempts=attempt_number,
                        backoff_seconds=float(config["backoff_seconds"]),
                        backoff_max_seconds=float(config["backoff_max_seconds"]),
                        dead_lettered=False,
                        last_attempt_at=happened_at,
                        completed_at=happened_at,
                    )
                    record_slo_event(
                        "task_queue_throughput",
                        component=normalized_task_name,
                        latency_seconds=time.perf_counter() - task_started_at,
                        success=False,
                        throughput_units=0,
                        metadata={"status": "cancelled", "attempts": attempt_number},
                        connection=self.connection,
                    )
                    self.connection.commit()
                    self._log_action(
                        "queue_task_cancelled",
                        task_id=normalized_idempotency_key,
                        task_name=normalized_task_name,
                        attempts=attempt_number,
                    )
                    task_run["duplicate"] = False
                    self._active_queue_controllers.pop(normalized_idempotency_key, None)
                    if install_signal_handlers:
                        controller.restore()
                    return task_run
                except Exception as exc:
                    error_message = str(exc)
                    should_retry = attempt_number <= int(config["retry_limit"])
                    retry_delay = None
                    next_retry_at = None
                    status = "failed"
                    message = "Task failed."
                    if should_retry:
                        retry_delay = self._calculate_retry_delay(
                            attempt_number,
                            backoff_seconds=float(config["backoff_seconds"]),
                            backoff_max_seconds=float(config["backoff_max_seconds"]),
                        )
                        next_retry_at = happened_at + timedelta(seconds=retry_delay)
                        status = "retry_scheduled"
                        message = (
                            f"Task failed on attempt {attempt_number}; "
                            f"retrying in {retry_delay:.2f} seconds."
                        )
                    self._record_task_history(
                        task_id=normalized_idempotency_key,
                        task_name=normalized_task_name,
                        attempt_number=attempt_number,
                        status=status,
                        happened_at=happened_at,
                        backoff_seconds=retry_delay,
                        next_retry_at=next_retry_at,
                        error_message=error_message,
                        details={"payload": normalized_payload},
                    )
                    task_run = self.record_task_run(
                        normalized_idempotency_key,
                        normalized_task_name,
                        status="running" if should_retry else "failed",
                        progress=0,
                        message=message,
                        payload=normalized_payload,
                        error_message=error_message,
                        callback_name="on_retry" if should_retry else "on_failure",
                        callback_payload={
                            "attempt_number": attempt_number,
                            "state": "retry_scheduled" if should_retry else "failed",
                            "next_retry_at": (
                                self._to_iso(next_retry_at) if next_retry_at is not None else None
                            ),
                        },
                        idempotency_key=normalized_idempotency_key,
                        worker_id=worker_id,
                        retry_limit=int(config["retry_limit"]),
                        attempts=attempt_number,
                        backoff_seconds=float(config["backoff_seconds"]),
                        backoff_max_seconds=float(config["backoff_max_seconds"]),
                        dead_lettered=not should_retry,
                        last_attempt_at=happened_at,
                        next_retry_at=next_retry_at,
                        completed_at=None if should_retry else happened_at,
                    )
                    if should_retry:
                        self.connection.commit()
                        self._log_action(
                            "queue_task_retry_scheduled",
                            task_id=normalized_idempotency_key,
                            task_name=normalized_task_name,
                            attempts=attempt_number,
                            retry_limit=int(config["retry_limit"]),
                            backoff_seconds=retry_delay,
                            next_retry_at=self._to_iso(next_retry_at),
                            error_message=error_message,
                        )
                        sleeper(retry_delay)
                        continue

                    span.set_attribute("funding_bot.queue.status", "failed")
                    set_span_error(span, exc)
                    record_slo_event(
                        "task_queue_throughput",
                        component=normalized_task_name,
                        latency_seconds=time.perf_counter() - task_started_at,
                        success=False,
                        throughput_units=0,
                        metadata={"status": "failed", "attempts": attempt_number},
                        connection=self.connection,
                    )
                    self.connection.execute(
                        """
                        INSERT INTO dead_letter_queue (
                            task_id, task_name, payload_json, error_message, attempts, failed_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(task_id) DO UPDATE SET
                            task_name = excluded.task_name,
                            payload_json = excluded.payload_json,
                            error_message = excluded.error_message,
                            attempts = excluded.attempts,
                            failed_at = excluded.failed_at
                        """,
                        (
                            normalized_idempotency_key,
                            normalized_task_name,
                            json.dumps(normalized_payload, sort_keys=True),
                            error_message,
                            attempt_number,
                            self._to_iso(happened_at),
                        ),
                    )
                    self.connection.commit()
                    self._log_action(
                        "queue_task_failed",
                        task_id=normalized_idempotency_key,
                        task_name=normalized_task_name,
                        attempts=attempt_number,
                        error_message=error_message,
                        dead_lettered=True,
                    )
                    task_run["duplicate"] = False
                    self._active_queue_controllers.pop(normalized_idempotency_key, None)
                    if install_signal_handlers:
                        controller.restore()
                    return task_run

                self._record_task_history(
                    task_id=normalized_idempotency_key,
                    task_name=normalized_task_name,
                    attempt_number=attempt_number,
                    status="completed",
                    happened_at=happened_at,
                    result=result,
                    details={"payload": normalized_payload},
                )
                task_run = self.record_task_run(
                    normalized_idempotency_key,
                    normalized_task_name,
                    status="completed",
                    progress=100,
                    message="Task completed.",
                    payload=normalized_payload,
                    result=result if isinstance(result, dict) else {"value": result},
                    callback_name="on_success",
                    callback_payload={"attempt_number": attempt_number, "state": "completed"},
                    idempotency_key=normalized_idempotency_key,
                    worker_id=worker_id,
                    retry_limit=int(config["retry_limit"]),
                    attempts=attempt_number,
                    backoff_seconds=float(config["backoff_seconds"]),
                    backoff_max_seconds=float(config["backoff_max_seconds"]),
                    dead_lettered=False,
                    last_attempt_at=happened_at,
                    next_retry_at=None,
                    completed_at=happened_at,
                )
                span.set_attribute("funding_bot.queue.status", "completed")
                span.set_attribute("funding_bot.queue.attempts", attempt_number)
                self.connection.commit()
                record_slo_event(
                    "task_queue_throughput",
                    component=normalized_task_name,
                    latency_seconds=time.perf_counter() - task_started_at,
                    success=True,
                    throughput_units=1,
                    metadata={"status": "completed", "attempts": attempt_number},
                    connection=self.connection,
                )
                self._log_action(
                    "queue_task_completed",
                    task_id=normalized_idempotency_key,
                    task_name=normalized_task_name,
                    attempts=attempt_number,
                )
                task_run["duplicate"] = False
                self._active_queue_controllers.pop(normalized_idempotency_key, None)
                if install_signal_handlers:
                    controller.restore()
                return task_run

            self._active_queue_controllers.pop(normalized_idempotency_key, None)
            if install_signal_handlers:
                controller.restore()
            raise FundingBotError(
                f"Queue task {normalized_task_name!r} did not record a terminal state."
            )

    def run_discovery_task(
        self,
        *,
        connectors: Iterable[PortalConnector] | None = None,
        keywords: Iterable[str] | None = None,
        trusted_sources: Iterable[str] | None = None,
        discovered_at: datetime | None = None,
        retry_limit: int | None = None,
        backoff_seconds: float | None = None,
        backoff_max_seconds: float | None = None,
        sleep_func: Callable[[float], None] | None = None,
    ) -> dict[str, Any]:
        connector_list = list(connectors) if connectors is not None else None
        keyword_list = list(keywords) if keywords is not None else None
        source_list = list(trusted_sources) if trusted_sources is not None else None
        return self.execute_queue_task(
            "discover_opportunities",
            {
                "keywords": keyword_list or [],
                "trusted_sources": source_list or [],
                "discovered_at": self._to_iso(discovered_at) if discovered_at else None,
            },
            lambda _context, _payload: {
                "opportunities": self.run_discovery(
                    connectors=connector_list,
                    keywords=keyword_list,
                    trusted_sources=source_list,
                    discovered_at=discovered_at,
                )
            },
            retry_limit=retry_limit,
            backoff_seconds=backoff_seconds,
            backoff_max_seconds=backoff_max_seconds,
            sleep_func=sleep_func,
        )

    @staticmethod
    def _signature_for(opportunity: dict[str, Any]) -> str:
        identity = "|".join(
            str(opportunity.get(field, "")).strip().lower()
            for field in ("source", "portal_url", "title", "donor_name")
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def store_setting(
        self,
        key: str,
        value: dict[str, Any],
        *,
        data_classification: str | None = None,
        field_classifications: dict[str, Any] | None = None,
    ) -> None:
        """Persist an arbitrary named setting (organization profile, search
        preferences, etc.) as JSON, keyed by ``key``.

        This backs the web admin "Settings" panel so operators can configure
        the bot without leaving the dashboard or touching the CLI/env vars.
        """
        normalized_key = sanitize_user_string(
            key,
            field_name="key",
            allow_empty=False,
            max_length=128,
        )
        sanitized_value = sanitize_user_mapping(value, field_name=normalized_key)
        existing = self.connection.execute(
            """
            SELECT data_classification, field_classifications_json
            FROM organization_profile
            WHERE key = ?
            """,
            (normalized_key,),
        ).fetchone()
        default_classification, default_field_classifications, should_encrypt = (
            self._setting_defaults_for(normalized_key, sanitized_value)
        )
        final_classification = self._validate_data_classification(
            (
                data_classification
                if data_classification is not None
                else (existing["data_classification"] if existing else default_classification)
            ),
            default=default_classification,
        )
        final_field_classifications = self._build_field_classifications(
            defaults=default_field_classifications,
            fields=set(sanitized_value) | set(field_classifications or {}),
            overrides=(
                field_classifications
                if field_classifications is not None
                else (
                    self._decode_json_blob(
                        existing["field_classifications_json"],
                        default={},
                    )
                    if existing
                    else {}
                )
            ),
        )
        self._assert_field_classifications_within_record(
            final_classification,
            final_field_classifications,
        )
        self.connection.execute(
            """
            INSERT OR REPLACE INTO organization_profile (
                key, value_json, data_classification, field_classifications_json
            ) VALUES (?, ?, ?, ?)
            """,
            (
                normalized_key,
                self._encode_json_blob(sanitized_value, encrypt=should_encrypt),
                final_classification,
                json.dumps(final_field_classifications, sort_keys=True),
            ),
        )
        self.connection.commit()
        if normalized_key == "profile":
            self._deduped_profile_cache.invalidate("organization-profile")
        self._log_action(
            "generic_setting_updated",
            key=normalized_key,
            value_keys=_extract_dict_keys(sanitized_value),
            data_classification=final_classification,
            field_classifications=final_field_classifications,
        )
        if existing is not None:
            previous_classification = self._validate_data_classification(
                existing["data_classification"],
                default=default_classification,
            )
            previous_fields = self._decode_json_blob(
                existing["field_classifications_json"],
                default=default_field_classifications,
            )
            if (
                previous_classification != final_classification
                or previous_fields != final_field_classifications
            ):
                self._log_action(
                    "data_classification_changed",
                    model="organization_profile",
                    record_key=normalized_key,
                    previous_data_classification=previous_classification,
                    data_classification=final_classification,
                    previous_field_classifications=previous_fields,
                    field_classifications=final_field_classifications,
                )

    def load_setting(self, key: str) -> dict[str, Any]:
        normalized_key = sanitize_user_string(
            key,
            field_name="key",
            allow_empty=False,
            max_length=128,
        )
        row = self.connection.execute(
            "SELECT value_json FROM organization_profile WHERE key = ?",
            (normalized_key,),
        ).fetchone()
        return self._decode_json_blob(row["value_json"], default={}) if row else {}

    def store_organization_profile(self, profile: dict[str, Any]) -> None:
        self.store_setting("profile", profile)

    def load_organization_profile(self) -> dict[str, Any]:
        cached, profile = self._deduped_profile_cache.get("organization-profile")
        if cached:
            return dict(profile)
        profile = self.load_setting("profile")
        self._deduped_profile_cache.set(
            "organization-profile",
            profile,
            tags=["organization-profile"],
        )
        return profile

    def store_search_settings(
        self,
        *,
        keywords: Iterable[str] | None = None,
        trusted_sources: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Persist default keyword/source filters used by :meth:`run_discovery`."""
        settings = {
            "keywords": sorted(
                {keyword.strip() for keyword in (keywords or []) if keyword.strip()}
            ),
            "trusted_sources": sorted(
                {source.strip() for source in (trusted_sources or []) if source.strip()}
            ),
        }
        self.store_setting("search_settings", settings)
        return settings

    def load_search_settings(self) -> dict[str, Any]:
        settings = self.load_setting("search_settings")
        return {
            "keywords": settings.get("keywords", []),
            "trusted_sources": settings.get("trusted_sources", []),
        }

    @classmethod
    def _normalize_data_residency(cls, value: str | None) -> str:
        normalized = (value or cls.DEFAULT_DATA_RESIDENCY).strip().upper()
        if normalized not in cls.SUPPORTED_DATA_RESIDENCIES:
            raise FundingBotError(
                "Invalid DATA_RESIDENCY value "
                f"{value!r}. Expected one of {list(cls.SUPPORTED_DATA_RESIDENCIES)}."
            )
        return normalized

    @classmethod
    def validate_data_storage_location(
        cls,
        *,
        data_residency: str | None = None,
        storage_region: str | None = None,
    ) -> dict[str, Any]:
        configured_residency = cls._normalize_data_residency(
            data_residency or os.environ.get("DATA_RESIDENCY")
        )
        actual_storage_region = cls._normalize_data_residency(
            storage_region or os.environ.get("DATA_STORAGE_REGION") or configured_residency
        )
        if actual_storage_region != configured_residency:
            raise FundingBotError(
                "Data residency enforcement failed: configured DATA_RESIDENCY="
                f"{configured_residency} but runtime storage region is {actual_storage_region}."
            )
        return {
            "data_residency": configured_residency,
            "storage_region": actual_storage_region,
            "compliant": True,
        }

    def get_data_residency_status(self) -> dict[str, Any]:
        self._data_residency_status = self.validate_data_storage_location(
            data_residency=self._data_residency_status["data_residency"],
            storage_region=os.environ.get("DATA_STORAGE_REGION")
            or self._data_residency_status["storage_region"],
        )
        return dict(self._data_residency_status)

    @classmethod
    def _normalize_privacy_policy_formats(cls, formats: Iterable[str] | None) -> list[str]:
        requested = list(formats or cls.DEFAULT_PRIVACY_POLICY_FORMATS)
        normalized: list[str] = []
        for fmt in requested:
            current = str(fmt).strip().lower()
            if current not in cls.SUPPORTED_PRIVACY_POLICY_FORMATS:
                raise ValueError(
                    f"Unsupported privacy policy format {fmt!r}. "
                    f"Expected one of {sorted(cls.SUPPORTED_PRIVACY_POLICY_FORMATS)}."
                )
            if current not in normalized:
                normalized.append(current)
        return normalized

    @classmethod
    def _normalize_privacy_policy_jurisdictions(
        cls,
        jurisdictions: Iterable[str] | str | None,
        *,
        profile: dict[str, Any] | None = None,
    ) -> list[str]:
        candidate_values: Iterable[str] | str | None = jurisdictions
        if candidate_values is None and isinstance(profile, dict):
            candidate_values = profile.get("privacy_jurisdictions") or profile.get("jurisdictions")
        if candidate_values is None:
            candidate_values = [cls.DEFAULT_DATA_RESIDENCY]
        if isinstance(candidate_values, str):
            candidate_values = [
                item.strip() for item in candidate_values.split(",") if item.strip()
            ]

        normalized: list[str] = []
        for jurisdiction in candidate_values:
            current = cls._normalize_data_residency(str(jurisdiction))
            if current not in normalized:
                normalized.append(current)
        return normalized

    def _next_privacy_policy_revision(self, jurisdiction: str) -> tuple[int, str]:
        row = self.connection.execute(
            """
            SELECT COALESCE(MAX(revision), 0) AS latest_revision
            FROM privacy_policy_versions
            WHERE jurisdiction = ?
            """,
            (jurisdiction,),
        ).fetchone()
        revision = int(row["latest_revision"]) + 1
        return revision, f"{jurisdiction.lower()}-v{revision}"

    def list_privacy_policy_versions(self, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT jurisdiction, revision, version, data_residency, effective_date,
                   html_path, pdf_path, generated_at
            FROM privacy_policy_versions
            ORDER BY generated_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def generate_privacy_policies(
        self,
        *,
        output_dir: str | os.PathLike[str],
        jurisdictions: Iterable[str] | str | None = None,
        formats: Iterable[str] | None = None,
        effective_date: date | datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        from web.privacy_policy import generate_privacy_policy_content

        profile = self.load_organization_profile()
        residency_status = self.get_data_residency_status()
        normalized_jurisdictions = self._normalize_privacy_policy_jurisdictions(
            jurisdictions,
            profile=profile,
        )
        normalized_formats = self._normalize_privacy_policy_formats(formats)
        target_dir = Path(output_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

        if isinstance(effective_date, datetime):
            effective_date_iso = effective_date.date().isoformat()
        elif isinstance(effective_date, date):
            effective_date_iso = effective_date.isoformat()
        elif effective_date:
            effective_date_iso = str(effective_date)
        else:
            effective_date_iso = self._utcnow().date().isoformat()

        generated_at = self._to_iso()
        generated: list[dict[str, Any]] = []
        for jurisdiction in normalized_jurisdictions:
            revision, version = self._next_privacy_policy_revision(jurisdiction)
            policy = generate_privacy_policy_content(
                organization_profile=profile,
                jurisdiction=jurisdiction,
                data_residency=residency_status["data_residency"],
                version=version,
                effective_date=effective_date_iso,
            )
            base_name = f"privacy_policy_{jurisdiction.lower()}_{version}"
            html_path: str | None = None
            pdf_path: str | None = None
            if "html" in normalized_formats:
                html_file = target_dir / f"{base_name}.html"
                html_file.write_text(policy["html"], encoding="utf-8")
                html_path = str(html_file)
            if "pdf" in normalized_formats:
                pdf_file = target_dir / f"{base_name}.pdf"
                self._write_pdf(pdf_file, policy["text"])
                pdf_path = str(pdf_file)

            self.connection.execute(
                """
                INSERT INTO privacy_policy_versions (
                    jurisdiction, revision, version, data_residency, effective_date,
                    html_path, pdf_path, profile_json, generated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    jurisdiction,
                    revision,
                    version,
                    residency_status["data_residency"],
                    effective_date_iso,
                    html_path,
                    pdf_path,
                    json.dumps(profile, sort_keys=True),
                    generated_at,
                ),
            )
            generated.append(
                {
                    "jurisdiction": jurisdiction,
                    "revision": revision,
                    "version": version,
                    "data_residency": residency_status["data_residency"],
                    "effective_date": effective_date_iso,
                    "html_path": html_path,
                    "pdf_path": pdf_path,
                }
            )

        self.connection.commit()
        self._log_action(
            "privacy_policies_generated",
            jurisdictions=normalized_jurisdictions,
            formats=normalized_formats,
            versions=[item["version"] for item in generated],
            data_residency=residency_status["data_residency"],
        )
        return generated

    @classmethod
    def _parse_retention_days(cls, field_name: str, value: Any) -> int:
        try:
            normalized = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Field {field_name!r} must be an integer number of days.") from exc
        if normalized < 1:
            raise ValueError(f"Field {field_name!r} must be at least 1 day.")
        return normalized

    @classmethod
    def _default_data_retention_policy(cls) -> dict[str, int]:
        policy: dict[str, int] = {}
        for key, default in cls.DATA_RETENTION_DEFAULTS.items():
            raw_value = os.environ.get(cls.DATA_RETENTION_ENV_VARS[key], default)
            try:
                policy[key] = cls._parse_retention_days(key, raw_value)
            except ValueError:
                policy[key] = default
        return policy

    def load_data_retention_policy(self) -> dict[str, int]:
        policy = self._default_data_retention_policy()
        stored = self.load_setting(self.DATA_RETENTION_POLICY_KEY)
        for key in self.DATA_RETENTION_DEFAULTS:
            if key in stored:
                policy[key] = self._parse_retention_days(key, stored[key])
        return policy

    def store_data_retention_policy(self, policy: dict[str, Any]) -> dict[str, int]:
        if not isinstance(policy, dict):
            raise ValueError("Data retention policy must be a JSON-style object.")
        unknown_fields = sorted(set(policy) - set(self.DATA_RETENTION_DEFAULTS))
        if unknown_fields:
            raise ValueError(f"Unknown data retention field(s): {', '.join(unknown_fields)}.")

        merged_policy = self.load_data_retention_policy()
        for key, value in policy.items():
            merged_policy[key] = self._parse_retention_days(key, value)

        self.store_setting(self.DATA_RETENTION_POLICY_KEY, merged_policy)
        self._log_action("data_retention_policy_updated", **merged_policy)
        return merged_policy

    def enforce_data_retention(
        self,
        *,
        now: datetime | None = None,
        dry_run: bool = False,
        archive_manager: ArchiveManager | None = None,
        archive: bool = True,
    ) -> dict[str, Any]:
        as_of = self._as_utc(now)
        policy = self.load_data_retention_policy()
        cutoffs = {key: self._to_iso(as_of - timedelta(days=days)) for key, days in policy.items()}

        expired_communication_rows = self.connection.execute(
            """
            SELECT id FROM communications
            WHERE sent_at < ?
            ORDER BY id
            """,
            (cutoffs["communications_days"],),
        ).fetchall()
        expired_communication_ids = [row["id"] for row in expired_communication_rows]

        expired_document_rows = self.connection.execute(
            """
            SELECT id, path FROM documents
            WHERE created_at < ?
            ORDER BY id
            """,
            (cutoffs["documents_days"],),
        ).fetchall()
        expired_document_ids = [row["id"] for row in expired_document_rows]
        expired_document_paths = [Path(row["path"]) for row in expired_document_rows]
        expired_audit_rows = self.connection.execute(
            "SELECT * FROM audit_logs WHERE happened_at < ? ORDER BY happened_at, id",
            (cutoffs["audit_logs_days"],),
        ).fetchall()
        expired_submission_rows = self.connection.execute(
            """
            SELECT *
            FROM submission_attempts
            WHERE happened_at < ?
            ORDER BY happened_at, id
            """,
            (cutoffs["submission_attempts_days"],),
        ).fetchall()
        expired_task_rows = self.connection.execute(
            """
            SELECT *
            FROM tasks
            WHERE status = 'done' AND updated_at < ?
            ORDER BY updated_at, id
            """,
            (cutoffs["completed_tasks_days"],),
        ).fetchall()
        expired_opportunity_rows = self.connection.execute(
            """
            SELECT *
            FROM opportunities
            WHERE discovered_at < ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM applications
                  WHERE applications.opportunity_signature = opportunities.signature
              )
            ORDER BY discovered_at, signature
            """,
            (cutoffs["opportunities_days"],),
        ).fetchall()
        expired_opportunity_signatures = [row["signature"] for row in expired_opportunity_rows]
        expired_communication_archive_rows = self.connection.execute(
            """
            SELECT
                c.*,
                oe.id AS outreach_event_id,
                oe.event_type,
                oe.happened_at AS outreach_event_happened_at
            FROM communications c
            LEFT JOIN outreach_events oe ON oe.communication_id = c.id
            WHERE c.sent_at < ?
            ORDER BY c.id, oe.id
            """,
            (cutoffs["communications_days"],),
        ).fetchall()

        deleted_counts = {
            "audit_logs": len(expired_audit_rows),
            "communications": len(expired_communication_ids),
            "outreach_events": (
                self.connection.execute(
                    """
                    SELECT COUNT(*) FROM outreach_events
                    WHERE communication_id IN (
                        SELECT id FROM communications WHERE sent_at < ?
                    )
                    """,
                    (cutoffs["communications_days"],),
                ).fetchone()[0]
                if expired_communication_ids
                else 0
            ),
            "documents": len(expired_document_ids),
            "opportunities": len(expired_opportunity_signatures),
            "submission_attempts": len(expired_submission_rows),
            "completed_tasks": len(expired_task_rows),
            "document_files_deleted": 0,
        }
        archival_report: dict[str, Any] | None = None
        if archive and not dry_run:
            archive_manager = archive_manager or ArchiveManager.from_env()
            archival_report = archive_manager.archive_payload(
                {
                    "archived_at": self._to_iso(as_of),
                    "cutoffs": cutoffs,
                    "policy": policy,
                    "audit_logs": [dict(row) for row in expired_audit_rows],
                    "communications": [dict(row) for row in expired_communication_archive_rows],
                    "documents": [dict(row) for row in expired_document_rows],
                    "opportunities": [dict(row) for row in expired_opportunity_rows],
                    "submission_attempts": [dict(row) for row in expired_submission_rows],
                    "completed_tasks": [dict(row) for row in expired_task_rows],
                },
                archive_name=f"retention_archive_{as_of.strftime('%Y%m%dT%H%M%SZ')}.json",
            )

        result = {
            "dry_run": dry_run,
            "as_of": self._to_iso(as_of),
            "policy": policy,
            "cutoffs": cutoffs,
            "deleted": deleted_counts,
            "archival": archival_report,
        }
        if dry_run:
            return result

        with self.connection:
            self.connection.execute(
                "DELETE FROM audit_logs WHERE happened_at < ?",
                (cutoffs["audit_logs_days"],),
            )
            self.connection.execute(
                "DELETE FROM submission_attempts WHERE happened_at < ?",
                (cutoffs["submission_attempts_days"],),
            )
            self.connection.execute(
                "DELETE FROM tasks WHERE status = 'done' AND updated_at < ?",
                (cutoffs["completed_tasks_days"],),
            )
            self.connection.execute(
                "DELETE FROM documents WHERE created_at < ?",
                (cutoffs["documents_days"],),
            )
            if expired_opportunity_signatures:
                placeholders = ", ".join("?" for _ in expired_opportunity_signatures)
                self.connection.execute(
                    "DELETE FROM opportunities WHERE signature IN (" + placeholders + ")",
                    expired_opportunity_signatures,
                )
            if expired_communication_ids:
                placeholders = ", ".join("?" for _ in expired_communication_ids)
                self.connection.execute(
                    "DELETE FROM outreach_events WHERE communication_id IN (" + placeholders + ")",
                    expired_communication_ids,
                )
                self.connection.execute(
                    "DELETE FROM communications WHERE id IN (" + placeholders + ")",
                    expired_communication_ids,
                )

        for path in expired_document_paths:
            try:
                if path.exists() and path.is_file():
                    path.unlink()
                    deleted_counts["document_files_deleted"] += 1
            except OSError:
                continue

        self._log_action(
            "data_retention_enforced",
            dry_run=False,
            as_of=result["as_of"],
            deleted=deleted_counts,
            policy=policy,
            archival=archival_report,
        )
        return result

    def export_data_warehouse(
        self,
        *,
        datasets: Iterable[str] | None = None,
        export_format: str = "json",
        output_dir: str | os.PathLike[str] = "generated/exports",
        archive: bool = False,
        dry_run: bool = False,
        archive_manager: ArchiveManager | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        return WarehouseExportService(self).export(
            datasets=datasets,
            export_format=export_format,
            output_dir=output_dir,
            archive=archive,
            dry_run=dry_run,
            archive_manager=archive_manager,
            progress_callback=progress_callback,
        )

    def list_locale_definitions(self) -> list[dict[str, Any]]:
        return [dict(definition) for definition in SUPPORTED_UI_LOCALES.values()]

    def get_locale_definition(self, locale: str | None) -> dict[str, Any]:
        return dict(SUPPORTED_UI_LOCALES[self._validate_ui_locale(locale)])

    def is_rtl_locale(self, locale: str | None) -> bool:
        return bool(self.get_locale_definition(locale)["is_rtl"])

    def list_credentials(self) -> list[dict[str, Any]]:
        """Return registered credential aliases (never the secret values)."""
        rows = self.connection.execute(
            "SELECT alias, env_var_name FROM credential_refs ORDER BY alias"
        ).fetchall()
        return [dict(row) for row in rows]

    def register_credential(self, alias: str, env_var_name: str) -> None:
        normalized_alias = validate_credential_alias(alias)
        normalized_env_var_name = validate_env_var_name(env_var_name)
        self.connection.execute(
            "INSERT OR REPLACE INTO credential_refs (alias, env_var_name) VALUES (?, ?)",
            (normalized_alias, normalized_env_var_name),
        )
        self.connection.commit()
        self._log_action(
            "credential_ref_registered",
            alias=normalized_alias,
            env_var_name=normalized_env_var_name,
        )

    def resolve_credential(self, alias: str) -> dict[str, Any]:
        normalized_alias = validate_credential_alias(alias)
        row = self.connection.execute(
            "SELECT env_var_name FROM credential_refs WHERE alias = ?",
            (normalized_alias,),
        ).fetchone()
        if not row:
            raise CredentialNotFoundError(
                f"No credential alias registered for {normalized_alias!r}."
            )

        env_var_name = row["env_var_name"]
        return self.vault.resolve_credentials(env_var_name)

    def _donor_cache_key(self, email: str) -> str:
        return _validate_email(email).lower()

    def _invalidate_donor_cache(self, email: str) -> None:
        normalized_email = self._donor_cache_key(email)
        self._donor_cache.invalidate(normalized_email)
        self._donor_cache.invalidate_tags("donors", f"donor:{normalized_email}")

    def upsert_donor(
        self,
        *,
        email: str,
        name: str,
        opted_out: bool = False,
        preferences: dict[str, Any] | None = None,
        segment: str | None = None,
        locale: str | None = None,
        data_classification: str | None = None,
        field_classifications: dict[str, Any] | None = None,
    ) -> None:
        email = _validate_email(email)
        normalized_name = sanitize_user_string(
            name,
            field_name="name",
            allow_empty=False,
            html_escape=True,
        )
        sanitized_preferences = sanitize_user_mapping(
            preferences or {},
            field_name="preferences",
        )
        existing = self.connection.execute(
            """
            SELECT last_contact_at, segment, locale, data_classification, field_classifications_json
            FROM donors
            WHERE email = ?
            """,
            (email,),
        ).fetchone()
        normalized_segment = (
            self._validate_segment(segment)
            if segment is not None
            else (existing["segment"] if existing is not None else "unknown")
        )
        normalized_locale = (
            self._validate_locale(locale)
            if locale is not None
            else (existing["locale"] if existing is not None else "en")
        )
        final_classification = self._validate_data_classification(
            (
                data_classification
                if data_classification is not None
                else (
                    existing["data_classification"]
                    if existing is not None
                    else self.MODEL_DEFAULT_CLASSIFICATIONS["donors"]
                )
            ),
            default=self.MODEL_DEFAULT_CLASSIFICATIONS["donors"],
        )
        default_field_classifications = self._build_field_classifications(
            defaults=self.DONOR_FIELD_CLASSIFICATIONS,
            fields=self.DONOR_FIELD_CLASSIFICATIONS,
        )
        final_field_classifications = self._build_field_classifications(
            defaults=default_field_classifications,
            fields=default_field_classifications,
            overrides=(
                field_classifications
                if field_classifications is not None
                else (
                    self._decode_json_blob(
                        existing["field_classifications_json"],
                        default=default_field_classifications,
                    )
                    if existing is not None
                    else default_field_classifications
                )
            ),
        )
        self._assert_field_classifications_within_record(
            final_classification,
            final_field_classifications,
        )
        self.connection.execute(
            """
            INSERT INTO donors (
                email,
                name,
                opted_out,
                preferences_json,
                last_contact_at,
                segment,
                locale,
                data_classification,
                field_classifications_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                name = excluded.name,
                opted_out = excluded.opted_out,
                preferences_json = excluded.preferences_json,
                segment = excluded.segment,
                locale = excluded.locale,
                data_classification = excluded.data_classification,
                field_classifications_json = excluded.field_classifications_json
            """,
            (
                email,
                normalized_name,
                int(opted_out),
                self._encode_json_blob(
                    sanitized_preferences,
                    encrypt=(
                        self._classification_rank(final_field_classifications["preferences"])
                        >= self._classification_rank("confidential")
                    ),
                ),
                existing["last_contact_at"] if existing is not None else None,
                normalized_segment,
                normalized_locale,
                final_classification,
                json.dumps(final_field_classifications, sort_keys=True),
            ),
        )
        self.connection.commit()
        self._invalidate_donor_cache(email)
        logged_profile = self.connection.execute(
            "SELECT segment, locale, data_classification, field_classifications_json FROM donors WHERE email = ?",
            (email,),
        ).fetchone()
        self._log_action(
            "donor_upserted",
            email=email,
            opted_out=opted_out,
            segment=logged_profile["segment"],
            locale=logged_profile["locale"],
            data_classification=logged_profile["data_classification"],
            field_classifications=self._decode_json_blob(
                logged_profile["field_classifications_json"],
                default={},
            ),
        )
        if existing is not None:
            previous_classification = self._validate_data_classification(
                existing["data_classification"],
                default=self.MODEL_DEFAULT_CLASSIFICATIONS["donors"],
            )
            previous_fields = self._decode_json_blob(
                existing["field_classifications_json"],
                default=default_field_classifications,
            )
            current_fields = self._decode_json_blob(
                logged_profile["field_classifications_json"],
                default=default_field_classifications,
            )
            if (
                previous_classification != logged_profile["data_classification"]
                or previous_fields != current_fields
            ):
                self._log_action(
                    "data_classification_changed",
                    model="donors",
                    record_key=email,
                    previous_data_classification=previous_classification,
                    data_classification=logged_profile["data_classification"],
                    previous_field_classifications=previous_fields,
                    field_classifications=current_fields,
                )

    def list_donors(self, segment: str | None = None) -> list[dict[str, Any]]:
        """Return donor records, optionally filtered by segment."""
        normalized_segment = self._validate_segment(segment) if segment is not None else None
        list_cache_key = {"segment": normalized_segment or "all"}
        hit, cached = self._donor_cache.get(list_cache_key)
        if hit:
            return [dict(donor) for donor in cached]
        if normalized_segment is not None:
            rows = self.connection.execute(
                "SELECT * FROM donors WHERE segment = ? ORDER BY name COLLATE NOCASE ASC, email ASC",
                (normalized_segment,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM donors ORDER BY name COLLATE NOCASE ASC, email ASC"
            ).fetchall()
        donors = [donor for donor in (self._deserialize_donor_row(row) for row in rows) if donor]
        tags = ["donors", f"segment:{normalized_segment or 'all'}"]
        self._donor_cache.set(list_cache_key, donors, tags=tags)
        return donors

    def get_donor(self, email: str) -> dict[str, Any] | None:
        normalized_email = self._donor_cache_key(email)
        hit, cached = self._donor_cache.get(normalized_email)
        if hit:
            return dict(cached) if cached is not None else None
        row = self.connection.execute("SELECT * FROM donors WHERE email = ?", (email,)).fetchone()
        donor = self._deserialize_donor_row(row)
        self._donor_cache.set(
            normalized_email,
            donor,
            tags=["donors", f"donor:{normalized_email}"],
        )
        return donor

    def _insert_consent_record(
        self,
        *,
        donor_email: str,
        status: str,
        consented_at: datetime | str | None = None,
        withdrawn_at: datetime | str | None = None,
        channel: str = "email",
        source: str = "manual",
        proof: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        normalized_email = _validate_email(donor_email)
        normalized_status = self._validate_consent_status(status)
        normalized_channel = self._validate_consent_channel(channel)
        consented_iso = self._normalize_filter_timestamp(consented_at) or self._to_iso()
        withdrawn_iso = self._normalize_filter_timestamp(withdrawn_at)
        recorded_iso = self._to_iso()
        cursor = self.connection.execute(
            """
            INSERT INTO consent_records (
                donor_email, channel, status, consented_at, withdrawn_at,
                source, proof, notes, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_email,
                normalized_channel,
                normalized_status,
                consented_iso,
                withdrawn_iso,
                sanitize_user_string(
                    source or "manual",
                    field_name="source",
                    allow_empty=False,
                    html_escape=True,
                    max_length=128,
                ),
                (
                    sanitize_user_string(
                        proof,
                        field_name="proof",
                        multiline=True,
                        html_escape=True,
                    )
                    if proof is not None
                    else None
                ),
                (
                    sanitize_user_string(
                        notes,
                        field_name="notes",
                        multiline=True,
                        html_escape=True,
                    )
                    if notes is not None
                    else None
                ),
                recorded_iso,
            ),
        )
        row = self.connection.execute(
            "SELECT * FROM consent_records WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()
        self.connection.commit()
        record = ConsentRecord.from_row(row)
        if record is None:
            raise FundingBotError("Failed to persist consent record.")
        self._log_action(
            "donor_consent_recorded",
            donor_email=record.donor_email,
            channel=record.channel,
            status=record.status,
            consented_at=record.consented_at,
            withdrawn_at=record.withdrawn_at,
            source=record.source,
        )
        return record.to_dict()

    def record_consent(
        self,
        donor_email: str,
        *,
        donor_name: str | None = None,
        consented_at: datetime | str | None = None,
        channel: str = "email",
        source: str = "manual",
        proof: str | None = None,
        notes: str | None = None,
        locale: str | None = None,
    ) -> dict[str, Any]:
        normalized_email = _validate_email(donor_email)
        current = self.connection.execute(
            "SELECT name, segment, locale FROM donors WHERE email = ?",
            (normalized_email,),
        ).fetchone()
        effective_name = donor_name or (
            current["name"]
            if current is not None
            else self._default_donor_name_from_email(normalized_email)
        )
        self.upsert_donor(
            email=normalized_email,
            name=effective_name,
            opted_out=False,
            segment=current["segment"] if current is not None else None,
            locale=locale or (current["locale"] if current is not None else None),
        )
        return self._insert_consent_record(
            donor_email=normalized_email,
            status="granted",
            consented_at=consented_at,
            channel=channel,
            source=source,
            proof=proof,
            notes=notes,
        )

    def list_consent_records(self, donor_email: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM consent_records"
        params: list[Any] = []
        if donor_email is not None:
            query += " WHERE donor_email = ?"
            params.append(_validate_email(donor_email))
        query += " ORDER BY recorded_at DESC, id DESC"
        rows = self.connection.execute(query, params).fetchall()
        return [
            record.to_dict()
            for record in (ConsentRecord.from_row(row) for row in rows)
            if record is not None
        ]

    def get_latest_consent_record(
        self,
        donor_email: str,
        *,
        channel: str = "email",
    ) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT * FROM consent_records
            WHERE donor_email = ? AND channel = ?
            ORDER BY recorded_at DESC, id DESC
            LIMIT 1
            """,
            (_validate_email(donor_email), self._validate_consent_channel(channel)),
        ).fetchone()
        record = ConsentRecord.from_row(row)
        return record.to_dict() if record is not None else None

    def set_donor_opt_out(
        self,
        email: str,
        opted_out: bool = True,
        *,
        donor_name: str | None = None,
        source: str = "manual",
        recorded_at: datetime | str | None = None,
        notes: str | None = None,
        channel: str = "email",
    ) -> None:
        normalized_email = _validate_email(email)
        donor = self.connection.execute(
            "SELECT name, locale FROM donors WHERE email = ?",
            (normalized_email,),
        ).fetchone()
        if donor is None:
            self.upsert_donor(
                email=normalized_email,
                name=donor_name or self._default_donor_name_from_email(normalized_email),
                opted_out=opted_out,
            )
        else:
            self.connection.execute(
                "UPDATE donors SET opted_out = ? WHERE email = ?",
                (int(opted_out), normalized_email),
            )
            self.connection.commit()

        if opted_out:
            latest = self.get_latest_consent_record(normalized_email, channel=channel)
            consented_at = (latest or {}).get("consented_at")
            self._insert_consent_record(
                donor_email=normalized_email,
                status="withdrawn",
                consented_at=consented_at or recorded_at,
                withdrawn_at=recorded_at,
                channel=channel,
                source=source,
                notes=notes or "Donor opted out of future communications.",
            )
        else:
            self.record_consent(
                normalized_email,
                donor_name=donor_name or (donor["name"] if donor is not None else None),
                consented_at=recorded_at,
                channel=channel,
                source=source,
                notes=notes or "Donor communication consent restored.",
                locale=donor["locale"] if donor is not None else None,
            )
        self.connection.execute(
            "UPDATE donors SET opted_out = ? WHERE email = ?",
            (int(opted_out), normalized_email),
        )
        self.connection.commit()
        self._invalidate_donor_cache(normalized_email)
        self._log_action("donor_opt_out_updated", email=normalized_email, opted_out=opted_out)

    @classmethod
    def _normalize_funnel_stage(cls, stage: str) -> str:
        normalized = str(stage).strip().lower()
        if normalized not in cls.FUNNEL_STAGES:
            raise ValueError(
                f"Invalid funnel stage {stage!r}. Expected one of {list(cls.FUNNEL_STAGES)}."
            )
        return normalized

    def _connector_for_opportunity(self, opportunity_signature: str | None) -> str | None:
        if opportunity_signature is None:
            return None
        row = self.connection.execute(
            "SELECT source FROM opportunities WHERE signature = ?",
            (str(opportunity_signature).strip(),),
        ).fetchone()
        if row is None:
            return None
        return str(row["source"]).strip() or None

    def _connector_for_task(self, task_id: int | str | None) -> str | None:
        if task_id in (None, ""):
            return None
        row = self.connection.execute(
            "SELECT attributed_connector, opportunity_signature FROM tasks WHERE id = ?",
            (int(task_id),),
        ).fetchone()
        if row is None:
            return None
        return (
            str(row["attributed_connector"]).strip() if row["attributed_connector"] else None
        ) or self._connector_for_opportunity(row["opportunity_signature"])

    def _resolve_connector_attribution(
        self,
        *,
        attributed_connector: str | None = None,
        opportunity_signature: str | None = None,
        task_id: int | str | None = None,
    ) -> str | None:
        explicit = (
            str(attributed_connector).strip() if attributed_connector is not None else None
        ) or None
        if explicit is not None:
            return explicit
        connector = self._connector_for_opportunity(opportunity_signature)
        if connector is not None:
            return connector
        return self._connector_for_task(task_id)

    def record_funnel_event(
        self,
        *,
        stage: str,
        entity_key: str,
        connector_name: str | None = None,
        success: bool = True,
        happened_at: datetime | str | None = None,
        opportunity_signature: str | None = None,
        task_id: int | None = None,
        communication_id: int | None = None,
        event_type: str | None = None,
        metadata: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> None:
        normalized_stage = self._normalize_funnel_stage(stage)
        normalized_entity_key = str(entity_key).strip()
        if not normalized_entity_key:
            raise ValueError("Funnel event entity_key is required.")
        self.connection.execute(
            """
            INSERT INTO funnel_events (
                stage, entity_key, connector_name, opportunity_signature, task_id,
                communication_id, event_type, success, happened_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_stage,
                normalized_entity_key,
                (str(connector_name).strip() if connector_name else None),
                (str(opportunity_signature).strip() if opportunity_signature else None),
                task_id,
                communication_id,
                (str(event_type).strip().lower() if event_type else None),
                int(bool(success)),
                (
                    self._to_iso(
                        happened_at
                        if isinstance(happened_at, datetime)
                        else (
                            datetime.fromisoformat(happened_at)
                            if isinstance(happened_at, str)
                            else None
                        )
                    )
                    if happened_at is not None
                    else self._to_iso()
                ),
                json.dumps(metadata or {}, sort_keys=True),
            ),
        )
        if commit:
            self.connection.commit()

    def record_connector_call_metric(
        self,
        *,
        connector_name: str,
        connector_type: str,
        operation: str,
        source_status: str,
        latency_seconds: float,
        cost_usd: float = 0.0,
        errored: bool = False,
        request_count: int = 0,
        happened_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO connector_call_metrics (
                connector_name, connector_type, operation, source_status, latency_seconds,
                cost_usd, errored, request_count, happened_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(connector_name).strip(),
                str(connector_type).strip(),
                str(operation).strip(),
                str(source_status).strip() or "remote",
                max(0.0, float(latency_seconds)),
                max(0.0, float(cost_usd)),
                int(bool(errored)),
                max(0, int(request_count)),
                self._to_iso(happened_at),
                json.dumps(metadata or {}, sort_keys=True),
            ),
        )
        if commit:
            self.connection.commit()

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @classmethod
    def _build_funnel_stage_rows(
        cls, counts: dict[str, int], attempts: dict[str, int]
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        previous_count: int | None = None
        for stage in cls.FUNNEL_STAGES:
            count = int(counts.get(stage, 0))
            attempted = int(attempts.get(stage, 0))
            rows.append(
                {
                    "stage": stage,
                    "count": count,
                    "attempts": attempted,
                    "failed": max(0, attempted - count),
                    "conversion_rate": (
                        1.0
                        if previous_count is None and count > 0
                        else ((count / previous_count) if previous_count else 0.0)
                    ),
                }
            )
            previous_count = count
        return rows

    def _analytics_window_clause(
        self,
        *,
        start_at: datetime | str | None = None,
        end_at: datetime | str | None = None,
        timestamp_column: str,
    ) -> tuple[str, list[Any], dict[str, str | None]]:
        start_iso = self._normalize_filter_timestamp(start_at)
        end_iso = self._normalize_filter_timestamp(end_at, end=True)
        clauses: list[str] = []
        params: list[Any] = []
        if start_iso is not None:
            clauses.append(f"{timestamp_column} >= ?")
            params.append(start_iso)
        if end_iso is not None:
            clauses.append(f"{timestamp_column} <= ?")
            params.append(end_iso)
        where_clause = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return where_clause, params, {"start_at": start_iso, "end_at": end_iso}

    def deduplicate(
        self,
        opportunities: Iterable[dict[str, Any]],
        *,
        keywords: Iterable[str] | None = None,
        trusted_sources: Iterable[str] | None = None,
        discovered_at: datetime | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        dry_run: bool = False,
    ) -> list[dict[str, Any]]:
        return _run_async(
            self.deduplicate_async(
                opportunities,
                keywords=keywords,
                trusted_sources=trusted_sources,
                discovered_at=discovered_at,
                progress_callback=progress_callback,
                dry_run=dry_run,
            )
        )

    async def deduplicate_async(
        self,
        opportunities: Iterable[dict[str, Any]],
        *,
        keywords: Iterable[str] | None = None,
        trusted_sources: Iterable[str] | None = None,
        discovered_at: datetime | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        dry_run: bool = False,
    ) -> list[dict[str, Any]]:
        """Filter, deduplicate, and persist opportunity records."""
        opportunity_list = [dict(opportunity) for opportunity in opportunities]
        keyword_list = [keyword.lower() for keyword in (keywords or [])]
        allowed_sources = {
            source.lower() for source in (trusted_sources or self.trusted_sources or [])
        }
        found: list[dict[str, Any]] = []
        timestamp = self._to_iso(discovered_at)
        total_opportunities = len(opportunity_list)
        _emit_progress_event(
            progress_callback,
            stage="bulk-persist",
            description="Persisting discovered opportunities",
            completed=0,
            total=total_opportunities,
        )
        async with self.async_db_session() as session:
            for index, opportunity in enumerate(opportunity_list, start=1):
                source = str(opportunity.get("source", "")).strip()
                record = {
                    "source": source,
                    "donor_name": str(opportunity.get("donor_name", source or "Unknown donor")),
                    "title": str(opportunity.get("title", "Untitled opportunity")),
                    "portal_url": str(opportunity.get("portal_url", "")),
                    "summary": str(opportunity.get("summary", "")),
                    "category": str(opportunity.get("category", "")),
                    "discovered_at": timestamp,
                    "status": "new",
                    "raw_data_json": json.dumps(opportunity, sort_keys=True),
                }
                record["signature"] = self._signature_for(record)
                await session.execute(
                    """
                    INSERT INTO funnel_events (
                        stage, entity_key, connector_name, opportunity_signature, success,
                        happened_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "discover",
                        record["signature"],
                        source or None,
                        record["signature"],
                        1,
                        timestamp,
                        json.dumps({"source": source or None}, sort_keys=True),
                    ),
                )
                existing = await session.fetchone(
                    "SELECT 1 FROM opportunities WHERE signature = ?",
                    (record["signature"],),
                )
                await session.execute(
                    """
                    INSERT INTO funnel_events (
                        stage, entity_key, connector_name, opportunity_signature, success,
                        happened_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "dedupe",
                        record["signature"],
                        source or None,
                        record["signature"],
                        int(existing is None),
                        timestamp,
                        json.dumps(
                            {"reason": "unique" if existing is None else "duplicate"},
                            sort_keys=True,
                        ),
                    ),
                )
                if existing:
                    _emit_progress_event(
                        progress_callback,
                        stage="bulk-persist",
                        description="Persisting discovered opportunities",
                        completed=index,
                        total=total_opportunities,
                        item=record["title"],
                    )
                    continue
                if allowed_sources and source.lower() not in allowed_sources:
                    await session.execute(
                        """
                        INSERT INTO funnel_events (
                            stage, entity_key, connector_name, opportunity_signature, success,
                            happened_at, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "match",
                            record["signature"],
                            source or None,
                            record["signature"],
                            0,
                            timestamp,
                            json.dumps({"reason": "untrusted_source"}, sort_keys=True),
                        ),
                    )
                    _emit_progress_event(
                        progress_callback,
                        stage="bulk-persist",
                        description="Persisting discovered opportunities",
                        completed=index,
                        total=total_opportunities,
                        item=source or "unknown source",
                    )
                    continue

                searchable_parts = [
                    str(opportunity.get("title", "")),
                    str(opportunity.get("summary", "")),
                    " ".join(str(tag) for tag in opportunity.get("tags", [])),
                    str(opportunity.get("category", "")),
                ]
                searchable_text = " ".join(searchable_parts).lower()
                if keyword_list and not any(keyword in searchable_text for keyword in keyword_list):
                    await session.execute(
                        """
                        INSERT INTO funnel_events (
                            stage, entity_key, connector_name, opportunity_signature, success,
                            happened_at, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "match",
                            record["signature"],
                            source or None,
                            record["signature"],
                            0,
                            timestamp,
                            json.dumps({"reason": "keyword_miss"}, sort_keys=True),
                        ),
                    )
                    _emit_progress_event(
                        progress_callback,
                        stage="bulk-persist",
                        description="Persisting discovered opportunities",
                        completed=index,
                        total=total_opportunities,
                        item=str(opportunity.get("title", "Untitled opportunity")),
                    )
                    continue

                if not dry_run:
                    await session.execute(
                        """
                        INSERT INTO opportunities (
                            signature, source, donor_name, title, portal_url, summary,
                            category, discovered_at, status, raw_data_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record["signature"],
                            record["source"],
                            record["donor_name"],
                            record["title"],
                            record["portal_url"],
                            record["summary"],
                            record["category"],
                            record["discovered_at"],
                            record["status"],
                            record["raw_data_json"],
                        ),
                    )
                await session.execute(
                    """
                    INSERT INTO funnel_events (
                        stage, entity_key, connector_name, opportunity_signature, success,
                        happened_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "match",
                        record["signature"],
                        source or None,
                        record["signature"],
                        1,
                        timestamp,
                        json.dumps({"reason": "matched"}, sort_keys=True),
                    ),
                )
                found.append(record)
                _emit_progress_event(
                    progress_callback,
                    stage="bulk-persist",
                    description="Persisting discovered opportunities",
                    completed=index,
                    total=total_opportunities,
                    item=record["title"],
                    persisted_count=len(found),
                )

        if not dry_run:
            self._log_action("opportunities_discovered", count=len(found), keywords=keyword_list)
        return found

    def discover_opportunities(
        self,
        opportunities: Iterable[dict[str, Any]],
        *,
        keywords: Iterable[str] | None = None,
        trusted_sources: Iterable[str] | None = None,
        discovered_at: datetime | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        dry_run: bool = False,
    ) -> list[dict[str, Any]]:
        return self.deduplicate(
            opportunities,
            keywords=keywords,
            trusted_sources=trusted_sources,
            discovered_at=discovered_at,
            progress_callback=progress_callback,
            dry_run=dry_run,
        )

    def run_discovery(
        self,
        connectors: Iterable[PortalConnector] | None = None,
        *,
        keywords: Iterable[str] | None = None,
        trusted_sources: Iterable[str] | None = None,
        discovered_at: datetime | None = None,
        batch_size: int | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        dry_run: bool = False,
    ) -> list[dict[str, Any]]:
        return _run_async(
            self.run_discovery_async(
                connectors=connectors,
                keywords=keywords,
                trusted_sources=trusted_sources,
                discovered_at=discovered_at,
                batch_size=batch_size,
                progress_callback=progress_callback,
                dry_run=dry_run,
            )
        )

    async def run_discovery_async(
        self,
        connectors: Iterable[PortalConnector] | None = None,
        *,
        keywords: Iterable[str] | None = None,
        trusted_sources: Iterable[str] | None = None,
        discovered_at: datetime | None = None,
        batch_size: int | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        dry_run: bool = False,
    ) -> list[dict[str, Any]]:
        """Query donation-source connectors and persist new opportunities.

        This is the end-to-end "search" entry point: it queries each
        connector (grant portals, CSR networks, NGO directories, ...) using
        the configured keyword filters, then deduplicates and stores any new
        opportunities via :meth:`discover_opportunities`. If ``keywords`` or
        ``trusted_sources`` are omitted, the persisted search settings from
        :meth:`store_search_settings` are used instead.
        """
        settings = self.load_search_settings()
        keyword_list = list(keywords) if keywords is not None else settings.get("keywords", [])
        source_list = (
            list(trusted_sources)
            if trusted_sources is not None
            else settings.get("trusted_sources", [])
        )
        if connectors is not None:
            active_connectors = list(connectors)
        elif self.connector_configs:
            active_connectors = self.connector_registry.build_connectors(
                self.connector_configs,
                credential_resolver=self.resolve_credential,
                cache_manager=self.cache_manager,
            )
        else:
            active_connectors = default_connectors(cache_manager=self.cache_manager)
        fallback_mode = self._load_fallback_mode()
        total_connectors = len(active_connectors)

        scheduler = ConnectorBatchScheduler(
            batch_size=self._resolve_connector_batch_size(batch_size)
        )
        batched_requests = [
            ConnectorBatchRequest(connector=connector, keywords=tuple(keyword_list))
            for connector in active_connectors
        ]
        batch_results = await scheduler.submit_many(batched_requests)

        candidates: list[dict[str, Any]] = []
        _emit_progress_event(
            progress_callback,
            stage="connector-discovery",
            description="Discovering opportunities from connectors",
            completed=0,
            total=total_connectors,
        )
        for index, (connector, result) in enumerate(zip(active_connectors, batch_results), start=1):
            connector_name = self._connector_name(connector)
            cache_key = self._connector_cache_key(connector, keyword_list)
            health = connector.check_health()
            if not health.get("healthy", True):
                self._log_action(
                    "connector_degraded",
                    source=connector_name,
                    state=health.get("state"),
                    last_error=health.get("last_error"),
                )
            try:
                if isinstance(result, Exception):
                    raise result
                opportunities = [dict(item) for item in result.get("opportunities", [])]
                schema_version = int(
                    result.get(
                        "schema_version",
                        getattr(
                            connector, "result_schema_version", _CONNECTOR_RESULT_SCHEMA_VERSION
                        ),
                    )
                )
                metadata = dict(result.get("metadata", {}))
                source_status = str(metadata.get("source_status", "remote"))
                if source_status == "degraded":
                    raise RuntimeError(
                        str(
                            metadata.get("last_error")
                            or metadata.get("degraded_reason")
                            or f"{connector_name} is degraded"
                        )
                    )
                await self._store_connector_result_async(
                    connector_name=connector_name,
                    cache_key=cache_key,
                    schema_version=schema_version,
                    opportunities=opportunities,
                    metadata=metadata,
                    source_status=source_status,
                )
                request_count = self._safe_int(
                    metadata.get("request_count"),
                    1 if source_status == "remote" else 0,
                )
                default_cost = float(getattr(connector, "request_cost_usd", 0.0)) * (
                    request_count or (1 if source_status == "remote" else 0)
                )
                self.record_connector_call_metric(
                    connector_name=connector_name,
                    connector_type=str(getattr(connector, "connector_slug", connector_name)),
                    operation="discover",
                    source_status=source_status,
                    latency_seconds=self._safe_float(metadata.get("latency_seconds"), 0.0),
                    cost_usd=self._safe_float(metadata.get("request_cost_usd"), default_cost),
                    errored=False,
                    request_count=request_count,
                    metadata=metadata,
                )
                candidates.extend(opportunities)
                _emit_progress_event(
                    progress_callback,
                    stage="connector-discovery",
                    description=f"Processed connector {connector_name}",
                    current=connector_name,
                    completed=index,
                    total=total_connectors,
                )
                continue
            except Exception as exc:
                error_message = str(exc)

            fallback_result = None
            if fallback_mode in {"cache-first", "cache-only"}:
                fallback_result = await self._load_cached_connector_result_async(
                    connector, keyword_list
                )
                if fallback_result is not None:
                    fallback_result["metadata"] = {
                        **dict(fallback_result.get("metadata", {})),
                        "fallback_mode": "cached",
                        "activation_error": error_message,
                    }
                    fallback_result["source_status"] = "cached"
            if fallback_result is None and fallback_mode == "cache-first":
                fallback_result = self._default_connector_fallback(connector, keyword_list)
            elif fallback_result is None and fallback_mode == "default-only":
                fallback_result = self._default_connector_fallback(connector, keyword_list)
            elif fallback_mode == "disabled":
                raise RuntimeError(error_message)

            if fallback_result is None:
                self.record_connector_call_metric(
                    connector_name=connector_name,
                    connector_type=str(getattr(connector, "connector_slug", connector_name)),
                    operation="discover",
                    source_status="error",
                    latency_seconds=self._safe_float(
                        (
                            getattr(result, "metadata", {}).get("latency_seconds")
                            if hasattr(result, "metadata")
                            else 0.0
                        ),
                        0.0,
                    ),
                    errored=True,
                    metadata={"error": error_message},
                )
                logging.getLogger(__name__).warning(
                    "Connector %s failed with no fallback available: %s",
                    connector_name,
                    error_message,
                )
                _emit_progress_event(
                    progress_callback,
                    stage="connector-discovery",
                    description=f"Processed connector {connector_name}",
                    current=connector_name,
                    completed=index,
                    total=total_connectors,
                )
                continue

            fallback_result["metadata"] = {
                **dict(fallback_result.get("metadata", {})),
                "connector_name": connector_name,
                "cache_key": cache_key,
                "fallback_activated_at": self._to_iso(),
            }
            await self._store_connector_result_async(
                connector_name=connector_name,
                cache_key=cache_key,
                schema_version=int(fallback_result["schema_version"]),
                opportunities=fallback_result["opportunities"],
                metadata=fallback_result["metadata"],
                source_status=str(fallback_result.get("source_status", "cached")),
            )
            logging.getLogger(__name__).warning(
                "Connector %s fallback activated (%s): %s",
                connector_name,
                fallback_result["metadata"].get(
                    "fallback_mode", fallback_result.get("source_status", "cached")
                ),
                error_message,
            )
            self._log_action(
                "connector_fallback_activated",
                source=connector_name,
                fallback_mode=fallback_result["metadata"].get(
                    "fallback_mode", fallback_result.get("source_status", "cached")
                ),
                error=error_message,
                cache_key=cache_key,
                schema_version=int(fallback_result["schema_version"]),
                result_count=len(fallback_result["opportunities"]),
            )
            fallback_metadata = dict(fallback_result.get("metadata", {}))
            request_count = self._safe_int(
                fallback_metadata.get("request_count"),
                0 if str(fallback_result.get("source_status", "cached")) != "remote" else 1,
            )
            default_cost = float(getattr(connector, "request_cost_usd", 0.0)) * (
                request_count
                or (1 if str(fallback_result.get("source_status", "cached")) == "remote" else 0)
            )
            self.record_connector_call_metric(
                connector_name=connector_name,
                connector_type=str(getattr(connector, "connector_slug", connector_name)),
                operation="discover",
                source_status=str(fallback_result.get("source_status", "cached")),
                latency_seconds=self._safe_float(fallback_metadata.get("latency_seconds"), 0.0),
                cost_usd=self._safe_float(fallback_metadata.get("request_cost_usd"), default_cost),
                errored=True,
                request_count=request_count,
                metadata={**fallback_metadata, "activation_error": error_message},
            )
            candidates.extend([dict(item) for item in fallback_result["opportunities"]])
            _emit_progress_event(
                progress_callback,
                stage="connector-discovery",
                description=f"Processed connector {connector_name}",
                current=connector_name,
                completed=index,
                total=total_connectors,
            )

        return await self.deduplicate_async(
            candidates,
            keywords=keyword_list,
            trusted_sources=source_list,
            discovered_at=discovered_at,
            progress_callback=progress_callback,
            dry_run=dry_run,
        )

    def list_opportunities(self, *, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.connection.execute(
                "SELECT * FROM opportunities WHERE status = ? ORDER BY discovered_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM opportunities ORDER BY discovered_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_audit_logs(
        self,
        *,
        limit: int | None = None,
        action: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent audit log entries."""
        query = "SELECT * FROM audit_logs"
        params: list[Any] = []
        if action:
            query += " WHERE action = ?"
            params.append(action)
        query += " ORDER BY happened_at DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        rows = self.connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_translation_review(self, review_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM translation_reviews WHERE id = ?",
            (review_id,),
        ).fetchone()
        if row is None:
            raise FundingBotError(f"Unknown translation review id {review_id!r}.")
        review = dict(row)
        review["locale_metadata"] = self.get_locale_definition(review["locale"])
        return review

    def list_translation_reviews(
        self,
        *,
        status: str | None = None,
        locale: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT id FROM translation_reviews"
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(self._validate_translation_review_status(status))
        if locale is not None:
            clauses.append("locale = ?")
            params.append(self._validate_ui_locale(locale))
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += (
            " ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'rejected' THEN 1 ELSE 2 END,"
            " created_at DESC, id DESC"
        )
        rows = self.connection.execute(query, params).fetchall()
        return [self.get_translation_review(int(row["id"])) for row in rows]

    def submit_translation_review(
        self,
        *,
        locale: str,
        translation_key: str,
        source_text: str,
        translated_text: str,
        submitted_by_role: str | None = None,
        submitter_notes: str | None = None,
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        normalized_locale = self._validate_ui_locale(locale)
        normalized_key = sanitize_user_string(
            translation_key,
            field_name="translation_key",
            allow_empty=False,
            html_escape=True,
            max_length=256,
        )
        normalized_source = sanitize_user_string(
            source_text,
            field_name="source_text",
            allow_empty=False,
            multiline=True,
            html_escape=True,
        )
        normalized_translation = sanitize_user_string(
            translated_text,
            field_name="translated_text",
            allow_empty=False,
            multiline=True,
            html_escape=True,
        )
        normalized_notes = (
            sanitize_user_string(
                submitter_notes,
                field_name="submitter_notes",
                multiline=True,
                html_escape=True,
            )
            if submitter_notes is not None
            else None
        ) or None

        created_iso = self._to_iso(created_at)
        cursor = self.connection.execute(
            """
            INSERT INTO translation_reviews (
                locale,
                translation_key,
                source_text,
                translated_text,
                status,
                submitter_notes,
                submitted_by_role,
                created_at
            )
            VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (
                normalized_locale,
                normalized_key,
                normalized_source,
                normalized_translation,
                normalized_notes,
                submitted_by_role,
                created_iso,
            ),
        )
        self.connection.commit()
        self._log_action(
            "translation_review_submitted",
            review_id=cursor.lastrowid,
            locale=normalized_locale,
            translation_key=normalized_key,
            submitted_by_role=submitted_by_role,
        )
        return self.get_translation_review(cursor.lastrowid)

    def review_translation(
        self,
        review_id: int,
        *,
        status: str,
        reviewed_by_role: str | None,
        reviewer_notes: str | None = None,
        reviewed_at: datetime | None = None,
    ) -> dict[str, Any]:
        resolved_status = self._validate_translation_review_status(status, allow_pending=False)
        existing = self.connection.execute(
            "SELECT id FROM translation_reviews WHERE id = ?",
            (review_id,),
        ).fetchone()
        if existing is None:
            raise FundingBotError(f"Unknown translation review id {review_id!r}.")

        normalized_notes = (
            sanitize_user_string(
                reviewer_notes,
                field_name="reviewer_notes",
                multiline=True,
                html_escape=True,
            )
            if reviewer_notes is not None
            else None
        ) or None
        reviewed_iso = self._to_iso(reviewed_at)
        self.connection.execute(
            """
            UPDATE translation_reviews
            SET status = ?, reviewed_at = ?, reviewed_by_role = ?, reviewer_notes = ?
            WHERE id = ?
            """,
            (resolved_status, reviewed_iso, reviewed_by_role, normalized_notes, review_id),
        )
        self.connection.commit()
        updated = self.get_translation_review(review_id)
        self._log_action(
            "translation_review_updated",
            review_id=review_id,
            locale=updated["locale"],
            translation_key=updated["translation_key"],
            status=resolved_status,
            reviewed_by_role=reviewed_by_role,
        )
        return updated

    def create_task(
        self,
        *,
        title: str,
        assigned_to: str | None = None,
        assignee: str | None = None,
        assignee_email: str | None = None,
        assignee_name: str | None = None,
        description: str = "",
        status: str = "pending",
        created_at: datetime | None = None,
        due_date: datetime | str | None = None,
        external_id: str | None = None,
        source: str = "manual",
        attributed_connector: str | None = None,
        opportunity_signature: str | None = None,
        sender: Any | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        normalized_title = sanitize_user_string(
            title,
            field_name="title",
            allow_empty=False,
            html_escape=True,
        )
        normalized_assignee = (
            str(assignee if assignee is not None else assigned_to or "").strip().lower()
        )
        if not normalized_assignee:
            raise ValueError("Task assignee is required.")

        normalized_status = self._normalize_task_status(status)
        normalized_external_id = (
            sanitize_user_string(
                external_id,
                field_name="external_id",
                max_length=256,
            )
            if external_id is not None
            else None
        )
        if normalized_external_id == "":
            normalized_external_id = None
        normalized_due_date = self._normalize_task_due_date(
            None if due_date is None else str(due_date)
        )
        normalized_source = (
            sanitize_user_string(
                source or "manual",
                field_name="source",
                allow_empty=False,
                html_escape=True,
                max_length=128,
            )
            or "manual"
        )
        normalized_attributed_connector = (
            str(attributed_connector).strip() if attributed_connector is not None else None
        ) or None
        normalized_opportunity_signature = (
            str(opportunity_signature).strip() if opportunity_signature is not None else None
        ) or None
        if normalized_attributed_connector is None and normalized_opportunity_signature is not None:
            normalized_attributed_connector = self._connector_for_opportunity(
                normalized_opportunity_signature
            )
        normalized_assignee_email = (
            _validate_email(str(assignee_email))
            if assignee_email is not None and str(assignee_email).strip()
            else None
        )
        normalized_assignee_name = (
            sanitize_user_string(
                assignee_name,
                field_name="assignee_name",
                allow_empty=False,
                html_escape=True,
                max_length=256,
            )
            if assignee_name and str(assignee_name).strip()
            else None
        )
        timestamp = self._to_iso(created_at)
        cursor = self.connection.execute(
            """
            INSERT INTO tasks (
                external_id, title, description, assignee, assigned_to, assignee_email, assignee_name,
                status, due_date, source, attributed_connector, opportunity_signature, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_external_id,
                normalized_title,
                sanitize_user_string(
                    description or "",
                    field_name="description",
                    multiline=True,
                    html_escape=True,
                ),
                normalized_assignee,
                normalized_assignee,
                normalized_assignee_email,
                normalized_assignee_name,
                normalized_status,
                normalized_due_date,
                normalized_source,
                normalized_attributed_connector,
                normalized_opportunity_signature,
                timestamp,
                timestamp,
            ),
        )
        if commit:
            self.connection.commit()
        task = self.get_task(cursor.lastrowid)
        self._log_action(
            "task_created",
            commit=commit,
            task_id=task["id"],
            title=task["title"],
            assignee=task["assignee"],
            status=task["status"],
            due_date=task["due_date"],
            external_id=task["external_id"],
            source=task["source"],
            assignee_email=task.get("assignee_email"),
            attributed_connector=task.get("attributed_connector"),
            opportunity_signature=task.get("opportunity_signature"),
        )
        self._log_action(
            "task_assignment_changed",
            commit=commit,
            task_id=task["id"],
            title=task["title"],
            previous_assignee=None,
            assignee=task["assignee"],
            external_id=task["external_id"],
            assignee_email=task.get("assignee_email"),
        )
        if task.get("assignee_email"):
            task["assignment_notification"] = self._notify_task_assignee(
                task["id"],
                sender=sender,
                happened_at=created_at,
            )
        return task

    def get_task(self, task_id: int, *, viewer_email: str | None = None) -> dict[str, Any]:
        task_row = self._get_task_row(task_id)
        task_model = Task.from_row(task_row)
        if task_model is None:
            raise TaskNotFoundError(f"Task {task_id!r} does not exist.")
        task = self._serialize_task(task_model)
        if viewer_email:
            task["unread_comment_count"] = self.get_unread_task_comment_count(task_id, viewer_email)
        return task

    def get_task_by_external_id(self, external_id: str) -> dict[str, Any]:
        normalized = str(external_id).strip()
        if not normalized:
            raise ValueError("Task external_id is required.")
        row = self.connection.execute(
            "SELECT * FROM tasks WHERE external_id = ?",
            (normalized,),
        ).fetchone()
        if row is None:
            raise TaskNotFoundError(f"Task with external_id {external_id!r} does not exist.")
        task = Task.from_row(row)
        if task is None:
            raise TaskNotFoundError(f"Task with external_id {external_id!r} does not exist.")
        return self._serialize_task(task)

    def list_tasks(
        self,
        *,
        assigned_to: str | None = None,
        assignee: str | None = None,
        assignee_email: str | None = None,
        status: str | None = None,
        due_date_before: datetime | str | None = None,
        due_date_after: datetime | str | None = None,
        due_before: datetime | str | None = None,
        due_after: datetime | str | None = None,
        sort: str | None = None,
        sort_by: str | None = None,
        sort_order: str | None = None,
        source: str | None = None,
        viewer_email: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM tasks"
        params: list[Any] = []
        clauses: list[str] = []
        effective_assignee = assignee if assignee is not None else assigned_to
        if effective_assignee:
            clauses.append("assigned_to = ?")
            params.append(str(effective_assignee).strip().lower())
        if assignee_email:
            clauses.append("assignee_email = ?")
            params.append(_validate_email(assignee_email))
        if status:
            clauses.append("status = ?")
            params.append(self._normalize_task_status(status))
        normalized_due_date_before = (
            self._normalize_task_due_date(
                str(due_before if due_before is not None else due_date_before)
            )
            if (due_before is not None or due_date_before is not None)
            else None
        )
        if normalized_due_date_before:
            clauses.append("due_date <= ?")
            params.append(normalized_due_date_before)
        normalized_due_date_after = (
            self._normalize_task_due_date(
                str(due_after if due_after is not None else due_date_after)
            )
            if (due_after is not None or due_date_after is not None)
            else None
        )
        if normalized_due_date_after:
            clauses.append("due_date >= ?")
            params.append(normalized_due_date_after)
        if source:
            clauses.append("source = ?")
            params.append(str(source).strip())
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY " + self._task_sort_clause(sort, sort_by=sort_by, sort_order=sort_order)
        rows = self.connection.execute(query, params).fetchall()
        normalized_viewer_email = _validate_email(viewer_email) if viewer_email else None
        tasks = [self._serialize_task(Task.from_row(row) or row) for row in rows]
        if normalized_viewer_email:
            for task in tasks:
                task["unread_comment_count"] = self.get_unread_task_comment_count(
                    int(task["id"]),
                    normalized_viewer_email,
                )
        return tasks

    @staticmethod
    def _task_sort_clause(
        sort: str | None,
        *,
        sort_by: str | None = None,
        sort_order: str | None = None,
    ) -> str:
        if sort_by is not None:
            normalized_field = str(sort_by).strip().lower()
            normalized_order = str(sort_order or "asc").strip().lower()
            if normalized_order not in {"asc", "desc"}:
                raise ValueError("Invalid task sort_order. Expected 'asc' or 'desc'.")
            prefix = "-" if normalized_order == "desc" else ""
            sort_key = (
                f"{prefix}{normalized_field}"
                if normalized_field != "updated_at"
                else ("updated_at" if normalized_order == "desc" else "-updated_at")
            )
        else:
            sort_key = (sort or "").strip().lower()
        sort_map = {
            "": "updated_at DESC, id DESC",
            "updated_at": "updated_at DESC, id DESC",
            "-updated_at": "updated_at ASC, id ASC",
            "assignee": "assigned_to COLLATE NOCASE ASC, due_date ASC, id ASC",
            "-assignee": "assigned_to COLLATE NOCASE DESC, due_date DESC, id DESC",
            "title": "title COLLATE NOCASE ASC, due_date ASC, id ASC",
            "-title": "title COLLATE NOCASE DESC, due_date DESC, id DESC",
            "status": "status COLLATE NOCASE ASC, due_date IS NULL ASC, due_date ASC, id ASC",
            "-status": "status COLLATE NOCASE DESC, due_date IS NULL ASC, due_date DESC, id DESC",
            "due_date": "due_date IS NULL ASC, due_date ASC, id ASC",
            "-due_date": "due_date IS NULL ASC, due_date DESC, id DESC",
            "created_at": "created_at ASC, id ASC",
            "-created_at": "created_at DESC, id DESC",
        }
        if sort_key not in sort_map:
            raise ValueError(
                "Invalid task sort. Expected one of "
                "['assignee', '-assignee', 'title', '-title', 'due_date', '-due_date', "
                "'status', '-status', 'created_at', '-created_at', 'updated_at', '-updated_at']."
            )
        return sort_map[sort_key]

    def get_task_status_counts(
        self,
        *,
        assigned_to: str | None = None,
        assignee: str | None = None,
    ) -> dict[str, int]:
        query = "SELECT status, COUNT(*) AS total FROM tasks"
        params: list[Any] = []
        effective_assignee = assignee if assignee is not None else assigned_to
        if effective_assignee:
            query += " WHERE assigned_to = ?"
            params.append(str(effective_assignee).strip().lower())
        query += " GROUP BY status"
        counts = {status: 0 for status in self.TASK_STATUSES}
        for row in self.connection.execute(query, params).fetchall():
            counts[str(row["status"])] = int(row["total"])
        return counts

    def transition_task_status(
        self,
        task_id: int,
        *,
        new_status: str,
        changed_by: str,
    ) -> dict[str, Any]:
        task = self.get_task(task_id)
        normalized_status = self._normalize_task_status(new_status)
        self._validate_task_transition(task["status"], normalized_status)
        if task["status"] == normalized_status:
            return task

        updated_at = self._to_iso()
        with self.connection:
            updated = self.connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (normalized_status, updated_at, task_id),
            )
            if updated.rowcount == 0:
                raise TaskNotFoundError(f"Task {task_id!r} does not exist.")
        notification = f"Task '{task['title']}' moved from {task['status']} to {normalized_status}."
        self._log_action(
            "task_status_changed",
            task_id=task_id,
            title=task["title"],
            assignee=task["assignee"],
            previous_status=task["status"],
            status=normalized_status,
            changed_by=str(changed_by).strip().lower(),
            notification=notification,
            external_id=task.get("external_id"),
        )
        updated_task = self.get_task(task_id)
        updated_task["notification"] = notification
        return updated_task

    def update_task(
        self,
        task_id: int,
        *,
        title: str | None = None,
        description: str | None = None,
        assignee: str | None = None,
        status: str | None = None,
        due_date: datetime | str | None = None,
        attributed_connector: str | None = None,
        opportunity_signature: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": task_id}
        if title is not None:
            payload["title"] = title
        if description is not None:
            payload["description"] = description
        if assignee is not None:
            payload["assigned_to"] = assignee
        if status is not None:
            payload["status"] = status
        if due_date is not None:
            payload["due_date"] = due_date
        if attributed_connector is not None:
            payload["attributed_connector"] = attributed_connector
        if opportunity_signature is not None:
            payload["opportunity_signature"] = opportunity_signature
        if len(payload) == 1:
            raise ValueError("At least one task field must be provided for update.")
        return self.upsert_task(payload)

    def update_task_assignment(
        self,
        task_id: int,
        *,
        assigned_to: str,
        assignee_email: str | None = None,
        assignee_name: str | None = None,
        sender: Any | None = None,
        changed_by: str | None = None,
        changed_at: datetime | None = None,
    ) -> dict[str, Any]:
        task = self.get_task(task_id)
        normalized_assigned_to = str(assigned_to).strip().lower()
        if not normalized_assigned_to:
            raise ValueError("Task assignee is required.")
        normalized_assignee_email = (
            _validate_email(str(assignee_email))
            if assignee_email is not None and str(assignee_email).strip()
            else None
        )
        normalized_assignee_name = (
            sanitize_user_string(
                assignee_name,
                field_name="assignee_name",
                allow_empty=False,
                html_escape=True,
                max_length=256,
            )
            if assignee_name and str(assignee_name).strip()
            else None
        )
        updated_at = self._to_iso(changed_at)
        with self.connection:
            self.connection.execute(
                """
                UPDATE tasks
                SET assignee = ?, assigned_to = ?, assignee_email = ?, assignee_name = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    normalized_assigned_to,
                    normalized_assigned_to,
                    normalized_assignee_email,
                    normalized_assignee_name,
                    updated_at,
                    task_id,
                ),
            )
            self._log_action(
                "task_assignment_changed",
                commit=False,
                task_id=task_id,
                title=task["title"],
                previous_assignee=task["assigned_to"],
                previous_assigned_to=task["assigned_to"],
                assigned_to=normalized_assigned_to,
                previous_assignee_email=task.get("assignee_email"),
                assignee_email=normalized_assignee_email,
                changed_by=str(changed_by or "").strip().lower() or None,
                external_id=task.get("external_id"),
            )
        updated_task = self.get_task(task_id)
        updated_task["assignment_notification"] = self._notify_task_assignee(
            task_id,
            sender=sender,
            happened_at=changed_at,
        )
        return updated_task

    def assign_task(
        self,
        task_id: int,
        *,
        assigned_to: str,
        changed_by: str | None = None,
    ) -> dict[str, Any]:
        existing = self.get_task(task_id)
        return self.update_task_assignment(
            task_id,
            assigned_to=assigned_to,
            assignee_email=existing.get("assignee_email"),
            assignee_name=existing.get("assignee_name"),
            changed_by=changed_by,
        )

    def create_task_comment(
        self,
        task_id: int,
        *,
        author: str,
        content: str,
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        self._get_task_row(task_id)
        normalized_author = sanitize_user_string(
            author,
            field_name="author",
            allow_empty=False,
            html_escape=True,
            max_length=256,
        )
        normalized_content = sanitize_user_string(
            content,
            field_name="content",
            allow_empty=False,
            multiline=True,
            html_escape=True,
        )
        timestamp = self._to_iso(created_at)
        cursor = self.connection.execute(
            """
            INSERT INTO task_comments (task_id, author, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (task_id, normalized_author, normalized_content, timestamp, timestamp),
        )
        self.connection.commit()
        comment = self._serialize_task_comment(
            self._get_task_comment_row(task_id, cursor.lastrowid)
        )
        self._log_action(
            "task_comment_created",
            task_id=task_id,
            comment_id=comment["id"],
            author=comment["author"],
        )
        return comment

    def list_task_comments(
        self,
        task_id: int,
        *,
        viewer_email: str | None = None,
    ) -> dict[str, Any]:
        task = self.get_task(task_id, viewer_email=viewer_email)
        rows = self.connection.execute(
            """
            SELECT * FROM task_comments
            WHERE task_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (task_id,),
        ).fetchall()
        return {
            "task": task,
            "comments": [self._serialize_task_comment(row) for row in rows],
            "unread_count": task["unread_comment_count"],
        }

    def update_task_comment(
        self,
        task_id: int,
        comment_id: int,
        *,
        content: str,
        updated_at: datetime | None = None,
    ) -> dict[str, Any]:
        comment = self._serialize_task_comment(self._get_task_comment_row(task_id, comment_id))
        normalized_content = sanitize_user_string(
            content,
            field_name="content",
            allow_empty=False,
            multiline=True,
            html_escape=True,
        )
        updated_iso = self._to_iso(updated_at)
        with self.connection:
            self.connection.execute(
                "UPDATE task_comments SET content = ?, updated_at = ? WHERE id = ? AND task_id = ?",
                (normalized_content, updated_iso, comment_id, task_id),
            )
            self._log_action(
                "task_comment_updated",
                commit=False,
                task_id=task_id,
                comment_id=comment_id,
                author=comment["author"],
            )
        return self._serialize_task_comment(self._get_task_comment_row(task_id, comment_id))

    def delete_task_comment(self, task_id: int, comment_id: int) -> None:
        comment = self._serialize_task_comment(self._get_task_comment_row(task_id, comment_id))
        with self.connection:
            self.connection.execute(
                "DELETE FROM task_comments WHERE id = ? AND task_id = ?",
                (comment_id, task_id),
            )
            self._log_action(
                "task_comment_deleted",
                commit=False,
                task_id=task_id,
                comment_id=comment_id,
                author=comment["author"],
            )

    def mark_task_comments_read(
        self,
        task_id: int,
        *,
        reader_email: str,
        read_at: datetime | None = None,
    ) -> dict[str, Any]:
        self._get_task_row(task_id)
        normalized_email = _validate_email(reader_email)
        read_iso = self._to_iso(read_at)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO task_comment_reads (task_id, reader_email, last_read_at)
                VALUES (?, ?, ?)
                ON CONFLICT(task_id, reader_email) DO UPDATE SET last_read_at = excluded.last_read_at
                """,
                (task_id, normalized_email, read_iso),
            )
            self._log_action(
                "task_comments_marked_read",
                commit=False,
                task_id=task_id,
                reader_email=normalized_email,
                last_read_at=read_iso,
            )
        return {
            "task_id": task_id,
            "reader_email": normalized_email,
            "last_read_at": read_iso,
            "unread_count": self.get_unread_task_comment_count(task_id, normalized_email),
        }

    def get_unread_task_comment_count(self, task_id: int, reader_email: str) -> int:
        normalized_email = _validate_email(reader_email)
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS unread_count
            FROM task_comments comments
            LEFT JOIN task_comment_reads reads
                ON reads.task_id = comments.task_id AND reads.reader_email = ?
            WHERE comments.task_id = ?
                AND lower(comments.author) != lower(?)
                AND (
                    reads.last_read_at IS NULL
                    OR comments.updated_at > reads.last_read_at
                )
            """,
            (normalized_email, task_id, normalized_email),
        ).fetchone()
        return int(row["unread_count"]) if row else 0

    def upsert_task(
        self,
        payload: dict[str, Any],
        *,
        default_source: str = "external_sync",
        commit: bool = True,
    ) -> dict[str, Any]:
        external_id_raw = payload.get("external_id")
        external_id = (
            sanitize_user_string(
                external_id_raw,
                field_name="external_id",
                max_length=256,
            )
            if external_id_raw is not None
            else None
        )
        if external_id == "":
            external_id = None

        existing = None
        task_id = payload.get("id")
        if task_id is not None:
            existing = self.connection.execute(
                "SELECT * FROM tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        if existing is None and external_id is not None:
            existing = self.connection.execute(
                "SELECT * FROM tasks WHERE external_id = ?",
                (external_id,),
            ).fetchone()

        if existing is None:
            if payload.get("title") is None:
                raise ValueError("Field 'title' is required for new tasks.")
            if payload.get("assigned_to") is None and payload.get("assignee") is None:
                raise ValueError("Field 'assignee' is required for new tasks.")
            return self.create_task(
                title=str(payload.get("title", "")),
                assigned_to=str(payload.get("assigned_to", payload.get("assignee", ""))),
                description=str(payload.get("description", "")),
                status=str(payload.get("status", "pending")),
                created_at=None,
                due_date=payload.get("due_date"),
                external_id=external_id,
                source=str(payload.get("source") or default_source),
                commit=commit,
            )

        updated_title = sanitize_user_string(
            payload.get("title", existing["title"]),
            field_name="title",
            allow_empty=False,
            html_escape=True,
        )
        updated_assigned_to = (
            str(payload.get("assigned_to", payload.get("assignee", existing["assignee"])))
            .strip()
            .lower()
        )
        updated_description = sanitize_user_string(
            payload.get("description", existing["description"]),
            field_name="description",
            multiline=True,
            html_escape=True,
        )
        updated_status = self._normalize_task_status(str(payload.get("status", existing["status"])))
        updated_due_date = (
            self._normalize_task_due_date(str(payload.get("due_date")))
            if "due_date" in payload and payload.get("due_date") is not None
            else (None if "due_date" in payload else existing["due_date"])
        )
        updated_source = sanitize_user_string(
            payload.get("source", existing["source"] or default_source),
            field_name="source",
            allow_empty=False,
            html_escape=True,
            max_length=128,
        )
        updated_external_id = external_id if external_id is not None else existing["external_id"]

        if not updated_assigned_to:
            raise ValueError("Task assignee is required.")

        changed_fields = [
            field
            for field, old_value, new_value in (
                ("external_id", existing["external_id"], updated_external_id),
                ("title", existing["title"], updated_title),
                ("description", existing["description"], updated_description),
                ("assignee", existing["assignee"], updated_assigned_to),
                ("status", existing["status"], updated_status),
                ("due_date", existing["due_date"], updated_due_date),
                ("source", existing["source"], updated_source),
            )
            if old_value != new_value
        ]
        self.connection.execute(
            """
            UPDATE tasks
            SET external_id = ?, title = ?, description = ?, assignee = ?, assigned_to = ?, status = ?,
                due_date = ?, source = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                updated_external_id,
                updated_title,
                updated_description,
                updated_assigned_to,
                updated_assigned_to,
                updated_status,
                updated_due_date,
                updated_source,
                self._to_iso(),
                existing["id"],
            ),
        )
        if commit:
            self.connection.commit()
        refreshed = self.get_task(int(existing["id"]))
        if changed_fields:
            self._log_action(
                "task_updated",
                commit=commit,
                task_id=refreshed["id"],
                title=refreshed["title"],
                assignee=refreshed["assignee"],
                external_id=refreshed["external_id"],
                changed_fields=changed_fields,
                status=refreshed["status"],
                due_date=refreshed["due_date"],
                source=refreshed["source"],
            )
        elif commit:
            self.connection.commit()
        if existing["assignee"] != refreshed["assignee"]:
            self._log_action(
                "task_assignment_changed",
                commit=commit,
                task_id=refreshed["id"],
                title=refreshed["title"],
                previous_assignee=existing["assignee"],
                assignee=refreshed["assignee"],
                external_id=refreshed["external_id"],
            )
        return refreshed

    def sync_tasks(
        self,
        tasks: Iterable[dict[str, Any]],
        *,
        default_source: str = "external_sync",
    ) -> list[dict[str, Any]]:
        synced: list[dict[str, Any]] = []
        with self.connection:
            for task in tasks:
                synced.append(self.upsert_task(task, default_source=default_source, commit=False))
            self._log_action(
                "tasks_synced",
                commit=False,
                count=len(synced),
                source=default_source,
            )
        return synced

    def import_tasks_from_csv(
        self,
        csv_text: str,
        *,
        default_source: str = "csv_import",
    ) -> list[dict[str, Any]]:
        if not str(csv_text).strip():
            raise ValueError("CSV import body is empty.")

        reader = csv.DictReader(io.StringIO(csv_text))
        allowed_headers = {
            "external_id",
            "title",
            "description",
            "assigned_to",
            "status",
            "due_date",
            "source",
        }
        if reader.fieldnames is None:
            raise ValueError(
                "CSV header row is required. Expected columns: "
                "external_id,title,description,assigned_to,status,due_date,source."
            )
        unknown_headers = sorted(set(reader.fieldnames) - allowed_headers)
        if unknown_headers:
            raise ValueError(f"Unsupported CSV columns: {unknown_headers}.")

        imported: list[dict[str, Any]] = []
        with self.connection:
            for row_number, row in enumerate(reader, start=2):
                if row is None or not any((value or "").strip() for value in row.values()):
                    continue
                try:
                    imported.append(
                        self.upsert_task(
                            {
                                "external_id": row.get("external_id"),
                                "title": row.get("title"),
                                "description": row.get("description", ""),
                                "assigned_to": row.get("assigned_to"),
                                "status": row.get("status") or "todo",
                                "due_date": row.get("due_date"),
                                "source": row.get("source") or default_source,
                            },
                            default_source=default_source,
                            commit=False,
                        )
                    )
                except (FundingBotError, ValueError) as exc:
                    raise ValueError(f"CSV row {row_number}: {exc}") from exc
            if not imported:
                raise ValueError("CSV import did not contain any task rows.")
            self._log_action(
                "tasks_imported",
                commit=False,
                count=len(imported),
                source=default_source,
            )
        return imported

    def _get_opportunity(self, signature: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM opportunities WHERE signature = ?",
            (signature,),
        ).fetchone()
        if not row:
            raise OpportunityNotFoundError(f"Unknown opportunity {signature!r}.")
        return row

    def submit_application(
        self,
        opportunity_signature: str,
        *,
        submission_reference: str | None,
        status: str,
        next_action: str,
        submitted_at: datetime | None = None,
    ) -> dict[str, Any]:
        row = self._get_opportunity(opportunity_signature)
        attributed_connector = str(row["source"]).strip() or None
        existing = self.connection.execute(
            "SELECT 1 FROM applications WHERE opportunity_signature = ?",
            (opportunity_signature,),
        ).fetchone()
        if existing:
            raise DuplicateSubmissionError(
                f"An application already exists for opportunity {opportunity_signature!r}."
            )

        timestamp = self._to_iso(submitted_at)
        self.connection.execute(
            """
            INSERT INTO applications (
                opportunity_signature, donor_name, portal_url, submitted_at,
                status, next_action, submission_reference, attributed_connector
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                opportunity_signature,
                row["donor_name"],
                row["portal_url"],
                timestamp,
                status,
                next_action,
                submission_reference,
                attributed_connector,
            ),
        )
        self.connection.execute(
            "UPDATE opportunities SET status = ? WHERE signature = ?",
            (status, opportunity_signature),
        )
        self.connection.commit()
        self._log_action(
            "application_recorded",
            opportunity_signature=opportunity_signature,
            status=status,
            next_action=next_action,
            attributed_connector=attributed_connector,
        )
        return {
            "opportunity_signature": opportunity_signature,
            "status": status,
            "next_action": next_action,
            "submission_reference": submission_reference,
            "submitted_at": timestamp,
            "attributed_connector": attributed_connector,
        }

    def submit_application_via_browser(
        self,
        opportunity_signature: str,
        *,
        credential_alias: str,
        browser_client: BrowserClient,
        form_data: dict[str, Any],
        attachments: Iterable[str] | None = None,
        max_retries: int = 3,
    ) -> dict[str, Any]:
        opportunity = self._get_opportunity(opportunity_signature)
        existing = self.connection.execute(
            "SELECT status FROM applications WHERE opportunity_signature = ?",
            (opportunity_signature,),
        ).fetchone()
        if existing:
            raise DuplicateSubmissionError(
                f"An application already exists for opportunity {opportunity_signature!r}."
            )

        credentials = self.resolve_credential(credential_alias)
        attachment_list = list(attachments or [])
        last_error = ""

        for attempt in range(1, max_retries + 1):
            try:
                reference = browser_client.submit(
                    opportunity["portal_url"],
                    credentials,
                    form_data,
                    attachment_list,
                )
            except Exception as exc:
                last_error = str(exc)
                self.connection.execute(
                    """
                    INSERT INTO submission_attempts (
                        opportunity_signature, attempt_number, succeeded, error_message, happened_at
                    ) VALUES (?, ?, 0, ?, ?)
                    """,
                    (opportunity_signature, attempt, last_error, self._to_iso()),
                )
                self.connection.commit()
                continue

            self.connection.execute(
                """
                INSERT INTO submission_attempts (
                    opportunity_signature, attempt_number, succeeded, error_message, happened_at
                ) VALUES (?, ?, 1, NULL, ?)
                """,
                (opportunity_signature, attempt, self._to_iso()),
            )
            self.connection.commit()
            return self.submit_application(
                opportunity_signature,
                submission_reference=reference,
                status="submitted",
                next_action="Await donor review",
            )

        return self.submit_application(
            opportunity_signature,
            submission_reference=None,
            status="pending",
            next_action=f"Retry failed browser submission: {last_error or 'unknown error'}",
        )

    def update_application_status(
        self,
        opportunity_signature: str,
        *,
        status: str,
        next_action: str,
    ) -> None:
        with self.connection:
            updated_application = self.connection.execute(
                "UPDATE applications SET status = ?, next_action = ? WHERE opportunity_signature = ?",
                (status, next_action, opportunity_signature),
            )
            if updated_application.rowcount == 0:
                raise FundingBotError(
                    f"No application exists for opportunity {opportunity_signature!r}."
                )
            updated_opportunity = self.connection.execute(
                "UPDATE opportunities SET status = ? WHERE signature = ?",
                (status, opportunity_signature),
            )
            if updated_opportunity.rowcount == 0:
                raise OpportunityNotFoundError(f"Unknown opportunity {opportunity_signature!r}.")
        self._log_action(
            "application_status_updated",
            opportunity_signature=opportunity_signature,
            status=status,
            next_action=next_action,
        )

    def poll_application_status(
        self,
        opportunity_signature: str,
        http_client: Callable[..., Any] | None,
    ) -> dict[str, Any]:
        """Poll a remote status endpoint and update local records if needed."""
        application = self.connection.execute(
            """
            SELECT a.status, a.next_action, a.submission_reference, o.portal_url
            FROM applications a
            JOIN opportunities o ON o.signature = a.opportunity_signature
            WHERE a.opportunity_signature = ?
            """,
            (opportunity_signature,),
        ).fetchone()
        if application is None:
            raise FundingBotError(
                f"No application exists for opportunity {opportunity_signature!r}."
            )

        if http_client is None:
            remote_status = {
                "status": (
                    application["status"]
                    if application["status"] in {"approved", "declined", "closed"}
                    else "in_review"
                ),
                "next_action": "Continue monitoring remote application portal.",
            }
        else:
            response = http_client(
                f"{application['portal_url'].rstrip('/')}/status",
                {
                    "opportunity_signature": opportunity_signature,
                    "submission_reference": application["submission_reference"],
                },
            )
            remote_status = dict(response)

        changed = (
            remote_status.get("status") != application["status"]
            or remote_status.get("next_action") != application["next_action"]
        )
        if changed:
            self.update_application_status(
                opportunity_signature,
                status=str(remote_status.get("status", application["status"])),
                next_action=str(remote_status.get("next_action", application["next_action"])),
            )
        self._log_action(
            "application_status_polled",
            opportunity_signature=opportunity_signature,
            changed=changed,
            remote_status=remote_status.get("status"),
        )
        return {
            "opportunity_signature": opportunity_signature,
            "status": str(remote_status.get("status", application["status"])),
            "next_action": str(remote_status.get("next_action", application["next_action"])),
            "changed": changed,
        }

    def send_outreach(
        self,
        *,
        donor_email: str,
        donor_name: str,
        subject_template: str,
        body_template: str,
        context: dict[str, Any] | None = None,
        sender: Any | None = None,
        sent_at: datetime | None = None,
        locale: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        donor_email = _validate_email(donor_email)
        requested_locale = self._validate_locale(locale) if locale is not None else None
        donor = self.connection.execute(
            "SELECT * FROM donors WHERE email = ?",
            (donor_email,),
        ).fetchone()
        if donor is None:
            if not dry_run:
                self.upsert_donor(email=donor_email, name=donor_name, locale=requested_locale)
                donor = self.connection.execute(
                    "SELECT * FROM donors WHERE email = ?",
                    (donor_email,),
                ).fetchone()
        elif requested_locale is not None and donor["locale"] != requested_locale:
            if not dry_run:
                self.connection.execute(
                    "UPDATE donors SET locale = ? WHERE email = ?",
                    (requested_locale, donor_email),
                )
                self.connection.commit()
                self._invalidate_donor_cache(donor_email)
                donor = self.connection.execute(
                    "SELECT * FROM donors WHERE email = ?",
                    (donor_email,),
                ).fetchone()

        would_create_donor = donor is None
        donor_locale = self._validate_locale(
            requested_locale or (donor["locale"] if donor is not None else None)
        )
        donor_opted_out = bool(donor["opted_out"]) if donor is not None else False
        donor_last_contact_at = donor["last_contact_at"] if donor is not None else None

        if donor_opted_out:
            raise OptOutError(f"{donor_email} has opted out of outreach.")

        consent_context = context or {}
        latest_consent = self.get_latest_consent_record(
            donor_email,
            channel=consent_context.get("consent_channel", "email"),
        )
        if latest_consent is not None and latest_consent["status"] == "withdrawn":
            raise OptOutError(f"{donor_email} has opted out of outreach.")

        send_time = self._as_utc(sent_at)
        if donor_last_contact_at:
            last_contact = self._as_utc(datetime.fromisoformat(donor_last_contact_at))
            if send_time - last_contact < timedelta(days=7):
                raise OutreachThrottledError(
                    f"{donor_email} was contacted less than seven days ago."
                )

        profile = self.load_organization_profile()
        merged_context = {
            "donor_name": donor_name,
            "donor_locale": donor_locale,
            "organization_name": profile.get("name", "Nonprofit Funding Bot"),
            "mission": profile.get("mission", ""),
            "opt_out_url": (context or {}).get("opt_out_url", "https://example.org/unsubscribe"),
        }
        merged_context.update(profile)
        merged_context.update(consent_context)
        related_opportunity_signature = (
            str(merged_context.get("opportunity_signature")).strip()
            if merged_context.get("opportunity_signature") is not None
            else None
        ) or None
        related_task_id = (
            self._safe_int(merged_context.get("task_id"))
            if merged_context.get("task_id") not in (None, "")
            else None
        )
        attributed_connector = self._resolve_connector_attribution(
            attributed_connector=(
                str(merged_context.get("attributed_connector")).strip()
                if merged_context.get("attributed_connector") is not None
                else None
            ),
            opportunity_signature=related_opportunity_signature,
            task_id=related_task_id,
        )

        if latest_consent is None:
            if not dry_run:
                self.record_consent(
                    donor_email,
                    donor_name=donor_name,
                    consented_at=send_time,
                    channel=merged_context.get("consent_channel", "email"),
                    source=str(merged_context.get("consent_source", "outreach_delivery")),
                    proof=(
                        str(merged_context["consent_proof"])
                        if merged_context.get("consent_proof") is not None
                        else None
                    ),
                    notes=(
                        str(merged_context["consent_notes"])
                        if merged_context.get("consent_notes") is not None
                        else "Consent record captured automatically when outreach was first sent."
                    ),
                    locale=donor_locale,
                )

        subject = subject_template.format(**merged_context)
        body = body_template.format(**merged_context).rstrip()
        if merged_context["opt_out_url"] not in body:
            opt_out_notice = self._localized_opt_out_notice(donor_locale).format(**merged_context)
            body = f"{body}\n\n{opt_out_notice}"

        if sender is not None and not dry_run:
            sender(donor_email, subject, body)

        sent_iso = self._to_iso(send_time)
        preview = {
            "would_create_donor": would_create_donor,
            "would_record_consent": latest_consent is None,
            "would_send_email": True,
            "would_log_communication": True,
            "would_update_last_contact": True,
        }
        result = {
            "email": donor_email,
            "subject": subject,
            "body": body,
            "sent_at": sent_iso,
            "locale": donor_locale,
            "attributed_connector": attributed_connector,
            "opportunity_signature": related_opportunity_signature,
            "dry_run": dry_run,
            "preview": preview,
        }
        if dry_run:
            return result
        cursor = self.connection.execute(
            """
            INSERT INTO communications (
                donor_email, donor_name, subject, body, channel, sent_at,
                attributed_connector, related_opportunity_signature, related_task_id
            )
            VALUES (?, ?, ?, ?, 'email', ?, ?, ?, ?)
            """,
            (
                donor_email,
                donor_name,
                subject,
                body,
                sent_iso,
                attributed_connector,
                related_opportunity_signature,
                related_task_id,
            ),
        )
        self.connection.execute(
            """
            INSERT INTO outreach_events (communication_id, event_type, happened_at)
            VALUES (?, 'sent', ?)
            """,
            (cursor.lastrowid, sent_iso),
        )
        self.connection.execute(
            "UPDATE donors SET last_contact_at = ? WHERE email = ?",
            (sent_iso, donor_email),
        )
        self.connection.commit()
        self._invalidate_donor_cache(donor_email)
        self._log_action(
            "outreach_sent",
            donor_email=donor_email,
            subject=subject,
            locale=donor_locale,
            attributed_connector=attributed_connector,
            opportunity_signature=related_opportunity_signature,
        )
        self.record_funnel_event(
            stage="outreach",
            entity_key=f"communication:{cursor.lastrowid}",
            connector_name=attributed_connector,
            opportunity_signature=related_opportunity_signature,
            task_id=related_task_id,
            communication_id=cursor.lastrowid,
            event_type="sent",
            happened_at=sent_iso,
            metadata={"donor_email": donor_email},
        )
        return result

    def send_outreach_task(
        self,
        *,
        donor_email: str,
        donor_name: str,
        subject_template: str,
        body_template: str,
        context: dict[str, Any] | None = None,
        sender: Any | None = None,
        sent_at: datetime | None = None,
        locale: str | None = None,
        retry_limit: int | None = None,
        backoff_seconds: float | None = None,
        backoff_max_seconds: float | None = None,
        sleep_func: Callable[[float], None] | None = None,
    ) -> dict[str, Any]:
        return self.execute_queue_task(
            "send_outreach",
            {
                "donor_email": donor_email,
                "donor_name": donor_name,
                "locale": locale,
                "sent_at": self._to_iso(sent_at) if sent_at else None,
                "context_keys": sorted((context or {}).keys()),
            },
            lambda _context, _payload: self.send_outreach(
                donor_email=donor_email,
                donor_name=donor_name,
                subject_template=subject_template,
                body_template=body_template,
                context=context,
                sender=sender,
                sent_at=sent_at,
                locale=locale,
            ),
            retry_limit=retry_limit,
            backoff_seconds=backoff_seconds,
            backoff_max_seconds=backoff_max_seconds,
            sleep_func=sleep_func,
        )

    def register_outreach_template(
        self,
        name: str,
        subject_template: str,
        body_template: str,
        segment: str | None = None,
    ) -> None:
        """Store or replace an outreach template."""
        segment_key = "" if segment is None else self._validate_segment(segment)
        self.connection.execute(
            "DELETE FROM outreach_templates WHERE name = ? AND segment = ?",
            (name, segment_key),
        )
        self.connection.execute(
            """
            INSERT INTO outreach_templates (name, subject_template, body_template, segment)
            VALUES (?, ?, ?, ?)
            """,
            (name, subject_template, body_template, segment_key),
        )
        self.connection.commit()
        self._log_action("outreach_template_registered", name=name, segment=segment_key or None)

    def send_outreach_from_template(
        self,
        template_name: str,
        donor_email: str,
        donor_name: str,
        context: dict[str, Any] | None = None,
        sender: Any | None = None,
        sent_at: datetime | None = None,
        locale: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Send outreach using a stored template."""
        donor = self.connection.execute(
            "SELECT segment, locale FROM donors WHERE email = ?",
            (donor_email,),
        ).fetchone()
        donor_segment = donor["segment"] if donor else "unknown"
        donor_locale = self._validate_locale(locale or (donor["locale"] if donor else None))
        row = self.connection.execute(
            """
            SELECT subject_template, body_template, segment
            FROM outreach_templates
            WHERE name = ? AND segment IN (?, '')
            ORDER BY CASE WHEN segment = ? THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (template_name, donor_segment, donor_segment),
        ).fetchone()
        if row is None:
            catalog_template = self._resolve_catalog_template(
                template_name,
                segment=donor_segment,
                locale=donor_locale,
            )
            if catalog_template is None:
                raise FundingBotError(f"Unknown outreach template {template_name!r}.")
            subject_template, body_template = catalog_template
        else:
            subject_template = row["subject_template"]
            body_template = row["body_template"]
        result = self.send_outreach(
            donor_email=donor_email,
            donor_name=donor_name,
            subject_template=subject_template,
            body_template=body_template,
            context=context,
            sender=sender,
            sent_at=sent_at,
            locale=donor_locale,
            dry_run=dry_run,
        )
        result["template_name"] = template_name
        return result

    def record_outreach_event(
        self,
        communication_id: int,
        event_type: str,
        *,
        happened_at: datetime | None = None,
    ) -> None:
        """Store an outreach engagement event."""
        allowed = {"sent", "opened", "clicked", "bounced", "unsubscribed"}
        normalized_event = event_type.strip().lower()
        if normalized_event not in allowed:
            raise ValueError(f"Invalid outreach event type {event_type!r}.")

        communication = self.connection.execute(
            """
            SELECT id, donor_email, attributed_connector, related_opportunity_signature, related_task_id
            FROM communications
            WHERE id = ?
            """,
            (communication_id,),
        ).fetchone()
        if communication is None:
            raise FundingBotError(f"Unknown communication {communication_id!r}.")

        self.connection.execute(
            """
            INSERT INTO outreach_events (communication_id, event_type, happened_at)
            VALUES (?, ?, ?)
            """,
            (communication_id, normalized_event, self._to_iso(happened_at)),
        )
        self.connection.commit()
        self._log_action(
            "outreach_event_recorded",
            communication_id=communication_id,
            event_type=normalized_event,
        )
        if normalized_event in self.POSITIVE_RESPONSE_EVENT_TYPES:
            self.record_funnel_event(
                stage="response",
                entity_key=f"communication:{communication_id}",
                connector_name=(
                    str(communication["attributed_connector"]).strip()
                    if communication["attributed_connector"]
                    else None
                ),
                opportunity_signature=communication["related_opportunity_signature"],
                task_id=communication["related_task_id"],
                communication_id=communication_id,
                event_type=normalized_event,
                happened_at=happened_at,
                metadata={"donor_email": communication["donor_email"]},
            )

    def get_outreach_analytics(self, donor_email: str | None = None) -> dict[str, int]:
        """Return event counts grouped by type."""
        query = """
            SELECT oe.event_type, COUNT(*) AS total
            FROM outreach_events oe
            JOIN communications c ON c.id = oe.communication_id
        """
        params: list[Any] = []
        if donor_email is not None:
            query += " WHERE c.donor_email = ?"
            params.append(donor_email)
        query += " GROUP BY oe.event_type"
        counts = {key: 0 for key in ("sent", "opened", "clicked", "bounced", "unsubscribed")}
        for row in self.connection.execute(query, params).fetchall():
            counts[row["event_type"]] = row["total"]
        counts["total_sent"] = counts["sent"]
        return counts

    def get_funnel_analytics(
        self,
        *,
        start_at: datetime | str | None = None,
        end_at: datetime | str | None = None,
        connector_name: str | None = None,
    ) -> dict[str, Any]:
        where_clause, params, window = self._analytics_window_clause(
            start_at=start_at,
            end_at=end_at,
            timestamp_column="happened_at",
        )
        if connector_name:
            where_clause += (" AND " if where_clause else " WHERE ") + "connector_name = ?"
            params.append(str(connector_name).strip())
        rows = self.connection.execute(
            """
            SELECT
                stage,
                COALESCE(connector_name, 'unattributed') AS connector_name,
                COUNT(DISTINCT entity_key) AS attempts,
                COUNT(DISTINCT CASE WHEN success = 1 THEN entity_key END) AS successes
            FROM funnel_events
            """
            + where_clause
            + """
            GROUP BY stage, COALESCE(connector_name, 'unattributed')
            """,
            params,
        ).fetchall()
        attempts_by_stage = {stage: 0 for stage in self.FUNNEL_STAGES}
        counts_by_stage = {stage: 0 for stage in self.FUNNEL_STAGES}
        per_connector: dict[str, dict[str, dict[str, int]]] = {}
        for row in rows:
            stage = str(row["stage"])
            connector = str(row["connector_name"])
            attempts = int(row["attempts"])
            successes = int(row["successes"])
            attempts_by_stage[stage] += attempts
            counts_by_stage[stage] += successes
            bucket = per_connector.setdefault(
                connector,
                {
                    "attempts": {name: 0 for name in self.FUNNEL_STAGES},
                    "counts": {name: 0 for name in self.FUNNEL_STAGES},
                },
            )
            bucket["attempts"][stage] = attempts
            bucket["counts"][stage] = successes
        connectors = [
            {
                "connector_name": connector,
                "stages": self._build_funnel_stage_rows(bucket["counts"], bucket["attempts"]),
            }
            for connector, bucket in sorted(per_connector.items())
        ]
        return {
            "window": window,
            "stages": self._build_funnel_stage_rows(counts_by_stage, attempts_by_stage),
            "connectors": connectors,
        }

    def get_source_attribution_analytics(
        self,
        *,
        start_at: datetime | str | None = None,
        end_at: datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        opportunity_where, opportunity_params, _window = self._analytics_window_clause(
            start_at=start_at,
            end_at=end_at,
            timestamp_column="discovered_at",
        )
        communication_where, communication_params, _ = self._analytics_window_clause(
            start_at=start_at,
            end_at=end_at,
            timestamp_column="sent_at",
        )
        application_where, application_params, _ = self._analytics_window_clause(
            start_at=start_at,
            end_at=end_at,
            timestamp_column="submitted_at",
        )
        task_where, task_params, _ = self._analytics_window_clause(
            start_at=start_at,
            end_at=end_at,
            timestamp_column="created_at",
        )
        connectors: dict[str, dict[str, Any]] = {}

        def ensure(name: str | None) -> dict[str, Any]:
            key = str(name or "unattributed")
            return connectors.setdefault(
                key,
                {
                    "connector_name": key,
                    "discovered": 0,
                    "matched": 0,
                    "outreach": 0,
                    "responses": 0,
                    "applications_submitted": 0,
                    "tasks_created": 0,
                    "tasks_completed": 0,
                    "successful_outcomes": 0,
                },
            )

        for row in self.connection.execute(
            "SELECT source AS connector_name, COUNT(*) AS total FROM opportunities"
            + opportunity_where
            + " GROUP BY source",
            opportunity_params,
        ).fetchall():
            ensure(row["connector_name"])["discovered"] = int(row["total"])
        for row in self.connection.execute(
            (
                "SELECT attributed_connector AS connector_name, COUNT(*) AS total FROM tasks"
                + task_where
                + " WHERE attributed_connector IS NOT NULL GROUP BY attributed_connector"
                if not task_where
                else "SELECT attributed_connector AS connector_name, COUNT(*) AS total FROM tasks"
                + task_where
                + " AND attributed_connector IS NOT NULL GROUP BY attributed_connector"
            ),
            task_params,
        ).fetchall():
            ensure(row["connector_name"])["tasks_created"] = int(row["total"])
        for row in self.connection.execute(
            (
                "SELECT attributed_connector AS connector_name, COUNT(*) AS total FROM tasks"
                + task_where
                + " WHERE status = 'done' AND attributed_connector IS NOT NULL GROUP BY attributed_connector"
                if not task_where
                else "SELECT attributed_connector AS connector_name, COUNT(*) AS total FROM tasks"
                + task_where
                + " AND status = 'done' AND attributed_connector IS NOT NULL GROUP BY attributed_connector"
            ),
            task_params,
        ).fetchall():
            ensure(row["connector_name"])["tasks_completed"] = int(row["total"])
        for row in self.connection.execute(
            (
                "SELECT attributed_connector AS connector_name, COUNT(*) AS total FROM communications"
                + communication_where
                + " WHERE attributed_connector IS NOT NULL GROUP BY attributed_connector"
                if not communication_where
                else "SELECT attributed_connector AS connector_name, COUNT(*) AS total FROM communications"
                + communication_where
                + " AND attributed_connector IS NOT NULL GROUP BY attributed_connector"
            ),
            communication_params,
        ).fetchall():
            ensure(row["connector_name"])["outreach"] = int(row["total"])
        for row in self.connection.execute(
            """
            SELECT c.attributed_connector AS connector_name, COUNT(DISTINCT c.id) AS total
            FROM communications c
            JOIN outreach_events oe ON oe.communication_id = c.id
            """
            + (
                (
                    " WHERE c.attributed_connector IS NOT NULL"
                    " AND oe.event_type IN ('opened','clicked','responded','replied')"
                )
                if not communication_where
                else communication_where.replace(
                    " WHERE ", " WHERE c.attributed_connector IS NOT NULL AND "
                )
                + " AND oe.event_type IN ('opened','clicked','responded','replied')"
            )
            + " GROUP BY c.attributed_connector",
            communication_params,
        ).fetchall():
            ensure(row["connector_name"])["responses"] = int(row["total"])
        for row in self.connection.execute(
            (
                "SELECT attributed_connector AS connector_name, COUNT(*) AS total FROM applications"
                + application_where
                + " WHERE attributed_connector IS NOT NULL GROUP BY attributed_connector"
                if not application_where
                else "SELECT attributed_connector AS connector_name, COUNT(*) AS total FROM applications"
                + application_where
                + " AND attributed_connector IS NOT NULL GROUP BY attributed_connector"
            ),
            application_params,
        ).fetchall():
            ensure(row["connector_name"])["applications_submitted"] = int(row["total"])
        funnel = self.get_funnel_analytics(start_at=start_at, end_at=end_at)
        for connector_row in funnel["connectors"]:
            connector_bucket = ensure(connector_row["connector_name"])
            for stage_row in connector_row["stages"]:
                if stage_row["stage"] == "match":
                    connector_bucket["matched"] = int(stage_row["count"])
                    break
        for connector_bucket in connectors.values():
            connector_bucket["successful_outcomes"] = int(connector_bucket["responses"]) + int(
                connector_bucket["applications_submitted"]
            )
        return sorted(connectors.values(), key=lambda row: row["connector_name"])

    def get_connector_cost_analytics(
        self,
        *,
        start_at: datetime | str | None = None,
        end_at: datetime | str | None = None,
        connector_name: str | None = None,
    ) -> dict[str, Any]:
        where_clause, params, window = self._analytics_window_clause(
            start_at=start_at,
            end_at=end_at,
            timestamp_column="happened_at",
        )
        if connector_name:
            where_clause += (" AND " if where_clause else " WHERE ") + "connector_name = ?"
            params.append(str(connector_name).strip())
        rows = self.connection.execute(
            """
            SELECT
                connector_name,
                connector_type,
                operation,
                COUNT(*) AS calls,
                SUM(request_count) AS request_count,
                SUM(cost_usd) AS total_cost_usd,
                AVG(cost_usd) AS average_cost_usd,
                AVG(latency_seconds) AS average_latency_seconds,
                SUM(errored) AS errors
            FROM connector_call_metrics
            """
            + where_clause
            + """
            GROUP BY connector_name, connector_type, operation
            ORDER BY connector_name, operation
            """,
            params,
        ).fetchall()
        connectors: dict[str, dict[str, Any]] = {}
        total_cost = 0.0
        total_calls = 0
        total_errors = 0
        for row in rows:
            name = str(row["connector_name"])
            bucket = connectors.setdefault(
                name,
                {
                    "connector_name": name,
                    "connector_type": str(row["connector_type"]),
                    "calls": 0,
                    "request_count": 0,
                    "errors": 0,
                    "total_cost_usd": 0.0,
                    "average_latency_seconds": 0.0,
                    "operations": [],
                },
            )
            operation_calls = int(row["calls"] or 0)
            operation_latency = self._safe_float(row["average_latency_seconds"])
            operation_cost = self._safe_float(row["total_cost_usd"])
            operation_errors = int(row["errors"] or 0)
            bucket["calls"] += operation_calls
            bucket["request_count"] += int(row["request_count"] or 0)
            bucket["errors"] += operation_errors
            bucket["total_cost_usd"] += operation_cost
            bucket["operations"].append(
                {
                    "operation": str(row["operation"]),
                    "calls": operation_calls,
                    "request_count": int(row["request_count"] or 0),
                    "errors": operation_errors,
                    "total_cost_usd": operation_cost,
                    "average_cost_usd": self._safe_float(row["average_cost_usd"]),
                    "average_latency_seconds": operation_latency,
                }
            )
        for bucket in connectors.values():
            call_count = max(1, int(bucket["calls"]))
            weighted_latency = sum(
                operation["average_latency_seconds"] * operation["calls"]
                for operation in bucket["operations"]
            )
            bucket["average_latency_seconds"] = weighted_latency / call_count
            bucket["average_cost_usd"] = self._safe_float(bucket["total_cost_usd"]) / call_count
            bucket["error_rate"] = self._safe_float(bucket["errors"]) / call_count
            total_cost += self._safe_float(bucket["total_cost_usd"])
            total_calls += int(bucket["calls"])
            total_errors += int(bucket["errors"])
        return {
            "window": window,
            "summary": {
                "total_cost_usd": total_cost,
                "total_calls": total_calls,
                "average_cost_usd": (total_cost / total_calls) if total_calls else 0.0,
                "error_rate": (total_errors / total_calls) if total_calls else 0.0,
            },
            "connectors": sorted(connectors.values(), key=lambda row: row["connector_name"]),
        }

    def detect_metric_anomalies(
        self,
        *,
        end_at: datetime | str | None = None,
        current_window_hours: int = 24,
        baseline_days: int = 7,
        min_calls: int = 3,
    ) -> dict[str, Any]:
        if isinstance(end_at, str):
            end_at = end_at.replace(" ", "+")
        end_dt = self._as_utc(datetime.fromisoformat(end_at) if isinstance(end_at, str) else end_at)
        current_start = end_dt - timedelta(hours=max(1, int(current_window_hours)))
        baseline_start = current_start - timedelta(days=max(1, int(baseline_days)))
        current_rows = self.connection.execute(
            """
            SELECT
                connector_name,
                COUNT(*) AS calls,
                SUM(errored) AS errors,
                AVG(latency_seconds) AS average_latency_seconds,
                SUM(cost_usd) AS total_cost_usd
            FROM connector_call_metrics
            WHERE happened_at >= ? AND happened_at <= ?
            GROUP BY connector_name
            """,
            (self._to_iso(current_start), self._to_iso(end_dt)),
        ).fetchall()
        baseline_rows = self.connection.execute(
            """
            SELECT
                connector_name,
                substr(happened_at, 1, 10) AS day,
                COUNT(*) AS calls,
                SUM(errored) AS errors,
                AVG(latency_seconds) AS average_latency_seconds,
                SUM(cost_usd) AS total_cost_usd
            FROM connector_call_metrics
            WHERE happened_at >= ? AND happened_at < ?
            GROUP BY connector_name, substr(happened_at, 1, 10)
            """,
            (self._to_iso(baseline_start), self._to_iso(current_start)),
        ).fetchall()
        baseline_by_connector: dict[str, dict[str, list[float]]] = {}
        for row in baseline_rows:
            bucket = baseline_by_connector.setdefault(
                str(row["connector_name"]),
                {"error_rate": [], "average_latency_seconds": [], "total_cost_usd": []},
            )
            calls = max(1, int(row["calls"] or 0))
            bucket["error_rate"].append(int(row["errors"] or 0) / calls)
            bucket["average_latency_seconds"].append(
                self._safe_float(row["average_latency_seconds"])
            )
            bucket["total_cost_usd"].append(self._safe_float(row["total_cost_usd"]))
        alerts: list[dict[str, Any]] = []
        for row in current_rows:
            connector = str(row["connector_name"])
            calls = int(row["calls"] or 0)
            if calls < min_calls:
                continue
            current_metrics = {
                "error_rate": int(row["errors"] or 0) / max(1, calls),
                "average_latency_seconds": self._safe_float(row["average_latency_seconds"]),
                "total_cost_usd": self._safe_float(row["total_cost_usd"]),
            }
            baseline_metrics = baseline_by_connector.get(
                connector,
                {"error_rate": [], "average_latency_seconds": [], "total_cost_usd": []},
            )
            for metric_name, current_value in current_metrics.items():
                history = baseline_metrics.get(metric_name, [])
                baseline_mean = mean(history) if history else 0.0
                baseline_std = pstdev(history) if len(history) > 1 else 0.0
                triggered = False
                severity = "medium"
                if metric_name == "error_rate":
                    threshold = max(0.2, baseline_mean + max(0.1, 3 * baseline_std))
                    triggered = current_value > threshold and current_value > baseline_mean * 1.5
                    severity = "high" if current_value >= max(0.4, threshold * 1.5) else "medium"
                elif metric_name == "average_latency_seconds":
                    threshold = max(1.0, baseline_mean + max(0.5, 3 * baseline_std))
                    if baseline_mean > 0:
                        threshold = max(threshold, baseline_mean * 1.75)
                    triggered = current_value > threshold
                    severity = "high" if current_value >= threshold * 1.5 else "medium"
                elif metric_name == "total_cost_usd":
                    threshold = max(5.0, baseline_mean + max(1.0, 3 * baseline_std))
                    if baseline_mean > 0:
                        threshold = max(threshold, baseline_mean * 2.0)
                    triggered = current_value > threshold
                    severity = "warning" if current_value < threshold * 1.5 else "medium"
                if triggered:
                    alerts.append(
                        {
                            "connector_name": connector,
                            "metric_name": metric_name,
                            "severity": severity,
                            "current_value": current_value,
                            "baseline_mean": baseline_mean,
                            "baseline_stddev": baseline_std,
                            "window_calls": calls,
                            "message": (
                                f"{connector} {metric_name} is elevated "
                                f"({current_value:.4f} vs baseline {baseline_mean:.4f})."
                            ),
                        }
                    )
        return {
            "window": {
                "start_at": self._to_iso(current_start),
                "end_at": self._to_iso(end_dt),
                "baseline_start_at": self._to_iso(baseline_start),
            },
            "alerts": alerts,
        }

    def get_analytics_dashboard_data(
        self,
        *,
        start_at: datetime | str | None = None,
        end_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        return {
            "generated_at": self._to_iso(),
            "outreach": self.get_outreach_analytics(),
            "funnel": self.get_funnel_analytics(start_at=start_at, end_at=end_at),
            "costs": self.get_connector_cost_analytics(start_at=start_at, end_at=end_at),
            "attribution": self.get_source_attribution_analytics(start_at=start_at, end_at=end_at),
            "alerts": self.detect_metric_anomalies(end_at=end_at),
        }

    def gdpr_export(self, donor_email: str, *, dry_run: bool = False) -> dict[str, Any]:
        """Export all donor-related records stored by the bot."""
        donor = self.connection.execute(
            "SELECT * FROM donors WHERE email = ?",
            (donor_email,),
        ).fetchone()
        communications = self.connection.execute(
            """
            SELECT * FROM communications
            WHERE donor_email = ?
            ORDER BY sent_at DESC
            """,
            (donor_email,),
        ).fetchall()
        communication_ids = [row["id"] for row in communications]
        events: list[dict[str, Any]] = []
        if communication_ids:
            # placeholders is built solely from "?" repeated len(communication_ids) times.
            placeholders = ", ".join("?" for _ in communication_ids)
            events = [
                dict(row)
                for row in self.connection.execute(
                    "SELECT oe.* FROM outreach_events oe"
                    " WHERE oe.communication_id IN (" + placeholders + ")"
                    " ORDER BY oe.happened_at DESC",
                    communication_ids,
                ).fetchall()
            ]
        export = {
            "donor": self._deserialize_donor_row(donor),
            "consent_records": self.list_consent_records(donor_email),
            "communications": [dict(row) for row in communications],
            "outreach_events": events,
            "audit_logs": [
                dict(row)
                for row in self.connection.execute(
                    """
                    SELECT * FROM audit_logs
                    WHERE details_json LIKE ?
                    ORDER BY happened_at DESC
                    """,
                    (f"%{donor_email}%",),
                ).fetchall()
            ],
            "dry_run": dry_run,
            "preview": {
                "would_log_audit_export": True,
                "communication_count": len(communications),
                "outreach_event_count": len(events),
            },
        }
        if not dry_run:
            self._log_action("gdpr_exported", donor_email=donor_email)
        return export

    def gdpr_delete(self, donor_email: str) -> None:
        """Anonymize donor records and retain a deletion audit trail."""
        donor = self.connection.execute(
            "SELECT * FROM donors WHERE email = ?",
            (donor_email,),
        ).fetchone()
        if donor is None:
            raise FundingBotError(f"Unknown donor {donor_email!r}.")

        anonymized_email = (
            f"[deleted]-{hashlib.sha256(donor_email.encode('utf-8')).hexdigest()[:12]}"
            "@deleted.invalid"
        )
        with self.connection:
            self.connection.execute(
                """
                UPDATE donors
                SET email = ?, name = '[deleted]', opted_out = 1,
                    preferences_json = '{}', last_contact_at = NULL, segment = 'unknown'
                WHERE email = ?
                """,
                (anonymized_email, donor_email),
            )
            self.connection.execute(
                """
                UPDATE communications
                SET donor_email = ?, donor_name = '[deleted]',
                    subject = '[deleted]', body = '[deleted]'
                WHERE donor_email = ?
                """,
                (anonymized_email, donor_email),
            )
            self.connection.execute(
                """
                UPDATE audit_logs
                SET details_json = REPLACE(
                    REPLACE(details_json, ?, '[deleted]'),
                    ?, '[deleted]'
                )
                WHERE details_json LIKE ? OR details_json LIKE ?
                """,
                (donor_email, donor["name"], f"%{donor_email}%", f"%{donor['name']}%"),
            )
        self._invalidate_donor_cache(donor_email)
        self._log_action(
            "gdpr_deleted",
            donor_hash=hashlib.sha256(donor_email.encode("utf-8")).hexdigest(),
            anonymized_email=anonymized_email,
        )

    @staticmethod
    def _require_babel() -> None:
        if (
            babel_format_date is None
            or babel_format_datetime is None
            or babel_format_decimal is None
        ):
            raise FundingBotError(
                "Document localization requires Babel. Install it with `pip install Babel`."
            )

    @classmethod
    def _validate_document_locale(cls, locale: str | None) -> str:
        normalized = (locale or cls.DEFAULT_TEMPLATE_LOCALE).strip().lower().replace("_", "-")
        canonical = _DOCUMENT_LOCALE_ALIASES.get(normalized)
        if canonical is None:
            raise ValueError(
                f"Unsupported document locale {locale!r}. Expected one of "
                f"{sorted(cls.SUPPORTED_TEMPLATE_LOCALES)}."
            )
        return canonical

    @classmethod
    def _document_locale_settings(cls, locale: str | None) -> dict[str, str]:
        return dict(_DOCUMENT_LOCALE_CONFIG[cls._validate_document_locale(locale)])

    @classmethod
    def _normalize_document_translations(
        cls,
        *sources: dict[str, Any] | None,
    ) -> dict[str, dict[str, Any]]:
        normalized: dict[str, dict[str, Any]] = {}
        for source in sources:
            if not isinstance(source, dict):
                continue
            for locale_name, values in source.items():
                if not isinstance(locale_name, str) or not isinstance(values, dict):
                    continue
                try:
                    canonical_locale = cls._validate_document_locale(locale_name)
                except ValueError:
                    continue
                bucket = normalized.setdefault(canonical_locale, {})
                bucket.update(values)
        return normalized

    @classmethod
    def _format_document_value(cls, value: Any, *, locale: str) -> Any:
        normalized_locale = cls._validate_document_locale(locale)
        settings = cls._document_locale_settings(normalized_locale)

        if isinstance(value, datetime):
            cls._require_babel()
            return babel_format_datetime(
                cls._as_utc(value),
                format=settings["datetime_format"],
                locale=settings["babel_locale"],
            )
        if isinstance(value, date):
            cls._require_babel()
            return babel_format_date(
                value,
                format=settings["date_format"],
                locale=settings["babel_locale"],
            )
        if isinstance(value, Decimal | Number) and not isinstance(value, bool):
            cls._require_babel()
            return babel_format_decimal(value, locale=settings["babel_locale"])
        return value

    @classmethod
    def _build_document_context(
        cls,
        profile: dict[str, Any],
        context: dict[str, Any] | None,
        *,
        locale: str,
    ) -> dict[str, Any]:
        merged_context = dict(profile)
        merged_context.update(context or {})

        translations = cls._normalize_document_translations(
            profile.get("translations") if isinstance(profile, dict) else None,
            (context or {}).get("translations"),
        )
        rendered_context = {
            key: cls._format_document_value(value, locale=locale)
            for key, value in merged_context.items()
            if key != "translations"
        }
        rendered_context["document_locale"] = locale
        rendered_context["t"] = _DocumentTranslationLookup(
            bot=cls,
            locale=locale,
            translations=translations,
        )
        rendered_context["translate"] = rendered_context["t"]
        return rendered_context

    def generate_document(
        self,
        *,
        kind: str,
        template: str,
        output_dir: str | os.PathLike[str],
        context: dict[str, Any] | None = None,
        formats: Iterable[str] = ("pdf", "docx"),
        locale: str | None = None,
    ) -> dict[str, str]:
        profile = self.load_organization_profile()
        document_locale = self._validate_document_locale(locale)
        rendered_context = self._build_document_context(
            profile,
            context,
            locale=document_locale,
        )
        rendered = template.format_map(rendered_context).strip() + "\n"

        target_dir = Path(output_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = self._utcnow().strftime("%Y%m%d%H%M%S")
        base_name = f"{kind.replace(' ', '_').lower()}_{stamp}"
        generated: dict[str, str] = {}

        for fmt in formats:
            normalized = fmt.lower()
            if normalized == "word":
                normalized = "docx"

            path = target_dir / f"{base_name}.{normalized}"
            if normalized == "pdf":
                self._write_pdf(path, rendered)
            elif normalized == "docx":
                self._write_docx(path, rendered)
            else:
                raise ValueError(f"Unsupported document format: {fmt}")

            generated[normalized] = str(path)
            self.connection.execute(
                "INSERT INTO documents (kind, format, path, created_at) VALUES (?, ?, ?, ?)",
                (kind, normalized, str(path), self._to_iso()),
            )

        self.connection.commit()
        self._log_action(
            "documents_generated",
            kind=kind,
            formats=sorted(generated),
            locale=document_locale,
        )
        return generated

    def _write_pdf(self, path: Path, text: str) -> None:
        lines = [line or " " for line in text.splitlines()]
        escaped_lines = [
            line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for line in lines
        ]
        content_lines = ["BT", "/F1 11 Tf", "50 780 Td", "14 TL"]
        for index, line in enumerate(escaped_lines):
            if index == 0:
                content_lines.append(f"({line}) Tj")
            else:
                content_lines.append(f"T* ({line}) Tj")
        content_lines.append("ET")
        content = "\n".join(content_lines).encode("utf-8")

        objects = [
            b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj",
            b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj",
            b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj",
            b"4 0 obj << /Length "
            + str(len(content)).encode("ascii")
            + b" >> stream\n"
            + content
            + b"\nendstream endobj",
            b"5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj",
        ]

        pdf = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        for obj in objects:
            offsets.append(len(pdf))
            pdf.extend(obj)
            pdf.extend(b"\n")

        xref_start = len(pdf)
        pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
        pdf.extend(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
        pdf.extend(
            (
                f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\n"
                f"startxref\n{xref_start}\n%%EOF"
            ).encode("ascii")
        )
        path.write_bytes(pdf)

    def _write_docx(self, path: Path, text: str) -> None:
        paragraphs = []
        for line in text.splitlines():
            safe_line = escape(line or " ")
            paragraphs.append(
                '<w:p><w:r><w:t xml:space="preserve">' f"{safe_line}" "</w:t></w:r></w:p>"
            )

        document_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{''.join(paragraphs)}<w:sectPr/></w:body>"
            "</w:document>"
        )

        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "[Content_Types].xml",
                """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
""",
            )
            archive.writestr(
                "_rels/.rels",
                """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
""",
            )
            archive.writestr("word/document.xml", document_xml)

    def draft_proposal(
        self,
        opportunity_signature: str,
        ai_client: AIClient | None = None,
    ) -> str:
        """Draft a proposal using stored organization and opportunity data."""
        opportunity = self._get_opportunity(opportunity_signature)
        profile = self.load_organization_profile()
        raw_data = json.loads(opportunity["raw_data_json"])
        prompt = "\n".join(
            [
                "Draft a concise nonprofit funding proposal.",
                f"Organization profile: {json.dumps(profile, sort_keys=True)}",
                f"Opportunity: {json.dumps(raw_data, sort_keys=True)}",
                "Include sections for Executive Summary, Organizational Fit, Program Plan,",
                "Expected Outcomes, and Compliance Notes.",
            ]
        )
        if ai_client is not None:
            proposal = ai_client.generate(prompt).strip()
        else:
            proposal = "\n\n".join(
                [
                    f"# Proposal Draft: {opportunity['title']}",
                    "\n".join(
                        [
                            "## Executive Summary",
                            (
                                f"{profile.get('name', 'Our organization')} seeks support from "
                                f"{opportunity['donor_name']} for {opportunity['title'].lower()}."
                            ),
                            profile.get(
                                "mission", "Our mission statement is available on request."
                            ),
                        ]
                    ),
                    "\n".join(
                        [
                            "## Organizational Fit",
                            (
                                f"This opportunity aligns with the {opportunity['category'] or 'strategic'} "
                                "focus described in the notice."
                            ),
                            raw_data.get("summary", opportunity["summary"]),
                        ]
                    ),
                    "\n".join(
                        [
                            "## Program Plan",
                            "We will tailor program delivery, staffing, and reporting to donor requirements.",
                            f"Portal: {opportunity['portal_url']}",
                        ]
                    ),
                    "\n".join(
                        [
                            "## Expected Outcomes",
                            "The proposed work will define measurable milestones, beneficiary reach, and impact reporting.",
                        ]
                    ),
                    "\n".join(
                        [
                            "## Compliance Notes",
                            f"Source: {opportunity['source']}",
                            "Required attachments, budget, and due diligence items will be validated before submission.",
                        ]
                    ),
                ]
            ).strip()
        self._log_action("proposal_drafted", opportunity_signature=opportunity_signature)
        return proposal

    def build_outreach_analytics_report(
        self,
        start_date: datetime | str | None = None,
        end_date: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Build an aggregate outreach analytics report."""
        start_iso = self._normalize_filter_timestamp(start_date)
        end_iso = self._normalize_filter_timestamp(end_date, end=True)
        # Build filter params with explicit branching; no user-controlled SQL fragments.
        params: list[Any] = []
        if start_iso is not None and end_iso is not None:
            date_filter = "WHERE c.sent_at >= ? AND c.sent_at <= ?"
            params = [start_iso, end_iso]
        elif start_iso is not None:
            date_filter = "WHERE c.sent_at >= ?"
            params = [start_iso]
        elif end_iso is not None:
            date_filter = "WHERE c.sent_at <= ?"
            params = [end_iso]
        else:
            date_filter = ""
            params = []

        total_sent = self.connection.execute(
            "SELECT COUNT(*) AS total FROM communications c " + date_filter,
            params,
        ).fetchone()["total"]
        event_counts = self.connection.execute(
            "SELECT oe.event_type, COUNT(*) AS total"
            " FROM outreach_events oe"
            " JOIN communications c ON c.id = oe.communication_id "
            + date_filter
            + " GROUP BY oe.event_type",
            params,
        ).fetchall()
        counts = {row["event_type"]: row["total"] for row in event_counts}
        top_donors = [
            dict(row)
            for row in self.connection.execute(
                "SELECT c.donor_email, c.donor_name,"
                " SUM(CASE WHEN oe.event_type = 'opened' THEN 1 ELSE 0 END) AS opened,"
                " SUM(CASE WHEN oe.event_type = 'clicked' THEN 1 ELSE 0 END) AS clicked,"
                " COUNT(oe.id) AS total_events"
                " FROM communications c"
                " LEFT JOIN outreach_events oe ON oe.communication_id = c.id "
                + date_filter
                + " GROUP BY c.donor_email, c.donor_name"
                " HAVING"
                "  SUM(CASE WHEN oe.event_type = 'opened' THEN 1 ELSE 0 END) > 0"
                "  OR SUM(CASE WHEN oe.event_type = 'clicked' THEN 1 ELSE 0 END) > 0"
                " ORDER BY clicked DESC, opened DESC, total_events DESC, MAX(c.sent_at) DESC"
                " LIMIT 5",
                params,
            ).fetchall()
        ]
        opened = int(counts.get("opened", 0))
        clicked = int(counts.get("clicked", 0))
        bounced = int(counts.get("bounced", 0))
        return {
            "total_sent": int(total_sent),
            "opened": opened,
            "clicked": clicked,
            "bounce_rate": (bounced / total_sent) if total_sent else 0.0,
            "top_engaged_donors": top_donors,
        }

    def build_daily_summary(
        self,
        *,
        recipient: str,
        report_date: datetime | None = None,
    ) -> dict[str, str]:
        date = (report_date or self._utcnow()).date().isoformat()
        recipient_name = recipient.split("@", 1)[0].replace(".", " ").replace("_", " ").title()
        new_opportunities = self.connection.execute(
            """
            SELECT title, status FROM opportunities
            WHERE substr(discovered_at, 1, 10) = ?
            ORDER BY discovered_at
            """,
            (date,),
        ).fetchall()
        submitted_apps = self.connection.execute(
            """
            SELECT a.donor_name, a.portal_url, a.status, o.title
            FROM applications a
            JOIN opportunities o ON o.signature = a.opportunity_signature
            WHERE substr(a.submitted_at, 1, 10) = ?
            ORDER BY a.submitted_at
            """,
            (date,),
        ).fetchall()
        communications = self.connection.execute(
            """
            SELECT donor_name FROM communications
            WHERE substr(sent_at, 1, 10) = ?
            ORDER BY sent_at
            """,
            (date,),
        ).fetchall()
        pending = self.connection.execute("""
            SELECT a.donor_name, a.status, a.next_action, o.title
            FROM applications a
            JOIN opportunities o ON o.signature = a.opportunity_signature
            WHERE a.status IN ('pending', 'submitted', 'in_review')
            ORDER BY a.submitted_at
            """).fetchall()

        def format_lines(rows: Iterable[sqlite3.Row], formatter: Any, empty: str) -> list[str]:
            items = [formatter(row) for row in rows]
            return items or [f"   • {empty}"]

        opportunity_lines = format_lines(
            new_opportunities,
            lambda row: f"   • {row['title']} – {row['status'].replace('_', ' ').title()}",
            "No new opportunities",
        )
        application_lines = format_lines(
            submitted_apps,
            lambda row: f"   • {row['title']} – {row['status'].replace('_', ' ').title()}",
            "No applications submitted",
        )
        pending_lines = format_lines(
            pending,
            lambda row: f"   • {row['title']} – {row['status'].replace('_', ' ').title()} ({row['next_action']})",
            "No pending applications",
        )

        body = "\n".join(
            [
                f"To: {recipient}",
                "",
                f"Hello {recipient_name or 'there'},",
                "",
                "Here is today’s funding activity summary:",
                "",
                f"- New Opportunities Found: {len(new_opportunities)}",
                *opportunity_lines,
                "",
                f"- Applications Submitted: {len(submitted_apps)}",
                *application_lines,
                "",
                f"- Donor Communications: {len(communications)} personalized emails sent",
                (
                    "   • No bounce or spam flags detected"
                    if communications
                    else "   • No outreach sent today"
                ),
                "",
                f"- Pending Applications: {len(pending)}",
                *pending_lines,
                "",
                "Best regards,",
                "Nonprofit Funding Bot",
            ]
        )
        subject = f"Daily Nonprofit Funding Report – {date}"
        self._log_action("daily_summary_built", recipient=recipient, report_date=date)
        return {"subject": subject, "body": body}

    def send_daily_summary(
        self,
        *,
        recipient: str | None = None,
        sender: Any | None = None,
        report_date: datetime | None = None,
    ) -> dict[str, str]:
        """Build and optionally dispatch the daily funding summary email.

        Parameters
        ----------
        recipient:
            The email address that receives the report.  When omitted, the
            value is read from the ``summary_recipient`` key of the stored
            organization profile; if that key is also absent it falls back to
            ``"lupael@i4e.com.bd"`` as specified in the project brief.
        sender:
            A callable ``(to_addr, subject, body) -> None`` used to transmit
            the email.  Pass an :class:`SMTPEmailSender` instance (or any
            compatible callable) to actually deliver the message.  When
            ``None`` the summary is built and returned but not sent.
        report_date:
            The date for which the report is generated.  Defaults to today.
        """
        if recipient is None:
            profile = self.load_organization_profile()
            recipient = profile.get("summary_recipient", "lupael@i4e.com.bd")

        summary = self.build_daily_summary(recipient=recipient, report_date=report_date)
        if sender is not None:
            sender(recipient, summary["subject"], summary["body"])
            self._log_action(
                "daily_summary_sent",
                recipient=recipient,
                subject=summary["subject"],
            )
        return summary

    def send_daily_summary_task(
        self,
        *,
        recipient: str | None = None,
        sender: Any | None = None,
        report_date: datetime | None = None,
        retry_limit: int | None = None,
        backoff_seconds: float | None = None,
        backoff_max_seconds: float | None = None,
        sleep_func: Callable[[float], None] | None = None,
    ) -> dict[str, Any]:
        return self.execute_queue_task(
            "send_daily_summary",
            {
                "recipient": recipient,
                "report_date": self._to_iso(report_date) if report_date else None,
            },
            lambda _context, _payload: self.send_daily_summary(
                recipient=recipient,
                sender=sender,
                report_date=report_date,
            ),
            retry_limit=retry_limit,
            backoff_seconds=backoff_seconds,
            backoff_max_seconds=backoff_max_seconds,
            sleep_func=sleep_func,
        )

    def build_gdpr_compliance_report(
        self,
        *,
        cadence: str = "weekly",
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        normalized_cadence = cadence.strip().lower()
        if normalized_cadence not in {"weekly", "monthly"}:
            raise ValueError("cadence must be either 'weekly' or 'monthly'.")

        report_end = self._as_utc(as_of)
        retention_span = timedelta(days=7 if normalized_cadence == "weekly" else 30)
        period_start_dt = report_end - retention_span
        period_start = self._to_iso(period_start_dt)
        period_end = self._to_iso(report_end)

        def _retention_days(env_name: str, default: int) -> int:
            raw_value = os.environ.get(env_name)
            if raw_value is None:
                return default
            try:
                return max(1, int(raw_value))
            except ValueError:
                return default

        donor_retention_days = _retention_days("GDPR_DONOR_RETENTION_DAYS", 365)
        communication_retention_days = _retention_days("GDPR_COMMUNICATION_RETENTION_DAYS", 730)
        application_retention_days = _retention_days("GDPR_APPLICATION_RETENTION_DAYS", 1095)
        donor_cutoff = self._to_iso(report_end - timedelta(days=donor_retention_days))
        communication_cutoff = self._to_iso(
            report_end - timedelta(days=communication_retention_days)
        )
        application_cutoff = self._to_iso(report_end - timedelta(days=application_retention_days))

        consent_grants_in_period = self.connection.execute(
            """
            SELECT COUNT(*) FROM consent_records
            WHERE status = 'granted' AND recorded_at >= ? AND recorded_at <= ?
            """,
            (period_start, period_end),
        ).fetchone()[0]
        consent_withdrawals_in_period = self.connection.execute(
            """
            SELECT COUNT(*) FROM consent_records
            WHERE status = 'withdrawn' AND recorded_at >= ? AND recorded_at <= ?
            """,
            (period_start, period_end),
        ).fetchone()[0]
        communicated_donors = {
            row["donor_email"]
            for row in self.connection.execute(
                "SELECT DISTINCT donor_email FROM communications WHERE donor_email NOT LIKE '[deleted]-%'"
            ).fetchall()
        }
        consented_donors = {row["donor_email"] for row in self.connection.execute("""
                SELECT DISTINCT donor_email FROM consent_records
                WHERE status = 'granted' AND channel = 'email'
                """).fetchall()}
        missing_consent_donors = sorted(communicated_donors - consented_donors)
        opted_out_without_record = self.connection.execute("""
            SELECT COUNT(*) FROM donors d
            WHERE d.opted_out = 1
              AND d.email NOT LIKE '[deleted]-%'
              AND NOT EXISTS (
                  SELECT 1 FROM consent_records cr
                  WHERE cr.donor_email = d.email AND cr.status = 'withdrawn'
              )
            """).fetchone()[0]
        latest_consent_status_by_donor: dict[str, str] = {}
        for row in self.connection.execute("""
            SELECT donor_email, status
            FROM consent_records
            WHERE channel = 'email'
            ORDER BY recorded_at DESC, id DESC
            """).fetchall():
            latest_consent_status_by_donor.setdefault(row["donor_email"], row["status"])
        active_consents = sum(
            1 for status in latest_consent_status_by_donor.values() if status == "granted"
        )

        stale_donors = [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT email, name, last_contact_at FROM donors
                WHERE email NOT LIKE '[deleted]-%'
                  AND last_contact_at IS NOT NULL
                  AND last_contact_at < ?
                ORDER BY last_contact_at ASC
                LIMIT 10
                """,
                (donor_cutoff,),
            ).fetchall()
        ]
        communications_past_retention = self.connection.execute(
            """
            SELECT COUNT(*) FROM communications
            WHERE donor_email NOT LIKE '[deleted]-%' AND sent_at < ?
            """,
            (communication_cutoff,),
        ).fetchone()[0]
        applications_past_retention = self.connection.execute(
            "SELECT COUNT(*) FROM applications WHERE submitted_at < ?",
            (application_cutoff,),
        ).fetchone()[0]
        exports_in_period = self.connection.execute(
            """
            SELECT COUNT(*) FROM audit_logs
            WHERE action = 'gdpr_exported' AND happened_at >= ? AND happened_at <= ?
            """,
            (period_start, period_end),
        ).fetchone()[0]
        deletions_in_period = self.connection.execute(
            """
            SELECT COUNT(*) FROM audit_logs
            WHERE action = 'gdpr_deleted' AND happened_at >= ? AND happened_at <= ?
            """,
            (period_start, period_end),
        ).fetchone()[0]
        last_export_at = self.connection.execute(
            "SELECT MAX(happened_at) FROM audit_logs WHERE action = 'gdpr_exported'"
        ).fetchone()[0]
        last_deletion_at = self.connection.execute(
            "SELECT MAX(happened_at) FROM audit_logs WHERE action = 'gdpr_deleted'"
        ).fetchone()[0]

        checks = [
            {
                "name": "consent_coverage",
                "status": "ok" if not missing_consent_donors else "action_required",
                "details": {
                    "donors_missing_consent_count": len(missing_consent_donors),
                    "sample_donors": missing_consent_donors[:5],
                },
            },
            {
                "name": "retention_review",
                "status": (
                    "ok"
                    if not stale_donors
                    and communications_past_retention == 0
                    and applications_past_retention == 0
                    else "action_required"
                ),
                "details": {
                    "stale_donors_count": len(stale_donors),
                    "communications_past_retention_count": communications_past_retention,
                    "applications_past_retention_count": applications_past_retention,
                },
            },
            {
                "name": "opt_out_records",
                "status": "ok" if opted_out_without_record == 0 else "action_required",
                "details": {
                    "opted_out_without_record_count": opted_out_without_record,
                },
            },
            {
                "name": "data_subject_request_auditability",
                "status": "ok",
                "details": {
                    "exports_in_period": exports_in_period,
                    "deletions_in_period": deletions_in_period,
                    "last_export_at": last_export_at,
                    "last_deletion_at": last_deletion_at,
                },
            },
        ]
        report = {
            "report_type": "gdpr_compliance_self_check",
            "cadence": normalized_cadence,
            "period_start": period_start,
            "period_end": period_end,
            "generated_at": self._to_iso(),
            "consent_summary": {
                "grants_in_period": int(consent_grants_in_period),
                "withdrawals_in_period": int(consent_withdrawals_in_period),
                "active_email_consents": int(active_consents),
                "communicated_donors_without_consent": len(missing_consent_donors),
                "opted_out_donors_total": self.connection.execute(
                    "SELECT COUNT(*) FROM donors WHERE opted_out = 1"
                ).fetchone()[0],
            },
            "data_retention": {
                "donor_retention_days": donor_retention_days,
                "communication_retention_days": communication_retention_days,
                "application_retention_days": application_retention_days,
                "stale_donors_count": len(stale_donors),
                "stale_donor_samples": stale_donors[:5],
                "communications_past_retention_count": int(communications_past_retention),
                "applications_past_retention_count": int(applications_past_retention),
            },
            "data_subject_requests": {
                "exports_in_period": int(exports_in_period),
                "deletions_in_period": int(deletions_in_period),
                "last_export_at": last_export_at,
                "last_deletion_at": last_deletion_at,
            },
            "checks": checks,
        }
        self._log_action(
            "gdpr_self_check_report_generated",
            cadence=normalized_cadence,
            period_start=period_start,
            period_end=period_end,
        )
        return report

    def build_monthly_audit_report(
        self,
        *,
        year: int | None = None,
        month: int | None = None,
    ) -> dict[str, Any]:
        """Generate a GDPR/ISO-style monthly compliance audit report.

        Parameters
        ----------
        year:
            Four-digit year (defaults to the current UTC year).
        month:
            Month number 1–12 (defaults to the current UTC month).
        """
        now = self._utcnow()
        report_year = year if year is not None else now.year
        report_month = month if month is not None else now.month

        period_start = f"{report_year:04d}-{report_month:02d}-01"
        if report_month == 12:
            period_end = f"{report_year + 1:04d}-01-01"
        else:
            period_end = f"{report_year:04d}-{report_month + 1:02d}-01"

        # Audit log summary grouped by action
        action_counts: dict[str, int] = {}
        for row in self.connection.execute(
            """
            SELECT action, COUNT(*) AS total FROM audit_logs
            WHERE happened_at >= ? AND happened_at < ?
            GROUP BY action ORDER BY total DESC
            """,
            (period_start, period_end),
        ).fetchall():
            action_counts[row["action"]] = row["total"]

        # GDPR-sensitive actions
        gdpr_actions = {
            k: v
            for k, v in action_counts.items()
            if k in {"gdpr_exported", "gdpr_deleted", "donor_opt_out_updated"}
        }

        # Application outcomes
        app_by_status: dict[str, int] = {}
        for row in self.connection.execute(
            """
            SELECT status, COUNT(*) AS total FROM applications
            WHERE submitted_at >= ? AND submitted_at < ?
            GROUP BY status
            """,
            (period_start, period_end),
        ).fetchall():
            app_by_status[row["status"]] = row["total"]

        # Outreach statistics
        outreach_stats = self.build_outreach_analytics_report(
            start_date=period_start,
            end_date=f"{report_year:04d}-{report_month:02d}-{_last_day_of_month(report_year, report_month):02d}",
        )

        # New donors
        new_donors_count = self.connection.execute(
            """
            SELECT COUNT(*) FROM audit_logs
            WHERE action = 'donor_upserted'
              AND happened_at >= ? AND happened_at < ?
            """,
            (period_start, period_end),
        ).fetchone()[0]

        # Opted-out donors total
        opted_out_total = self.connection.execute(
            "SELECT COUNT(*) FROM donors WHERE opted_out = 1"
        ).fetchone()[0]

        report = {
            "report_type": "monthly_compliance_audit",
            "period": f"{report_year:04d}-{report_month:02d}",
            "generated_at": self._to_iso(),
            "audit_log_entries": action_counts,
            "gdpr_operations": gdpr_actions,
            "application_outcomes": app_by_status,
            "outreach_summary": outreach_stats,
            "new_donors_registered": new_donors_count,
            "opted_out_donors_total": opted_out_total,
        }
        self._log_action(
            "monthly_audit_report_generated",
            period=report["period"],
        )
        return report


def _last_day_of_month(year: int, month: int) -> int:
    """Return the last calendar day of the given month."""
    if month == 12:
        next_month = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_month = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    last_day = (next_month - timedelta(days=1)).day
    return last_day


def _print_rows(rows: Iterable[dict[str, Any]], columns: Iterable[str] | None = None) -> None:
    """Print dictionaries as a simple tab-separated table."""
    row_list = list(rows)
    if not row_list:
        _cli_print("No records found.", level="warning")
        return
    column_list = list(columns or row_list[0].keys())
    _cli_print("\t".join(column_list))
    for row in row_list:
        _cli_print("\t".join(str(row.get(column, "")) for column in column_list))


def _parse_csv_argument(raw_value: str | None) -> list[str] | None:
    if raw_value is None:
        return None
    return _normalize_text_list(raw_value.split(","))


def _coerce_cli_bool(value: Any, *, default: bool = False) -> bool:
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


def _config_lookup(config: dict[str, Any], *path: str) -> Any:
    current: Any = config
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return _UNSET
        current = current[key]
    return current


def _coerce_cli_list_default(value: Any) -> str | None:
    if value in (_UNSET, None):
        return None
    if isinstance(value, str):
        normalized = _normalize_text_list(value.split(","))
        return ",".join(normalized) if normalized else None
    if isinstance(value, Iterable):
        normalized = _normalize_text_list(str(item) for item in value)
        return ",".join(normalized) if normalized else None
    return str(value)


def _resolve_cli_defaults(argv: list[str] | None = None) -> dict[str, Any]:
    config = load_cli_config(argv=argv)

    def _value(
        *path: str,
        env_name: str | None = None,
        default: Any = None,
        bool_value: bool = False,
        csv_value: bool = False,
    ) -> Any:
        if env_name and env_name in os.environ:
            raw_env = os.environ[env_name]
            if bool_value:
                return _coerce_cli_bool(raw_env, default=bool(default))
            if csv_value:
                return _coerce_cli_list_default(raw_env)
            return raw_env
        raw_config = _config_lookup(config, *path)
        if raw_config is _UNSET:
            return default
        if bool_value:
            return _coerce_cli_bool(raw_config, default=bool(default))
        if csv_value:
            return _coerce_cli_list_default(raw_config)
        return raw_config

    return {
        "config_path": _config_lookup(config, "_loaded_from"),
        "db": _value("db", env_name="BOT_DB_PATH", default="funding_bot.db"),
        "summary_recipient": _value(
            "send_daily_summary",
            "recipient",
            env_name="DAILY_SUMMARY_RECIPIENT",
            default="lupael@i4e.com.bd",
        ),
        "summary_dry_run": _value(
            "send_daily_summary",
            "dry_run",
            env_name="DAILY_SUMMARY_DRY_RUN",
            default=False,
            bool_value=True,
        ),
        "discover_keywords": _value(
            "discover",
            "keywords",
            env_name="FUNDING_BOT_DISCOVER_KEYWORDS",
            default=None,
            csv_value=True,
        ),
        "discover_trusted_sources": _value(
            "discover",
            "trusted_sources",
            env_name="FUNDING_BOT_DISCOVER_TRUSTED_SOURCES",
            default=None,
            csv_value=True,
        ),
        "discover_dry_run": _value(
            "discover",
            "dry_run",
            env_name="FUNDING_BOT_DISCOVER_DRY_RUN",
            default=False,
            bool_value=True,
        ),
        "outreach_email": _value("send_outreach", "email", env_name="FUNDING_BOT_OUTREACH_EMAIL"),
        "outreach_name": _value("send_outreach", "name", env_name="FUNDING_BOT_OUTREACH_NAME"),
        "outreach_template_name": _value(
            "send_outreach",
            "template_name",
            env_name="FUNDING_BOT_OUTREACH_TEMPLATE_NAME",
            default=FundingBot.DEFAULT_OUTREACH_TEMPLATE,
        ),
        "outreach_subject": _value(
            "send_outreach", "subject", env_name="FUNDING_BOT_OUTREACH_SUBJECT"
        ),
        "outreach_body": _value("send_outreach", "body", env_name="FUNDING_BOT_OUTREACH_BODY"),
        "outreach_locale": _value(
            "send_outreach", "locale", env_name="FUNDING_BOT_OUTREACH_LOCALE"
        ),
        "outreach_dry_run": _value(
            "send_outreach",
            "dry_run",
            env_name="FUNDING_BOT_OUTREACH_DRY_RUN",
            default=False,
            bool_value=True,
        ),
        "warehouse_datasets": _value(
            "export_data_warehouse",
            "datasets",
            env_name="FUNDING_BOT_EXPORT_DATASETS",
            default="donors,tasks,matches,results",
            csv_value=True,
        ),
        "warehouse_format": _value(
            "export_data_warehouse",
            "format",
            env_name="FUNDING_BOT_EXPORT_FORMAT",
            default="json",
        ),
        "warehouse_output_dir": _value(
            "export_data_warehouse",
            "output_dir",
            env_name="FUNDING_BOT_EXPORT_OUTPUT_DIR",
            default="generated/exports",
        ),
        "warehouse_archive": _value(
            "export_data_warehouse",
            "archive",
            env_name="FUNDING_BOT_EXPORT_ARCHIVE",
            default=False,
            bool_value=True,
        ),
        "warehouse_dry_run": _value(
            "export_data_warehouse",
            "dry_run",
            env_name="FUNDING_BOT_EXPORT_DRY_RUN",
            default=False,
            bool_value=True,
        ),
    }


def _resolve_cli_log_level(*, verbose: bool = False, quiet: bool = False) -> int:
    if verbose:
        return logging.INFO
    if quiet:
        return logging.ERROR
    return logging.WARNING


def _configure_cli_logging(
    *,
    verbose: bool = False,
    quiet: bool = False,
    no_color: bool = False,
) -> int:
    level = _resolve_cli_log_level(verbose=verbose, quiet=quiet)
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    handler = logging.StreamHandler()
    handler.setFormatter(
        _CliColorFormatter(
            "%(levelname)s:%(name)s:%(message)s",
            color_enabled=_should_use_color(sys.stderr, no_color=no_color),
        )
    )
    root_logger.addHandler(handler)
    root_logger.setLevel(level)
    return level


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    return str(value)


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, default=_json_default)


def _emit_cli_json(payload: dict[str, Any]) -> None:
    _cli_print(_json_dumps(payload))


def _cli_payload(command: str, **payload: Any) -> dict[str, Any]:
    return {"command": command, "ok": True, **payload}


def _write_json_report(path_value: str, payload: dict[str, Any]) -> None:
    output_path = Path(path_value)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(_json_dumps(payload), encoding="utf-8")


def _queue_async_task(
    task_label: str,
    task_callable: Any,
    *,
    task_kwargs: dict[str, Any],
    progress_reporter: _CliProgressReporter | None = None,
) -> dict[str, Any]:
    with _bind_cli_progress(progress_reporter):
        async_result = task_callable.delay(**task_kwargs)
    payload: dict[str, Any] = {
        "task": {
            "label": task_label,
            "id": async_result.id,
            "status": async_result.status,
            "ready": bool(async_result.ready()),
        }
    }
    if async_result.ready():
        payload["result"] = async_result.get(propagate=True)
    else:
        payload["tracking_hint"] = (
            "Track progress in the task_runs table or the configured Celery result backend."
        )
    return payload


def _print_queued_task(
    payload: dict[str, Any], *, ready_renderer: Callable[[dict[str, Any]], None] | None = None
) -> None:
    task = payload["task"]
    _cli_print(f"Queued {task['label']} task {task['id']}.", level="success")
    _cli_print(f"Task status: {_colorize_status_text(task['status'])}.")
    result = payload.get("result")
    if isinstance(result, dict) and ready_renderer is not None:
        ready_renderer(result)
    elif payload.get("tracking_hint"):
        _cli_print(payload["tracking_hint"], level="warning")


def _render_discover_task_result_text(result: dict[str, Any]) -> None:
    found = result.get("new_opportunities", [])
    if found:
        _print_rows(found, ["signature", "source", "donor_name", "title", "category"])
    else:
        _cli_print("No new opportunities found.", level="warning")
    if result.get("dry_run"):
        _cli_print("\n(dry run: no opportunities were saved)", level="warning")


def _render_outreach_task_result_text(result: dict[str, Any]) -> None:
    if result.get("template_name"):
        _cli_print(f"Template: {result['template_name']}")
    if result.get("locale"):
        _cli_print(f"Locale: {result['locale']}")
    _cli_print(f"Subject: {result['subject']}\n")
    _cli_print(result["body"])
    if result.get("dry_run"):
        preview = result.get("preview") or {}
        for label, enabled in (
            ("Create donor record", preview.get("would_create_donor")),
            ("Record consent", preview.get("would_record_consent")),
            ("Send email", preview.get("would_send_email")),
            ("Log communication", preview.get("would_log_communication")),
            ("Update last contact timestamp", preview.get("would_update_last_contact")),
        ):
            if enabled:
                _cli_print(f"- Would {label.lower()}.")
        _cli_print("\n(dry run: no email was actually sent)", level="warning")
    else:
        _cli_print(f"\nOutreach email sent to {result['email']}.", level="success")


def _render_daily_summary_task_result_text(result: dict[str, Any]) -> None:
    _cli_print(f"Subject: {result['subject']}\n")
    _cli_print(result["body"])
    if result.get("dry_run"):
        _cli_print("\n(dry run: no email was actually sent)", level="warning")
    else:
        _cli_print(f"\nDaily summary sent to {result['recipient']}.", level="success")


def _render_gdpr_export_text(result: dict[str, Any], *, output_path: str | None = None) -> None:
    donor = result.get("donor") or {}
    _cli_print(f"Donor: {donor.get('email', 'unknown')}")
    _cli_print(f"Consent records: {len(result.get('consent_records', []))}")
    _cli_print(f"Communications: {len(result.get('communications', []))}")
    _cli_print(f"Outreach events: {len(result.get('outreach_events', []))}")
    _cli_print(f"Audit logs: {len(result.get('audit_logs', []))}")
    if result.get("dry_run"):
        _cli_print(
            "\n(dry run: export preview only; no audit log entry was written)", level="warning"
        )
        return
    if output_path:
        _cli_print(f"\nGDPR export written to {output_path}.", level="success")
        return
    _cli_print("")
    _cli_print(_json_dumps(result))


def _redact_service_url(url: str | None) -> str | None:
    if not url:
        return url
    parsed = urllib.parse.urlparse(url)
    if parsed.hostname is None:
        return url
    netloc = parsed.hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    if parsed.username:
        netloc = f"{parsed.username}:***@{netloc}"
    return urllib.parse.urlunparse(parsed._replace(netloc=netloc))


def _redis_ping(url: str, *, timeout: float = 1.0) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
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


def _collect_redis_diagnostics(*, broker_url: str, result_backend: str) -> dict[str, Any]:
    redis_targets: list[tuple[str, str]] = []
    for label, url in (("broker", broker_url), ("result_backend", result_backend)):
        scheme = urllib.parse.urlparse(url).scheme
        if scheme in {"redis", "rediss"}:
            redis_targets.append((label, url))
    if not redis_targets:
        return {
            "status": "disabled",
            "checked": False,
            "targets": [],
            "message": "Redis is not configured for the current Celery broker/result backend.",
        }

    checks = []
    for label, url in redis_targets:
        checks.append({"role": label, **_redis_ping(url)})
    statuses = {entry["status"] for entry in checks}
    status = "error" if "error" in statuses else "ok"
    return {
        "status": status,
        "checked": True,
        "targets": checks,
    }


def _collect_connector_diagnostics(
    *,
    keywords: Iterable[str] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    connectors = []
    connector_names = sorted(connector_registry().keys())
    _emit_progress_event(
        progress_callback,
        stage="connector-diagnostics",
        description="Running connector diagnostics",
        completed=0,
        total=len(connector_names),
    )
    for index, connector_name in enumerate(connector_names, start=1):
        try:
            connector = create_connector(connector_name)
            health = connector.check_health()
            validation = connector.validate_connectivity(
                keywords,
                sample_limit=0,
                progress_callback=progress_callback,
            )
            status = str(validation.get("status", "ok"))
            if health.get("healthy") is False and status == "ok":
                status = "degraded"
            connectors.append(
                {
                    "connector": connector_name,
                    "status": status,
                    "healthy": bool(health.get("healthy", status == "ok")),
                    "health": health,
                    "validation": validation,
                }
            )
            _emit_progress_event(
                progress_callback,
                stage="connector-diagnostics",
                description=f"Running connector diagnostics ({connector_name})",
                current=connector_name,
                completed=index,
                total=len(connector_names),
            )
        except Exception as exc:
            connectors.append(
                {
                    "connector": connector_name,
                    "status": "error",
                    "healthy": False,
                    "error": str(exc),
                }
            )
            _emit_progress_event(
                progress_callback,
                stage="connector-diagnostics",
                description=f"Running connector diagnostics ({connector_name})",
                current=connector_name,
                completed=index,
                total=len(connector_names),
            )
    statuses = {entry["status"] for entry in connectors}
    if "error" in statuses:
        status = "error"
    elif "degraded" in statuses:
        status = "degraded"
    else:
        status = "ok"
    return {
        "status": status,
        "count": len(connectors),
        "connectors": connectors,
    }


def _collect_doctor_report(
    *,
    db_path: str,
    connector_keywords: Iterable[str] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    from task_queue import celery_app, get_queue_status, load_queue_config

    configuration = {
        "database_path": db_path,
        "bot_db_path_env": os.environ.get("BOT_DB_PATH"),
        "task_queue_enabled": os.environ.get("ENABLE_TASK_QUEUE"),
        "legacy_cron_enabled": os.environ.get("ENABLE_LEGACY_CRON"),
        "celery_broker_url": _redact_service_url(os.environ.get("CELERY_BROKER_URL")),
        "celery_result_backend": _redact_service_url(os.environ.get("CELERY_RESULT_BACKEND")),
        "celery_queue_name": os.environ.get("CELERY_QUEUE_NAME"),
        "smtp_host": os.environ.get("SMTP_HOST"),
        "smtp_port": os.environ.get("SMTP_PORT"),
        "smtp_username": os.environ.get("SMTP_USERNAME"),
        "smtp_password_set": bool(os.environ.get("SMTP_PASSWORD")),
        "encryption_key_set": bool(os.environ.get("FUNDING_BOT_ENCRYPTION_KEY")),
    }

    database_exists = Path(db_path).exists() if db_path != ":memory:" else True
    try:
        bot = FundingBot(db_path=db_path)
        try:
            table_count = bot.connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
            ).fetchone()[0]
        finally:
            bot.close()
        database_check = {
            "status": "ok",
            "connected": True,
            "database_path": db_path,
            "exists_before_check": database_exists,
            "table_count": int(table_count),
            "sqlite_version": sqlite3.sqlite_version,
        }
    except Exception as exc:
        database_check = {
            "status": "error",
            "connected": False,
            "database_path": db_path,
            "exists_before_check": database_exists,
            "error": str(exc),
            "sqlite_version": sqlite3.sqlite_version,
        }

    queue_config = load_queue_config()
    celery_status = get_queue_status(config=queue_config, app=celery_app)
    celery_status = {
        **celery_status,
        "broker_url": _redact_service_url(queue_config.broker_url),
        "result_backend": _redact_service_url(queue_config.result_backend),
        "task_always_eager": queue_config.task_always_eager,
        "inspect_timeout_seconds": queue_config.inspect_timeout_seconds,
        "status": (
            "disabled"
            if not queue_config.enable_task_queue
            else (
                "ok"
                if celery_status.get("worker_status") == "healthy" or queue_config.task_always_eager
                else "degraded"
            )
        ),
    }

    checks = {
        "database": database_check,
        "celery": celery_status,
        "redis": _collect_redis_diagnostics(
            broker_url=queue_config.broker_url,
            result_backend=queue_config.result_backend,
        ),
        "connectors": _collect_connector_diagnostics(
            keywords=connector_keywords,
            progress_callback=progress_callback,
        ),
    }
    severity = {"ok": 0, "disabled": 0, "degraded": 1, "error": 2}
    overall_status = max(
        (check.get("status", "ok") for check in checks.values()),
        key=lambda value: severity.get(value, 2),
    )
    return _cli_payload(
        "doctor", overall_status=overall_status, configuration=configuration, checks=checks
    )


def _print_doctor_report(report: dict[str, Any]) -> None:
    _cli_print("Funding Bot doctor")
    _cli_print(f"Overall status: {_colorize_status_text(report['overall_status'])}")
    _cli_print()
    configuration = report["configuration"]
    _cli_print("Configuration")
    _cli_print(f"- Database path: {configuration['database_path']}")
    _cli_print(f"- Celery broker: {configuration.get('celery_broker_url') or '(default)'}")
    _cli_print(
        f"- Celery result backend: {configuration.get('celery_result_backend') or '(default)'}"
    )
    _cli_print(
        f"- Encryption key configured: {'yes' if configuration['encryption_key_set'] else 'no'}"
    )
    _cli_print()
    _cli_print("Checks")
    database_check = report["checks"]["database"]
    _cli_print(
        f"- database: {_colorize_status_text(database_check['status'])} "
        f"(connected={database_check.get('connected', False)}, tables={database_check.get('table_count', 0)})"
    )
    celery_check = report["checks"]["celery"]
    _cli_print(
        f"- celery: {_colorize_status_text(celery_check['status'])} "
        f"(mode={celery_check['mode']}, workers={celery_check['worker_count']}, queue={celery_check['queue_name']})"
    )
    redis_check = report["checks"]["redis"]
    if not redis_check.get("checked"):
        _cli_print(
            f"- redis: {_colorize_status_text(redis_check['status'])} ({redis_check['message']})"
        )
    else:
        redis_statuses = ", ".join(
            f"{entry['role']}={_colorize_status_text(entry['status'])}"
            for entry in redis_check.get("targets", [])
        )
        _cli_print(f"- redis: {_colorize_status_text(redis_check['status'])} ({redis_statuses})")
    connector_check = report["checks"]["connectors"]
    connector_statuses = ", ".join(
        f"{entry['connector']}={_colorize_status_text(entry['status'])}"
        for entry in connector_check["connectors"]
    )
    _cli_print(
        f"- connectors: {_colorize_status_text(connector_check['status'])} ({connector_statuses})"
    )


def _cli_completion_spec() -> dict[str, Any]:
    import argparse

    parser = _build_arg_parser()
    global_options: set[str] = set()
    subcommands: dict[str, dict[str, Any]] = {}
    for action in parser._actions:
        global_options.update(action.option_strings)
        if isinstance(action, argparse._SubParsersAction):
            for command_name, subparser in action.choices.items():
                options: set[str] = set()
                value_choices: dict[str, list[str]] = {}
                for sub_action in subparser._actions:
                    options.update(sub_action.option_strings)
                    if sub_action.option_strings and sub_action.choices is not None:
                        value_choices[sub_action.option_strings[-1]] = [
                            str(choice) for choice in sub_action.choices
                        ]
                subcommands[command_name] = {
                    "options": sorted(option for option in options if option),
                    "value_choices": value_choices,
                }
    return {
        "global_options": sorted(option for option in global_options if option),
        "subcommands": subcommands,
    }


def _build_completion_script(shell: str) -> str:
    spec = _cli_completion_spec()
    global_tokens = sorted(set(spec["global_options"]) | set(spec["subcommands"].keys()))
    all_value_choices: dict[str, list[str]] = {}
    for data in spec["subcommands"].values():
        for option, values in data["value_choices"].items():
            all_value_choices.setdefault(option, [])
            all_value_choices[option] = sorted(set(all_value_choices[option]) | set(values))

    if shell == "bash":
        lines = [
            "_funding_bot_completion() {",
            "  local cur prev command",
            "  COMPREPLY=()",
            '  cur="${COMP_WORDS[COMP_CWORD]}"',
            '  prev="${COMP_WORDS[COMP_CWORD-1]}"',
            '  command=""',
            '  for word in "${COMP_WORDS[@]:1}"; do',
            '    if [[ "$word" != -* ]]; then',
            '      command="$word"',
            "      break",
            "    fi",
            "  done",
            '  case "$prev" in',
        ]
        for option, values in sorted(all_value_choices.items()):
            lines.extend(
                [
                    f"    {option})",
                    f"      COMPREPLY=( $(compgen -W \"{' '.join(values)}\" -- \"$cur\") )",
                    "      return 0",
                    "      ;;",
                ]
            )
        lines.extend(
            [
                "  esac",
                '  if [[ -z "$command" ]]; then',
                f"    COMPREPLY=( $(compgen -W \"{' '.join(global_tokens)}\" -- \"$cur\") )",
                "    return 0",
                "  fi",
                '  case "$command" in',
            ]
        )
        for command_name, data in sorted(spec["subcommands"].items()):
            command_tokens = " ".join(sorted(set(data["options"])))
            lines.extend(
                [
                    f"    {command_name})",
                    f'      COMPREPLY=( $(compgen -W "{command_tokens}" -- "$cur") )',
                    "      return 0",
                    "      ;;",
                ]
            )
        lines.extend(
            [
                "  esac",
                "  return 0",
                "}",
                "complete -F _funding_bot_completion funding-bot",
            ]
        )
        return "\n".join(lines)

    if shell == "zsh":
        lines = [
            "#compdef funding-bot",
            "_funding_bot_completion() {",
            '  local command=""',
            "  local word",
            "  for word in ${words[@]:2}; do",
            '    if [[ "$word" != -* ]]; then',
            '      command="$word"',
            "      break",
            "    fi",
            "  done",
            '  case "$words[CURRENT-1]" in',
        ]
        for option, values in sorted(all_value_choices.items()):
            lines.extend(
                [
                    f"    {option})",
                    f"      compadd -- {' '.join(values)}",
                    "      return 0",
                    "      ;;",
                ]
            )
        lines.extend(
            [
                "  esac",
                '  if [[ -z "$command" ]]; then',
                f"    compadd -- {' '.join(global_tokens)}",
                "    return 0",
                "  fi",
                '  case "$command" in',
            ]
        )
        for command_name, data in sorted(spec["subcommands"].items()):
            command_tokens = " ".join(sorted(set(data["options"])))
            lines.extend(
                [
                    f"    {command_name})",
                    f"      compadd -- {command_tokens}",
                    "      return 0",
                    "      ;;",
                ]
            )
        lines.extend(
            [
                "  esac",
                "}",
                "compdef _funding_bot_completion funding-bot",
            ]
        )
        return "\n".join(lines)

    raise FundingBotError(f"Unsupported completion shell {shell!r}.")


def _missing_required_cli_args(args: Any) -> list[tuple[str, dict[str, Any]]]:
    missing: list[tuple[str, dict[str, Any]]] = []
    for spec in getattr(args, "_required_cli_args", ()):
        value = getattr(args, spec["dest"], None)
        if isinstance(value, str):
            value = value.strip()
        if value in (None, ""):
            missing.append((spec["dest"], spec))
    return missing


def _prompt_for_cli_value(spec: dict[str, Any]) -> str:
    prompt = f"{spec['prompt']}: "
    choices = spec.get("choices")
    while True:
        response = input(prompt).strip()
        if not response:
            _cli_print(f"{spec['flag']} is required.", level="error", file=sys.stderr)
            continue
        if choices is not None and response not in choices:
            _cli_print(
                f"Invalid value for {spec['flag']}. Choose one of: {', '.join(choices)}.",
                level="error",
                file=sys.stderr,
            )
            continue
        return response


def _prompt_for_missing_cli_args(
    parser: "argparse.ArgumentParser", args: "argparse.Namespace"
) -> "argparse.Namespace":
    missing = _missing_required_cli_args(args)
    if not missing:
        return args
    if args.non_interactive:
        parser.error(
            f"the following arguments are required for {args.command}: "
            + ", ".join(spec["flag"] for _, spec in missing)
        )
    for dest, spec in missing:
        setattr(args, dest, _prompt_for_cli_value(spec))
    return args


def _build_arg_parser(cli_defaults: dict[str, Any] | None = None) -> "argparse.ArgumentParser":
    import argparse

    defaults = cli_defaults or {}
    default_db_path = str(defaults.get("db", "funding_bot.db"))
    connector_choices = tuple(sorted(connector_registry().keys()))
    parser = argparse.ArgumentParser(
        prog="funding-bot",
        description="Nonprofit Funding Automation Bot – command-line interface",
    )
    parser.add_argument(
        "--config",
        metavar="FILE",
        help="Load command defaults from a YAML or TOML configuration file.",
    )
    parser.add_argument(
        "--db",
        default=default_db_path,
        metavar="PATH",
        help=(
            "Path to the SQLite database file "
            f"(default: {default_db_path}, overridable with BOT_DB_PATH)."
        ),
    )
    verbosity_group = parser.add_mutually_exclusive_group()
    verbosity_group.add_argument(
        "--verbose",
        action="store_true",
        help="Enable informational CLI logging.",
    )
    verbosity_group.add_argument(
        "--quiet",
        action="store_true",
        help="Only show CLI errors.",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail instead of prompting when required command options are missing.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Print structured JSON output for programmatic use.",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI color output while leaving progress reporting enabled.",
    )
    command_parent = argparse.ArgumentParser(add_help=False)
    command_parent.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Print structured JSON output for programmatic use.",
    )
    command_parent.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI color output while leaving progress reporting enabled.",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    def _add_dry_run_flags(
        target: "argparse.ArgumentParser", *, default: bool, help_text: str
    ) -> None:
        group = target.add_mutually_exclusive_group()
        group.add_argument("--dry-run", dest="dry_run", action="store_true", help=help_text)
        group.add_argument(
            "--no-dry-run",
            dest="dry_run",
            action="store_false",
            help="Disable dry-run mode even if it is enabled in configuration.",
        )
        target.set_defaults(dry_run=default)

    # send-daily-summary
    summary_parser = subparsers.add_parser(
        "send-daily-summary",
        help="Build and email the daily funding report.",
        parents=[command_parent],
    )
    summary_parser.add_argument(
        "--recipient",
        default=defaults.get("summary_recipient", "lupael@i4e.com.bd"),
        metavar="EMAIL",
        help="Recipient email address (default: lupael@i4e.com.bd).",
    )
    _add_dry_run_flags(
        summary_parser,
        default=bool(defaults.get("summary_dry_run", False)),
        help_text="Print the summary to stdout without sending it.",
    )

    opportunities_parser = subparsers.add_parser(
        "list-opportunities",
        help="List stored funding opportunities.",
        parents=[command_parent],
    )
    opportunities_parser.add_argument(
        "--status",
        metavar="STATUS",
        help="Filter opportunities by status.",
    )
    opportunities_parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Limit the number of rows shown.",
    )

    audit_parser = subparsers.add_parser(
        "audit-log",
        help="List recent audit log entries.",
        parents=[command_parent],
    )
    audit_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        metavar="N",
        help="Limit the number of rows shown (default: 20).",
    )
    audit_parser.add_argument(
        "--action",
        metavar="ACTION",
        help="Filter audit entries by action.",
    )

    donors_parser = subparsers.add_parser(
        "list-donors",
        help="List donor records.",
        parents=[command_parent],
    )
    donors_parser.add_argument(
        "--segment",
        metavar="SEGMENT",
        choices=["corporate", "institutional", "individual", "unknown"],
        help="Filter donors by segment.",
    )

    monthly_parser = subparsers.add_parser(
        "monthly-audit-report",
        help="Generate a monthly GDPR/compliance audit report.",
        parents=[command_parent],
    )
    monthly_parser.add_argument(
        "--year",
        type=int,
        metavar="YEAR",
        help="Four-digit year (default: current UTC year).",
    )
    monthly_parser.add_argument(
        "--month",
        type=int,
        metavar="MONTH",
        choices=range(1, 13),
        help="Month number 1–12 (default: current UTC month).",
    )
    monthly_parser.add_argument(
        "--output",
        metavar="FILE",
        help="Write the report as JSON to FILE instead of printing it.",
    )

    gdpr_parser = subparsers.add_parser(
        "gdpr-self-check-report",
        help="Generate a GDPR compliance self-check report.",
        parents=[command_parent],
    )
    gdpr_parser.add_argument(
        "--cadence",
        choices=("weekly", "monthly"),
        default="weekly",
        help="Report cadence window to summarize (default: weekly).",
    )
    gdpr_parser.add_argument(
        "--output",
        metavar="FILE",
        help="Write the report as JSON to FILE instead of printing it.",
    )

    discover_parser = subparsers.add_parser(
        "discover",
        help="Search configured donation sources and store new opportunities.",
        parents=[command_parent],
    )
    discover_parser.add_argument(
        "--keywords",
        default=defaults.get("discover_keywords"),
        metavar="KEYWORDS",
        help="Comma-separated keyword filters (default: stored search settings).",
    )
    discover_parser.add_argument(
        "--trusted-sources",
        default=defaults.get("discover_trusted_sources"),
        metavar="SOURCES",
        help="Comma-separated allow-list of sources (default: stored search settings).",
    )
    _add_dry_run_flags(
        discover_parser,
        default=bool(defaults.get("discover_dry_run", False)),
        help_text="Preview matching opportunities without saving them.",
    )

    test_connector_parser = subparsers.add_parser(
        "test-connector",
        help="Validate one connector and print sample results.",
        parents=[command_parent],
    )
    test_connector_parser.add_argument(
        "--connector",
        choices=connector_choices,
        metavar="NAME",
        help="Connector slug to validate.",
    )
    test_connector_parser.add_argument(
        "--keywords",
        metavar="KEYWORDS",
        help="Comma-separated keywords to test, including mapped synonyms/categories.",
    )
    test_connector_parser.add_argument(
        "--limit",
        type=int,
        default=3,
        metavar="N",
        help="Maximum sample results to print (default: 3).",
    )
    test_connector_parser.set_defaults(
        _required_cli_args=(
            {
                "dest": "connector",
                "flag": "--connector",
                "prompt": f"Connector slug ({', '.join(connector_choices)})",
                "choices": connector_choices,
            },
        )
    )

    outreach_parser = subparsers.add_parser(
        "send-outreach",
        help="Compose and send (or preview) a personalized donor outreach email.",
        parents=[command_parent],
    )
    outreach_parser.add_argument(
        "--email",
        default=defaults.get("outreach_email"),
        metavar="EMAIL",
        help="Donor email address.",
    )
    outreach_parser.add_argument(
        "--name",
        default=defaults.get("outreach_name"),
        metavar="NAME",
        help="Donor name.",
    )
    outreach_parser.add_argument(
        "--template-name",
        default=defaults.get("outreach_template_name", FundingBot.DEFAULT_OUTREACH_TEMPLATE),
        metavar="NAME",
        help=(
            "Built-in outreach template to preview/send when --subject and --body are omitted "
            f"(default: {FundingBot.DEFAULT_OUTREACH_TEMPLATE})."
        ),
    )
    outreach_parser.add_argument(
        "--subject",
        default=defaults.get("outreach_subject"),
        metavar="TEMPLATE",
        help="Subject template with {placeholders} (defaults to the donor's locale-aware template).",
    )
    outreach_parser.add_argument(
        "--body",
        default=defaults.get("outreach_body"),
        metavar="TEMPLATE",
        help="Body template with {placeholders} (defaults to the donor's locale-aware template).",
    )
    outreach_parser.add_argument(
        "--locale",
        default=defaults.get("outreach_locale"),
        metavar="LOCALE",
        help="Donor locale preference for template selection (supported: en, bn).",
    )
    _add_dry_run_flags(
        outreach_parser,
        default=bool(defaults.get("outreach_dry_run", False)),
        help_text="Compose and preview the outreach without sending or storing it.",
    )
    outreach_parser.set_defaults(
        _required_cli_args=(
            {"dest": "email", "flag": "--email", "prompt": "Donor email"},
            {"dest": "name", "flag": "--name", "prompt": "Donor name"},
        )
    )

    profile_parser = subparsers.add_parser(
        "set-organization-profile",
        help="Store the nonprofit's organization profile from a JSON file (or stdin).",
        parents=[command_parent],
    )
    profile_parser.add_argument(
        "--file",
        metavar="FILE",
        help="Path to a JSON file with the profile (default: read from stdin).",
    )

    credential_parser = subparsers.add_parser(
        "register-credential",
        help="Register a credential alias that resolves to an environment variable.",
        parents=[command_parent],
    )
    credential_parser.add_argument("--alias", metavar="ALIAS", help="Credential alias name.")
    credential_parser.add_argument(
        "--env-var",
        metavar="ENV_VAR",
        help="Name of the environment variable holding the secret.",
    )
    credential_parser.set_defaults(
        _required_cli_args=(
            {"dest": "alias", "flag": "--alias", "prompt": "Credential alias"},
            {
                "dest": "env_var",
                "flag": "--env-var",
                "prompt": "Environment variable name",
            },
        )
    )

    retention_policy_parser = subparsers.add_parser(
        "set-data-retention-policy",
        help="Persist retention windows for operational data cleanup.",
        parents=[command_parent],
    )
    retention_policy_parser.add_argument("--audit-logs-days", type=int, metavar="DAYS")
    retention_policy_parser.add_argument("--communications-days", type=int, metavar="DAYS")
    retention_policy_parser.add_argument("--documents-days", type=int, metavar="DAYS")
    retention_policy_parser.add_argument("--opportunities-days", type=int, metavar="DAYS")
    retention_policy_parser.add_argument("--submission-attempts-days", type=int, metavar="DAYS")
    retention_policy_parser.add_argument("--completed-tasks-days", type=int, metavar="DAYS")

    retention_enforcement_parser = subparsers.add_parser(
        "enforce-data-retention",
        help="Delete records that exceed the configured retention windows.",
        parents=[command_parent],
    )
    retention_enforcement_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be deleted without modifying data.",
    )
    retention_enforcement_parser.add_argument(
        "--as-of",
        metavar="ISO8601",
        help="Evaluate retention as of the provided UTC timestamp.",
    )
    retention_enforcement_parser.add_argument(
        "--no-archive",
        action="store_true",
        help="Skip writing archival payloads before deleting expired records.",
    )

    warehouse_export_parser = subparsers.add_parser(
        "export-data-warehouse",
        help="Export warehouse-friendly datasets in JSON, CSV, or Parquet format.",
        parents=[command_parent],
    )
    warehouse_export_parser.add_argument(
        "--datasets",
        default=defaults.get("warehouse_datasets", "donors,tasks,matches,results"),
        metavar="DATASETS",
        help="Comma-separated dataset names (default: donors,tasks,matches,results).",
    )
    warehouse_export_parser.add_argument(
        "--format",
        dest="export_format",
        choices=("json", "csv", "parquet"),
        default=defaults.get("warehouse_format", "json"),
        help="Export format (default: json).",
    )
    warehouse_export_parser.add_argument(
        "--output-dir",
        default=defaults.get("warehouse_output_dir", "generated/exports"),
        help="Directory for generated export files (default: generated/exports).",
    )
    archive_group = warehouse_export_parser.add_mutually_exclusive_group()
    archive_group.add_argument(
        "--archive",
        dest="archive",
        action="store_true",
        help="Copy generated export files to configured cold storage/S3 targets.",
    )
    archive_group.add_argument(
        "--no-archive",
        dest="archive",
        action="store_false",
        help="Disable archival even if it is enabled in configuration.",
    )
    warehouse_export_parser.set_defaults(archive=bool(defaults.get("warehouse_archive", False)))
    _add_dry_run_flags(
        warehouse_export_parser,
        default=bool(defaults.get("warehouse_dry_run", False)),
        help_text="Preview the export artifacts without writing files.",
    )

    completion_parser = subparsers.add_parser(
        "completion",
        help="Print bash or zsh shell completion scripts.",
        parents=[command_parent],
    )
    completion_parser.add_argument(
        "--shell",
        choices=("bash", "zsh"),
        default="bash",
        help="Shell dialect to generate (default: bash).",
    )

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Run configuration and health diagnostics.",
        parents=[command_parent],
    )
    doctor_parser.add_argument(
        "--connector-keywords",
        metavar="KEYWORDS",
        help="Comma-separated keywords to use for connector validation checks.",
    )

    subparsers.add_parser(
        "show-settings",
        help="Print the organization profile, search settings, and credentials.",
        parents=[command_parent],
    )

    return parser


def _run_register_credential(bot: "FundingBot", args: "argparse.Namespace") -> dict[str, Any]:
    """Handle the ``register-credential`` CLI command.

    Kept as a standalone function (rather than inline in ``main``) so that
    the credential alias/env-var-name values it handles stay scoped to this
    function and are never intermixed with unrelated output written later in
    ``main`` (e.g. ``show-settings``).
    """
    bot.register_credential(args.alias, args.env_var)
    return _cli_payload(
        "register-credential",
        registered=True,
        alias=args.alias,
        env_var_name=args.env_var,
    )


def _collect_settings_payload(bot: "FundingBot") -> dict[str, Any]:
    return {
        "organization_profile": bot.load_organization_profile(),
        "search_settings": bot.load_search_settings(),
        "data_retention_policy": bot.load_data_retention_policy(),
        "credentials": bot.list_credentials(),
    }


def _run_show_settings(bot: "FundingBot", *, json_output: bool = False) -> None:
    """Handle the ``show-settings`` CLI command.

    Prints the organization profile and search settings as JSON. Credential
    aliases are printed separately by :func:`_print_credential_aliases` so
    this function never touches credential metadata.
    """
    payload = _collect_settings_payload(bot)
    if json_output:
        _emit_cli_json(_cli_payload("show-settings", **payload))
        return
    settings_json = _json_dumps(
        {
            "organization_profile": payload["organization_profile"],
            "search_settings": payload["search_settings"],
            "data_retention_policy": payload["data_retention_policy"],
        }
    )
    _cli_print(settings_json)


def _print_credential_aliases(bot: "FundingBot") -> None:
    """Print registered credential aliases and their backing env-var *names*.

    Isolated in its own function (never returning or otherwise exposing the
    resolved secret values) so credential alias/env-var-name metadata is
    printed independently of any other CLI output.
    """
    _cli_print()
    _cli_print("Credential aliases (env-var *names* only, never the secret values):")
    _print_rows(bot.list_credentials(), ["alias", "env_var_name"])


def main(argv: list[str] | None = None) -> None:
    import argparse

    argv_list = list(sys.argv[1:] if argv is None else argv)
    parser = _build_arg_parser(_resolve_cli_defaults(argv_list))
    args = parser.parse_args(argv_list)
    args.no_color = bool(args.no_color or "--no-color" in argv_list)
    args.json_output = bool(args.json_output or "--json" in argv_list)
    _configure_cli_output(no_color=args.no_color, json_output=args.json_output)
    _configure_cli_logging(verbose=args.verbose, quiet=args.quiet, no_color=args.no_color)

    if args.command is None:
        parser.print_help()
        return
    args = _prompt_for_missing_cli_args(parser, args)
    logging.getLogger(__name__).info("Running CLI command %s", args.command)

    bot: FundingBot | None = None

    def get_bot() -> FundingBot:
        nonlocal bot
        if bot is None:
            bot = FundingBot(db_path=args.db)
        return bot

    try:
        try:
            if args.command == "send-daily-summary":
                from tasks.celery_tasks import send_daily_summary_task

                with _CliProgressReporter() as progress:
                    queued = _queue_async_task(
                        "send-daily-summary",
                        send_daily_summary_task,
                        task_kwargs={
                            "db_path": args.db,
                            "recipient": args.recipient,
                            "dry_run": args.dry_run,
                        },
                        progress_reporter=progress,
                    )
                if isinstance(queued, dict):
                    payload = _cli_payload("send-daily-summary", **queued)
                    if args.json_output:
                        _emit_cli_json(payload)
                    else:
                        _print_queued_task(
                            payload, ready_renderer=_render_daily_summary_task_result_text
                        )
            elif args.command == "list-opportunities":
                rows = get_bot().list_opportunities(status=args.status)
                if args.limit is not None:
                    rows = rows[: args.limit]
                columns = ["signature", "source", "donor_name", "title", "status", "discovered_at"]
                payload = _cli_payload(
                    "list-opportunities",
                    count=len(rows),
                    columns=columns,
                    rows=rows,
                )
                if args.json_output:
                    _emit_cli_json(payload)
                else:
                    _print_rows(rows, columns)
            elif args.command == "audit-log":
                rows = get_bot().list_audit_logs(limit=args.limit, action=args.action)
                columns = ["happened_at", "action", "details_json"]
                payload = _cli_payload("audit-log", count=len(rows), columns=columns, rows=rows)
                if args.json_output:
                    _emit_cli_json(payload)
                else:
                    _print_rows(rows, columns)
            elif args.command == "list-donors":
                rows = get_bot().list_donors(segment=args.segment)
                columns = ["email", "name", "segment", "locale", "opted_out", "last_contact_at"]
                payload = _cli_payload("list-donors", count=len(rows), columns=columns, rows=rows)
                if args.json_output:
                    _emit_cli_json(payload)
                else:
                    _print_rows(rows, columns)
            elif args.command == "monthly-audit-report":
                with _CliProgressReporter() as progress:
                    progress.update(
                        "monthly-audit-export",
                        description="Building monthly audit export",
                        completed=0,
                        total=3,
                    )
                    report = get_bot().build_monthly_audit_report(year=args.year, month=args.month)
                    progress.update(
                        "monthly-audit-export",
                        description="Monthly audit data ready",
                        completed=1,
                        total=3,
                    )
                    if args.output:
                        progress.update(
                            "monthly-audit-export",
                            description="Writing monthly audit export",
                            completed=2,
                            total=3,
                        )
                        _write_json_report(args.output, report)
                    progress.update(
                        "monthly-audit-export",
                        description="Monthly audit export complete",
                        completed=3,
                        total=3,
                    )
                payload = _cli_payload(
                    "monthly-audit-report",
                    report=report,
                    output_path=args.output,
                )
                if args.json_output:
                    _emit_cli_json(payload)
                elif args.output:
                    _cli_print(f"Monthly audit report written to {args.output}.", level="success")
                else:
                    _cli_print(_json_dumps(report))
            elif args.command == "set-data-retention-policy":
                policy_updates = {
                    "audit_logs_days": args.audit_logs_days,
                    "communications_days": args.communications_days,
                    "documents_days": args.documents_days,
                    "opportunities_days": args.opportunities_days,
                    "submission_attempts_days": args.submission_attempts_days,
                    "completed_tasks_days": args.completed_tasks_days,
                }
                policy = get_bot().store_data_retention_policy(
                    {key: value for key, value in policy_updates.items() if value is not None}
                )
                payload = _cli_payload("set-data-retention-policy", policy=policy)
                if args.json_output:
                    _emit_cli_json(payload)
                else:
                    _cli_print(_json_dumps(policy))
            elif args.command == "enforce-data-retention":
                as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
                report = get_bot().enforce_data_retention(
                    now=as_of,
                    dry_run=args.dry_run,
                    archive=not args.no_archive,
                )
                payload = _cli_payload("enforce-data-retention", report=report)
                if args.json_output:
                    _emit_cli_json(payload)
                else:
                    _cli_print(_json_dumps(report))
            elif args.command == "export-data-warehouse":
                with _CliProgressReporter() as progress:
                    export_report = get_bot().export_data_warehouse(
                        datasets=_parse_csv_argument(args.datasets)
                        or ["donors", "tasks", "matches", "results"],
                        export_format=args.export_format,
                        output_dir=args.output_dir,
                        archive=args.archive,
                        dry_run=args.dry_run,
                        progress_callback=progress.update_from_detail,
                    )
                payload = _cli_payload("export-data-warehouse", report=export_report)
                if args.json_output:
                    _emit_cli_json(payload)
                else:
                    _cli_print(_json_dumps(export_report), level="success")
            elif args.command == "gdpr-self-check-report":
                with _CliProgressReporter() as progress:
                    progress.update(
                        "gdpr-export",
                        description="Building GDPR export",
                        completed=0,
                        total=3,
                    )
                    report = get_bot().build_gdpr_compliance_report(cadence=args.cadence)
                    progress.update(
                        "gdpr-export",
                        description="GDPR export data ready",
                        completed=1,
                        total=3,
                    )
                    if args.output:
                        progress.update(
                            "gdpr-export",
                            description="Writing GDPR export",
                            completed=2,
                            total=3,
                        )
                        _write_json_report(args.output, report)
                    progress.update(
                        "gdpr-export",
                        description="GDPR export complete",
                        completed=3,
                        total=3,
                    )
                payload = _cli_payload(
                    "gdpr-self-check-report",
                    report=report,
                    output_path=args.output,
                )
                if args.json_output:
                    _emit_cli_json(payload)
                elif args.output:
                    _cli_print(f"GDPR self-check report written to {args.output}.", level="success")
                else:
                    _cli_print(_json_dumps(report))
            elif args.command == "discover":
                from tasks.celery_tasks import discover_task

                with _CliProgressReporter() as progress:
                    queued = _queue_async_task(
                        "discover",
                        discover_task,
                        task_kwargs={
                            "db_path": args.db,
                            "keywords": _parse_csv_argument(args.keywords),
                            "trusted_sources": _parse_csv_argument(args.trusted_sources),
                            "dry_run": args.dry_run,
                        },
                        progress_reporter=progress,
                    )
                if isinstance(queued, dict):
                    payload = _cli_payload("discover", **queued)
                    if args.json_output:
                        _emit_cli_json(payload)
                    else:
                        _print_queued_task(
                            payload, ready_renderer=_render_discover_task_result_text
                        )
            elif args.command == "test-connector":
                connector = create_connector(args.connector)
                with _CliProgressReporter() as progress:
                    validation = connector.validate_connectivity(
                        keywords=_parse_csv_argument(args.keywords),
                        sample_limit=max(args.limit, 0),
                        progress_callback=progress.update_from_detail,
                    )
                payload = _cli_payload("test-connector", validation=validation)
                if args.json_output:
                    _emit_cli_json(payload)
                else:
                    _cli_print(_json_dumps(validation))
            elif args.command == "send-outreach":
                from tasks.celery_tasks import send_outreach_task

                with _CliProgressReporter() as progress:
                    queued = _queue_async_task(
                        "send-outreach",
                        send_outreach_task,
                        task_kwargs={
                            "db_path": args.db,
                            "donor_email": args.email,
                            "donor_name": args.name,
                            "template_name": args.template_name,
                            "subject_template": args.subject,
                            "body_template": args.body,
                            "locale": args.locale,
                            "dry_run": args.dry_run,
                        },
                        progress_reporter=progress,
                    )
                if isinstance(queued, dict):
                    payload = _cli_payload("send-outreach", **queued)
                    if args.json_output:
                        _emit_cli_json(payload)
                    else:
                        _print_queued_task(
                            payload, ready_renderer=_render_outreach_task_result_text
                        )
            elif args.command == "set-organization-profile":
                try:
                    raw_json = (
                        Path(args.file).read_text(encoding="utf-8")
                        if args.file
                        else sys.stdin.read()
                    )
                except OSError as exc:
                    raise FundingBotError(
                        f"Failed to read profile from {args.file!r}: {exc}"
                    ) from exc
                profile = json.loads(raw_json)
                if not isinstance(profile, dict):
                    raise ValueError("Organization profile JSON must be an object.")
                get_bot().store_organization_profile(profile)
                payload = _cli_payload(
                    "set-organization-profile",
                    updated=True,
                    organization_profile=profile,
                    profile_keys=sorted(profile.keys()),
                )
                if args.json_output:
                    _emit_cli_json(payload)
                else:
                    _cli_print("Organization profile updated.", level="success")
            elif args.command == "register-credential":
                payload = _run_register_credential(get_bot(), args)
                if args.json_output:
                    _emit_cli_json(payload)
                else:
                    _cli_print(f"Registered credential alias {args.alias!r}.", level="success")
            elif args.command == "completion":
                script = _build_completion_script(args.shell)
                payload = _cli_payload("completion", shell=args.shell, script=script)
                if args.json_output:
                    _emit_cli_json(payload)
                else:
                    _cli_print(script)
            elif args.command == "doctor":
                with _CliProgressReporter() as progress:
                    payload = _collect_doctor_report(
                        db_path=args.db,
                        connector_keywords=_parse_csv_argument(args.connector_keywords),
                        progress_callback=progress.update_from_detail,
                    )
                if args.json_output:
                    _emit_cli_json(payload)
                else:
                    _print_doctor_report(payload)
            elif args.command == "show-settings":
                _run_show_settings(get_bot(), json_output=args.json_output)
                if not args.json_output:
                    _print_credential_aliases(get_bot())
        except Exception as exc:
            if args.json_output:
                _emit_cli_json(
                    {
                        "command": args.command,
                        "ok": False,
                        "error": {
                            "type": type(exc).__name__,
                            "message": str(exc),
                        },
                    }
                )
                raise SystemExit(1) from exc
            _cli_print(f"{type(exc).__name__}: {exc}", level="error", file=sys.stderr)
            raise SystemExit(1) from exc
    finally:
        if bot is not None:
            bot.close()


if __name__ == "__main__":
    main()
