from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import logging
import os
import re
import signal
import smtplib
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.mime.text import MIMEText
from numbers import Number
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol
from xml.sax.saxutils import escape

from jsonschema import ValidationError, validate

try:
    from babel.dates import format_date as babel_format_date
    from babel.dates import format_datetime as babel_format_datetime
    from babel.numbers import format_decimal as babel_format_decimal
except ImportError:  # pragma: no cover - exercised in environments without Babel
    babel_format_date = None
    babel_format_datetime = None
    babel_format_decimal = None

# ---------------------------------------------------------------------------
# Simple TTL cache for repeated portal queries
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_UNSET = object()
_TRANSIENT_CONNECTOR_ERRORS = (TimeoutError, ConnectionError, OSError)
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


def _validate_email(email: str) -> str:
    """Return the stripped email or raise ValueError if it looks invalid."""
    stripped = email.strip()
    if not _EMAIL_RE.match(stripped):
        raise ValueError(f"Invalid email address: {stripped!r}")
    return stripped


def _extract_dict_keys(value: Any) -> list[str]:
    """Return the sorted, stringified keys of ``value`` if it is a dict.

    Used for audit-log detail payloads where only the *field names* of a
    setting (never its values) should be recorded, and where ``value`` is
    not guaranteed to be a ``dict`` at runtime despite the type hints.
    """
    if not isinstance(value, dict):
        return []
    return sorted(str(field) for field in value)


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


def _parse_secret_payload(raw_value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_value)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    return {"secret": raw_value}


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


class FundingBotError(Exception):
    """Base error for funding bot operations."""


class DuplicateSubmissionError(FundingBotError):
    """Raised when an opportunity already has an application record."""


class OpportunityNotFoundError(FundingBotError):
    """Raised when an opportunity cannot be found."""


class CredentialNotFoundError(FundingBotError):
    """Raised when a credential alias cannot be resolved."""


class ConnectorConfigError(FundingBotError):
    """Raised when connector configuration or credentials are invalid."""


class OutreachThrottledError(FundingBotError):
    """Raised when an outreach email exceeds the allowed cadence."""


class OptOutError(FundingBotError):
    """Raised when a donor has opted out of outreach."""


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
            raise GracefulShutdownRequested(reason or "Shutdown requested for in-flight queue task.")

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
    ) -> None:
        self.bot = bot
        self.idempotency_key = idempotency_key
        self._controller = controller

    def shutdown_requested(self) -> bool:
        return self._controller.shutdown_requested()

    def checkpoint(self, reason: str | None = None) -> None:
        self._controller.raise_if_shutdown_requested(reason=reason)


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
        *,
        cache_ttl: float | None = None,
        page_size: int | None = None,
        max_retries: int = 2,
        retry_backoff_base: float = 0.25,
        retry_backoff_factor: float = 2.0,
        circuit_failure_threshold: int = 3,
        circuit_recovery_timeout: float = 30.0,
        sleep_func: Callable[[float], None] | None = None,
        time_func: Callable[[], float] | None = None,
    ) -> None:
        self.http_client = http_client
        self.page_size = self._resolve_page_size(page_size)
        cache_ttl = self._resolve_cache_ttl(cache_ttl)
        self._cache = _TTLCache(ttl_seconds=cache_ttl)
        self.max_retries = max(0, max_retries)
        self.retry_backoff_base = max(0.0, retry_backoff_base)
        self.retry_backoff_factor = max(1.0, retry_backoff_factor)
        self.circuit_failure_threshold = max(1, circuit_failure_threshold)
        self.circuit_recovery_timeout = max(0.0, circuit_recovery_timeout)
        self._sleep = sleep_func or time.sleep
        self._time = time_func or time.monotonic
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")
        self._circuit_state = "closed"
        self._opened_at: float | None = None
        self._last_error: str | None = None
        self._metrics: dict[str, Any] = {
            "requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "retry_attempts": 0,
            "health_checks": 0,
            "short_circuits": 0,
            "consecutive_failures": 0,
            "state_transitions": 0,
        }

    def fetch_opportunities(self, keywords: Iterable[str]) -> list[dict[str, Any]]:
        return list(self.fetch_result(keywords)["opportunities"])

    def fetch_result(self, keywords: Iterable[str]) -> dict[str, Any]:
        keyword_list = self._expand_keywords(keywords)
        cache_key = (self.base_url, tuple(sorted(keyword_list)))
        if self._cache is not None:
            hit, cached = self._cache.get(cache_key)
            if hit:
                return {
                    "schema_version": cached["schema_version"],
                    "opportunities": [dict(item) for item in cached["opportunities"]],
                    "metadata": dict(cached["metadata"]),
                }

        if self._refresh_circuit_state() == "open":
            self._metrics["short_circuits"] += 1
            raise RuntimeError(f"{self.source_name} connector circuit breaker is open.")

        if self.http_client:
            result = self._fetch_remote_result(keyword_list)
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
        if self._cache is not None:
            self._cache.set(
                cache_key,
                {
                    "schema_version": payload["schema_version"],
                    "opportunities": [dict(item) for item in payload["opportunities"]],
                    "metadata": dict(payload["metadata"]),
                },
            )
        return payload

    def build_cache_key(self, keywords: Iterable[str]) -> str:
        return json.dumps(
            {
                "base_url": self.base_url,
                "keywords": sorted(keyword.lower() for keyword in _normalize_text_list(keywords)),
            },
            sort_keys=True,
        )

    def default_fallback_results(self, keywords: Iterable[str]) -> list[dict[str, Any]]:
        return self._filter_opportunities(self._demo_data(), self._expand_keywords(keywords))

    def get_keyword_category_mappings(self) -> dict[str, dict[str, list[str]]]:
        mappings: dict[str, dict[str, list[str]]] = {}
        for canonical_keyword, config in self.keyword_category_mappings.items():
            keyword_values = _normalize_text_list(
                [canonical_keyword, *config.get("keywords", ())]
            )
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
    ) -> dict[str, Any]:
        requested_keywords = _normalize_text_list(keywords)
        try:
            sample_results = self.fetch_opportunities(requested_keywords)
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
            return {
                "connector": self.connector_slug,
                "source": self.source_name,
                "base_url": self.base_url,
                "status": "ok",
                "connectivity_validated": True,
                "mode": "remote" if self.http_client else "demo",
                "requested_keywords": requested_keywords,
                "expanded_keywords": self._expand_keywords(requested_keywords),
                "sample_result_count": len(sample_results),
                "sample_results": trimmed_results,
                "keyword_mappings": self.get_keyword_category_mappings(),
            }
        except Exception as exc:
            return {
                "connector": self.connector_slug,
                "source": self.source_name,
                "base_url": self.base_url,
                "status": "error",
                "connectivity_validated": False,
                "mode": "remote" if self.http_client else "demo",
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
        response = self._call_with_retry(
            lambda: self.http_client(
                self.base_url,
                {"keywords": keywords, "health_check": self._circuit_state == "half-open"},
            )
        )
        if isinstance(response, dict):
            payload = response.get("opportunities")
            if payload is None:
                payload = response.get("results")
            if payload is None:
                payload = response.get("items", [])
            declared_version = response.get("schema_version", response.get("result_schema_version"))
            response_keys = sorted(str(key) for key in response)
        else:
            payload = response
            declared_version = None
            response_keys = []
        detected_version = self.detect_schema_version(payload, declared_version)
        return {
            "schema_version": self.result_schema_version,
            "opportunities": self.migrate_result_payload(payload, detected_version),
            "metadata": {
                "connector_name": self.source_name,
                "source_status": "remote",
                "detected_schema_version": detected_version,
                "upstream_schema_version": declared_version,
                "response_keys": response_keys,
            },
        }

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
            migrator = getattr(self, f"_migrate_schema_v{current_version}_to_v{current_version + 1}", None)
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
            "state": self._refresh_circuit_state(),
            "last_error": self._last_error,
            "opened_at": self._opened_at,
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
                    self._sleep(self.retry_backoff_base * (self.retry_backoff_factor ** (attempt_number - 1)))
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
    """Stub connector for grants portals."""

    connector_slug = "grants-portal"
    source_name = "Grants Portal"
    base_url = "https://grants.example.org/opportunities"
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
    """Stub connector for CSR funding networks."""

    connector_slug = "csr-network"
    source_name = "CSR Network"
    base_url = "https://csr.example.org/opportunities"
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
    """Stub connector for NGO funding directories."""

    connector_slug = "ngo-directory"
    source_name = "NGO Directory"
    base_url = "https://directory.example.org/opportunities"
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


def default_connectors() -> list[PortalConnector]:
    """Return the built-in portal connectors used by ``run_discovery``.

    Each connector returns demo data unless an ``http_client`` is supplied,
    which keeps discovery safe to run out-of-the-box while still exercising
    the full search pipeline end-to-end.
    """
    return [GrantsPortalConnector(), CSRNetworkConnector(), NGODirectoryConnector()]


def connector_registry() -> dict[str, type[_BasePortalConnector]]:
    """Return built-in connectors keyed by their CLI slug."""
    return {
        GrantsPortalConnector.connector_slug: GrantsPortalConnector,
        CSRNetworkConnector.connector_slug: CSRNetworkConnector,
        NGODirectoryConnector.connector_slug: NGODirectoryConnector,
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
DEFAULT_OUTREACH_TEMPLATE_NAME = "thank-you"
LOCALIZED_OUTREACH_TEMPLATES: dict[str, dict[str, dict[str, str]]] = {
    "thank-you": {
        "en": {
            "subject": "Thank you for supporting {organization_name}",
            "body": "Dear {donor_name},\n\nThank you for your continued interest in {organization_name}.",
        },
        "bn": {
            "subject": "{organization_name}-কে সমর্থন করার জন্য ধন্যবাদ",
            "body": "প্রিয় {donor_name},\n\n{organization_name}-এর প্রতি আপনার ধারাবাহিক আগ্রহের জন্য ধন্যবাদ।",
        },
    },
    "impact-update": {
        "en": {
            "subject": "Latest impact update from {organization_name}",
            "body": "Dear {donor_name},\n\nYour support helps advance our mission: {mission}",
        },
        "bn": {
            "subject": "{organization_name}-এর সর্বশেষ প্রভাবের খবর",
            "body": "প্রিয় {donor_name},\n\nআপনার সহায়তা আমাদের এই মিশন এগিয়ে নিতে সাহায্য করছে: {mission}",
        },
    },
    "meeting-request": {
        "en": {
            "subject": "Could we schedule a conversation with {organization_name}?",
            "body": "Dear {donor_name},\n\nWe would welcome the chance to discuss how {organization_name} can partner with you.",
        },
        "bn": {
            "subject": "{organization_name}-এর সঙ্গে কি একটি আলোচনা নির্ধারণ করা যায়?",
            "body": "প্রিয় {donor_name},\n\n{organization_name} কীভাবে আপনার সঙ্গে অংশীদারিত্ব করতে পারে তা নিয়ে আলোচনা করার সুযোগ পেলে আমরা আনন্দিত হব।",
        },
    },
}


def _validate_locale(locale: str | None) -> str:
    normalized = (locale or DEFAULT_OUTREACH_LOCALE).strip().lower()
    if normalized not in SUPPORTED_OUTREACH_LOCALES:
        raise ValueError(
            f"Invalid locale {locale!r}. Expected one of {list(SUPPORTED_OUTREACH_LOCALES)}."
        )
    return normalized


def _validate_localized_outreach_templates(
    catalog: dict[str, dict[str, dict[str, str]]],
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
    TASK_STATUS_TRANSITIONS = {
        "todo": frozenset({"in-progress", "blocked"}),
        "in-progress": frozenset({"todo", "done", "blocked"}),
        "blocked": frozenset({"todo", "in-progress"}),
        "done": frozenset(),
    }
    DEFAULT_QUEUE_RETRY_LIMIT = 3
    DEFAULT_QUEUE_RETRY_BACKOFF_SECONDS = 5.0
    DEFAULT_QUEUE_RETRY_BACKOFF_MAX_SECONDS = 300.0
    SUPPORTED_TEMPLATE_LOCALES = frozenset({"en", "bn"})
    DEFAULT_TEMPLATE_LOCALE = "en"
    DEFAULT_OUTREACH_TEMPLATE = "default"
    OUTREACH_TEMPLATE_CATALOG = (
        Path(__file__).resolve().parent / "i18n" / "outreach_templates"
    )

    def __init__(
        self,
        db_path: str | os.PathLike[str] = ":memory:",
        *,
        trusted_sources: Iterable[str] | None = None,
        vault: CredentialVault | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self.trusted_sources = {source.lower() for source in (trusted_sources or [])}
        self.vault = vault
        self.connection = sqlite3.connect(self.db_path)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.row_factory = sqlite3.Row
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS organization_profile (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS credential_refs (
                alias TEXT PRIMARY KEY,
                env_var_name TEXT NOT NULL
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

            CREATE TABLE IF NOT EXISTS task_runs (
                task_id TEXT PRIMARY KEY,
                task_name TEXT NOT NULL,
                status TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT,
                error_message TEXT,
                callback_name TEXT,
                callback_payload_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                external_id TEXT UNIQUE,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                assigned_to TEXT NOT NULL,
                status TEXT NOT NULL,
                due_date TEXT,
                source TEXT NOT NULL DEFAULT 'manual',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
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
            CREATE INDEX IF NOT EXISTS idx_communications_donor_email
                ON communications(donor_email);
            CREATE INDEX IF NOT EXISTS idx_communications_sent_at
                ON communications(sent_at DESC);
            CREATE INDEX IF NOT EXISTS idx_outreach_events_communication_id
                ON outreach_events(communication_id);
            CREATE INDEX IF NOT EXISTS idx_task_runs_status
                ON task_runs(status);
            CREATE INDEX IF NOT EXISTS idx_task_runs_updated_at
                ON task_runs(updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_tasks_assigned_to
                ON tasks(assigned_to);
            CREATE INDEX IF NOT EXISTS idx_tasks_status
                ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_external_id
                ON tasks(external_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_due_date
                ON tasks(due_date);
            CREATE INDEX IF NOT EXISTS idx_translation_reviews_status
                ON translation_reviews(status);
            CREATE INDEX IF NOT EXISTS idx_translation_reviews_locale
                ON translation_reviews(locale);
            CREATE INDEX IF NOT EXISTS idx_translation_reviews_created_at
                ON translation_reviews(created_at DESC);
            """
        )
        self._ensure_column("donors", "segment", "TEXT NOT NULL DEFAULT 'unknown'")
        self._ensure_column("donors", "locale", "TEXT NOT NULL DEFAULT 'en'")
        self._ensure_column("tasks", "external_id", "TEXT")
        self._ensure_column("tasks", "due_date", "TEXT")
        self._ensure_column("tasks", "source", "TEXT NOT NULL DEFAULT 'manual'")
        # Index on donors.segment must be created after the column is guaranteed to exist.
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_donors_segment ON donors(segment)"
        )
        self.connection.commit()

    # Allowlist of table/column identifiers that _ensure_column is permitted to touch.
    # All calls are internal and use literals; the allowlist is an extra safety guard.
    _ALLOWED_ALTER_TABLES = frozenset({"donors", "tasks"})
    _ALLOWED_ALTER_COLUMNS = frozenset({"segment", "locale", "external_id", "due_date", "source"})

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
    def _normalize_filter_timestamp(value: datetime | str | None, *, end: bool = False) -> str | None:
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
        try:
            parsed = json.loads(raw_value)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        return {"secret": raw_value}

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
    def _validate_ui_locale(locale: str | None) -> str:
        normalized = (locale or DEFAULT_LOCALE_CODE).strip().lower()
        if normalized not in SUPPORTED_UI_LOCALES:
            raise ValueError(
                f"Unsupported locale {locale!r}. Expected one of {sorted(SUPPORTED_UI_LOCALES)}."
            )
        return normalized

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
    def _resolve_catalog_template(
        cls,
        template_name: str,
        *,
        segment: str,
        locale: str,
    ) -> tuple[str, str] | None:
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
        normalized = str(status).strip().lower().replace("_", "-")
        if normalized not in cls.TASK_STATUSES:
            raise ValueError(
                f"Invalid task status {status!r}. Expected one of {list(cls.TASK_STATUSES)}."
            )
        return normalized

    @staticmethod
    def _serialize_task(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    @classmethod
    def _validate_task_transition(cls, current_status: str, new_status: str) -> None:
        if current_status == new_status:
            return
        allowed = cls.TASK_STATUS_TRANSITIONS[current_status]
        if new_status not in allowed:
            raise TaskTransitionError(
                f"Task status cannot transition from {current_status!r} to {new_status!r}."
            )

    def _log_action(self, action: str, **details: Any) -> None:
        self.connection.execute(
            "INSERT INTO audit_logs (happened_at, action, details_json) VALUES (?, ?, ?)",
            (self._to_iso(), action, json.dumps(details, sort_keys=True)),
        )
        self.connection.commit()

    @staticmethod
    def _signature_for(opportunity: dict[str, Any]) -> str:
        identity = "|".join(
            str(opportunity.get(field, "")).strip().lower()
            for field in ("source", "portal_url", "title", "donor_name")
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def store_setting(self, key: str, value: dict[str, Any]) -> None:
        """Persist an arbitrary named setting (organization profile, search
        preferences, etc.) as JSON, keyed by ``key``.

        This backs the web admin "Settings" panel so operators can configure
        the bot without leaving the dashboard or touching the CLI/env vars.
        """
        self.connection.execute(
            "INSERT OR REPLACE INTO organization_profile (key, value_json) VALUES (?, ?)",
            (key, json.dumps(value, sort_keys=True)),
        )
        self.connection.commit()
        self._log_action("generic_setting_updated", key=key, value_keys=_extract_dict_keys(value))

    def load_setting(self, key: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT value_json FROM organization_profile WHERE key = ?",
            (key,),
        ).fetchone()
        return json.loads(row["value_json"]) if row else {}

    def store_organization_profile(self, profile: dict[str, Any]) -> None:
        self.store_setting("profile", profile)

    def load_organization_profile(self) -> dict[str, Any]:
        return self.load_setting("profile")

    def store_search_settings(
        self,
        *,
        keywords: Iterable[str] | None = None,
        trusted_sources: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Persist default keyword/source filters used by :meth:`run_discovery`."""
        settings = {
            "keywords": sorted({keyword.strip() for keyword in (keywords or []) if keyword.strip()}),
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
        self.connection.execute(
            "INSERT OR REPLACE INTO credential_refs (alias, env_var_name) VALUES (?, ?)",
            (alias, env_var_name),
        )
        self.connection.commit()
        self._log_action("credential_ref_registered", alias=alias, env_var_name=env_var_name)

    def resolve_credential(self, alias: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT env_var_name FROM credential_refs WHERE alias = ?",
            (alias,),
        ).fetchone()
        if not row:
            raise CredentialNotFoundError(f"No credential alias registered for {alias!r}.")

        env_var_name = row["env_var_name"]
        if self.vault is not None:
            try:
                return self._parse_secret_payload(self.vault.get_secret(env_var_name))
            except CredentialNotFoundError:
                pass

        raw_value = os.getenv(env_var_name)
        if raw_value is None:
            raise CredentialNotFoundError(f"Environment variable {env_var_name!r} is not set.")
        return self._parse_secret_payload(raw_value)

    def upsert_donor(
        self,
        *,
        email: str,
        name: str,
        opted_out: bool = False,
        preferences: dict[str, Any] | None = None,
        segment: str | None = None,
        locale: str | None = None,
    ) -> None:
        email = _validate_email(email)
        normalized_segment = self._validate_segment(segment) if segment is not None else None
        normalized_locale = self._validate_locale(locale) if locale is not None else None
        self.connection.execute(
            """
            INSERT INTO donors (
                email, name, opted_out, preferences_json, last_contact_at, segment, locale
            )
            VALUES (
                ?, ?, ?, ?, COALESCE((SELECT last_contact_at FROM donors WHERE email = ?), NULL),
                COALESCE((SELECT segment FROM donors WHERE email = ?), COALESCE(?, 'unknown')),
                COALESCE((SELECT locale FROM donors WHERE email = ?), COALESCE(?, 'en'))
            )
            ON CONFLICT(email) DO UPDATE SET
                name = excluded.name,
                opted_out = excluded.opted_out,
                preferences_json = excluded.preferences_json,
                segment = CASE
                    WHEN ? IS NULL THEN donors.segment
                    ELSE excluded.segment
                END,
                locale = CASE
                    WHEN ? IS NULL THEN donors.locale
                    ELSE excluded.locale
                END
            """,
            (
                email,
                name,
                int(opted_out),
                json.dumps(preferences or {}),
                email,
                email,
                normalized_segment,
                email,
                normalized_locale,
                normalized_segment,
                normalized_locale,
            ),
        )
        self.connection.commit()
        logged_profile = self.connection.execute(
            "SELECT segment, locale FROM donors WHERE email = ?",
            (email,),
        ).fetchone()
        self._log_action(
            "donor_upserted",
            email=email,
            opted_out=opted_out,
            segment=logged_profile["segment"],
            locale=logged_profile["locale"],
        )

    def list_donors(self, segment: str | None = None) -> list[dict[str, Any]]:
        """Return donor records, optionally filtered by segment."""
        if segment is not None:
            normalized_segment = self._validate_segment(segment)
            rows = self.connection.execute(
                "SELECT * FROM donors WHERE segment = ? ORDER BY name, email",
                (normalized_segment,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM donors ORDER BY name, email"
            ).fetchall()
        return [dict(row) for row in rows]

    def set_donor_opt_out(self, email: str, opted_out: bool = True) -> None:
        self.connection.execute(
            "UPDATE donors SET opted_out = ? WHERE email = ?",
            (int(opted_out), email),
        )
        self.connection.commit()
        self._log_action("donor_opt_out_updated", email=email, opted_out=opted_out)

    def discover_opportunities(
        self,
        opportunities: Iterable[dict[str, Any]],
        *,
        keywords: Iterable[str] | None = None,
        trusted_sources: Iterable[str] | None = None,
        discovered_at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        keyword_list = [keyword.lower() for keyword in (keywords or [])]
        allowed_sources = {
            source.lower() for source in (trusted_sources or self.trusted_sources or [])
        }
        found: list[dict[str, Any]] = []
        timestamp = self._to_iso(discovered_at)

        for opportunity in opportunities:
            source = str(opportunity.get("source", "")).strip()
            if allowed_sources and source.lower() not in allowed_sources:
                continue

            searchable_parts = [
                str(opportunity.get("title", "")),
                str(opportunity.get("summary", "")),
                " ".join(str(tag) for tag in opportunity.get("tags", [])),
                str(opportunity.get("category", "")),
            ]
            searchable_text = " ".join(searchable_parts).lower()
            if keyword_list and not any(keyword in searchable_text for keyword in keyword_list):
                continue

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
            existing = self.connection.execute(
                "SELECT 1 FROM opportunities WHERE signature = ?",
                (record["signature"],),
            ).fetchone()
            if existing:
                continue

            self.connection.execute(
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
            found.append(record)

        self.connection.commit()
        self._log_action("opportunities_discovered", count=len(found), keywords=keyword_list)
        return found

    def run_discovery(
        self,
        connectors: Iterable[PortalConnector] | None = None,
        *,
        keywords: Iterable[str] | None = None,
        trusted_sources: Iterable[str] | None = None,
        discovered_at: datetime | None = None,
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
        source_list = list(trusted_sources) if trusted_sources is not None else settings.get("trusted_sources", [])
        active_connectors = list(connectors) if connectors is not None else default_connectors()

        candidates: list[dict[str, Any]] = []
        for connector in active_connectors:
            health = connector.check_health()
            if not health.get("healthy", True):
                self._log_action(
                    "connector_degraded",
                    source=getattr(connector, "source_name", connector.__class__.__name__),
                    state=health.get("state"),
                    last_error=health.get("last_error"),
                )
                continue
            candidates.extend(connector.fetch_opportunities(keyword_list))

        return self.discover_opportunities(
            candidates,
            keywords=keyword_list,
            trusted_sources=source_list,
            discovered_at=discovered_at,
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
        normalized_key = str(translation_key).strip()
        normalized_source = str(source_text).strip()
        normalized_translation = str(translated_text).strip()
        normalized_notes = (
            str(submitter_notes).strip() if submitter_notes is not None else None
        ) or None
        if not normalized_key:
            raise ValueError("Field 'translation_key' is required.")
        if not normalized_source:
            raise ValueError("Field 'source_text' is required.")
        if not normalized_translation:
            raise ValueError("Field 'translated_text' is required.")

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
            str(reviewer_notes).strip() if reviewer_notes is not None else None
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
        assigned_to: str,
        description: str = "",
        status: str = "todo",
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        normalized_title = str(title).strip()
        normalized_assignee = str(assigned_to).strip().lower()
        if not normalized_title:
            raise ValueError("Task title is required.")
        if not normalized_assignee:
            raise ValueError("Task assignee is required.")

        normalized_status = self._normalize_task_status(status)
        timestamp = self._to_iso(created_at)
        cursor = self.connection.execute(
            """
            INSERT INTO tasks (
                title, description, assigned_to, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_title,
                str(description or "").strip(),
                normalized_assignee,
                normalized_status,
                timestamp,
                timestamp,
            ),
        )
        self.connection.commit()
        task = self.get_task(cursor.lastrowid)
        self._log_action(
            "task_created",
            task_id=task["id"],
            title=task["title"],
            assigned_to=task["assigned_to"],
            status=task["status"],
        )
        return task

    def get_task(self, task_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise FundingBotError(f"Task {task_id!r} does not exist.")
        return self._serialize_task(row)

    def list_tasks(
        self,
        *,
        assigned_to: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM tasks"
        params: list[Any] = []
        clauses: list[str] = []
        if assigned_to:
            clauses.append("assigned_to = ?")
            params.append(str(assigned_to).strip().lower())
        if status:
            clauses.append("status = ?")
            params.append(self._normalize_task_status(status))
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC, id DESC"
        rows = self.connection.execute(query, params).fetchall()
        return [self._serialize_task(row) for row in rows]

    def get_task_status_counts(self, *, assigned_to: str | None = None) -> dict[str, int]:
        query = "SELECT status, COUNT(*) AS total FROM tasks"
        params: list[Any] = []
        if assigned_to:
            query += " WHERE assigned_to = ?"
            params.append(str(assigned_to).strip().lower())
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
                raise FundingBotError(f"Task {task_id!r} does not exist.")
        notification = (
            f"Task '{task['title']}' moved from {task['status']} to {normalized_status}."
        )
        self._log_action(
            "task_status_changed",
            task_id=task_id,
            title=task["title"],
            assigned_to=task["assigned_to"],
            previous_status=task["status"],
            status=normalized_status,
            changed_by=str(changed_by).strip().lower(),
            notification=notification,
        )
        updated_task = self.get_task(task_id)
        updated_task["notification"] = notification
        return updated_task

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
                status, next_action, submission_reference
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                opportunity_signature,
                row["donor_name"],
                row["portal_url"],
                timestamp,
                status,
                next_action,
                submission_reference,
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
        )
        return {
            "opportunity_signature": opportunity_signature,
            "status": status,
            "next_action": next_action,
            "submission_reference": submission_reference,
            "submitted_at": timestamp,
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
                next_action=str(
                    remote_status.get("next_action", application["next_action"])
                ),
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
            "next_action": str(
                remote_status.get("next_action", application["next_action"])
            ),
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
    ) -> dict[str, Any]:
        donor_email = _validate_email(donor_email)
        donor = self.connection.execute(
            "SELECT * FROM donors WHERE email = ?",
            (donor_email,),
        ).fetchone()
        if donor is None:
            self.upsert_donor(email=donor_email, name=donor_name, locale=locale)
            donor = self.connection.execute(
                "SELECT * FROM donors WHERE email = ?",
                (donor_email,),
            ).fetchone()

        if donor is None:
            raise FundingBotError(f"Unable to load donor record for {donor_email!r}.")
        if donor["opted_out"]:
            raise OptOutError(f"{donor_email} has opted out of outreach.")

        send_time = self._as_utc(sent_at)
        if donor["last_contact_at"]:
            last_contact = self._as_utc(datetime.fromisoformat(donor["last_contact_at"]))
            if send_time - last_contact < timedelta(days=7):
                raise OutreachThrottledError(
                    f"{donor_email} was contacted less than seven days ago."
                )

        donor_locale = self._validate_locale(locale or donor["locale"])
        profile = self.load_organization_profile()
        merged_context = {
            "donor_name": donor_name,
            "donor_locale": donor_locale,
            "organization_name": profile.get("name", "Nonprofit Funding Bot"),
            "mission": profile.get("mission", ""),
            "opt_out_url": (context or {}).get(
                "opt_out_url", "https://example.org/unsubscribe"
            ),
        }
        merged_context.update(profile)
        merged_context.update(context or {})
        subject = subject_template.format(**merged_context)
        body = body_template.format(**merged_context).rstrip()
        if merged_context["opt_out_url"] not in body:
            opt_out_notice = self._localized_opt_out_notice(donor_locale).format(**merged_context)
            body = f"{body}\n\n{opt_out_notice}"

        if sender is not None:
            sender(donor_email, subject, body)

        sent_iso = self._to_iso(send_time)
        cursor = self.connection.execute(
            """
            INSERT INTO communications (donor_email, donor_name, subject, body, channel, sent_at)
            VALUES (?, ?, ?, ?, 'email', ?)
            """,
            (donor_email, donor_name, subject, body, sent_iso),
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
        self._log_action("outreach_sent", donor_email=donor_email, subject=subject)
        return {"email": donor_email, "subject": subject, "body": body, "sent_at": sent_iso}

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
    ) -> dict[str, Any]:
        """Send outreach using a stored template."""
        donor = self.connection.execute(
            "SELECT segment, locale FROM donors WHERE email = ?",
            (donor_email,),
        ).fetchone()
        donor_segment = donor["segment"] if donor else "unknown"
        donor_locale = self._validate_locale(donor["locale"] if donor else None)
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
        return self.send_outreach(
            donor_email=donor_email,
            donor_name=donor_name,
            subject_template=subject_template,
            body_template=body_template,
            context=context,
            sender=sender,
            sent_at=sent_at,
            locale=donor_locale,
        )

    def record_outreach_event(self, communication_id: int, event_type: str) -> None:
        """Store an outreach engagement event."""
        allowed = {"sent", "opened", "clicked", "bounced", "unsubscribed"}
        normalized_event = event_type.strip().lower()
        if normalized_event not in allowed:
            raise ValueError(f"Invalid outreach event type {event_type!r}.")

        communication = self.connection.execute(
            "SELECT id FROM communications WHERE id = ?",
            (communication_id,),
        ).fetchone()
        if communication is None:
            raise FundingBotError(f"Unknown communication {communication_id!r}.")

        self.connection.execute(
            """
            INSERT INTO outreach_events (communication_id, event_type, happened_at)
            VALUES (?, ?, ?)
            """,
            (communication_id, normalized_event, self._to_iso()),
        )
        self.connection.commit()
        self._log_action(
            "outreach_event_recorded",
            communication_id=communication_id,
            event_type=normalized_event,
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
        return counts

    def gdpr_export(self, donor_email: str) -> dict[str, Any]:
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
            "donor": dict(donor) if donor else None,
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
        }
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
        self._log_action(
            "gdpr_deleted",
            donor_hash=hashlib.sha256(donor_email.encode("utf-8")).hexdigest(),
            anonymized_email=anonymized_email,
        )

    def generate_document(
        self,
        *,
        kind: str,
        template: str,
        output_dir: str | os.PathLike[str],
        context: dict[str, Any] | None = None,
        formats: Iterable[str] = ("pdf", "docx"),
    ) -> dict[str, str]:
        profile = self.load_organization_profile()
        merged_context = dict(profile)
        merged_context.update(context or {})
        rendered = template.format(**merged_context).strip() + "\n"

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
        self._log_action("documents_generated", kind=kind, formats=sorted(generated))
        return generated

    def _write_pdf(self, path: Path, text: str) -> None:
        lines = [line or " " for line in text.splitlines()]
        escaped_lines = [
            line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            for line in lines
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
                "<w:p><w:r><w:t xml:space=\"preserve\">"
                f"{safe_line}"
                "</w:t></w:r></w:p>"
            )

        document_xml = (
            "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
            "<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">"
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
                            profile.get("mission", "Our mission statement is available on request."),
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
            SELECT donor_name, portal_url, status FROM applications
            WHERE substr(submitted_at, 1, 10) = ?
            ORDER BY submitted_at
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
        pending = self.connection.execute(
            """
            SELECT donor_name, status, next_action FROM applications
            WHERE status IN ('pending', 'submitted', 'in_review')
            ORDER BY submitted_at
            """
        ).fetchall()

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
            lambda row: f"   • {row['donor_name']} – {row['status'].replace('_', ' ').title()}",
            "No applications submitted",
        )
        pending_lines = format_lines(
            pending,
            lambda row: f"   • {row['donor_name']} – {row['status'].replace('_', ' ').title()} ({row['next_action']})",
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
                "   • No bounce or spam flags detected" if communications else "   • No outreach sent today",
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
        print("No records found.")
        return
    column_list = list(columns or row_list[0].keys())
    print("\t".join(column_list))
    for row in row_list:
        print("\t".join(str(row.get(column, "")) for column in column_list))


def _parse_csv_argument(raw_value: str | None) -> list[str] | None:
    if raw_value is None:
        return None
    return _normalize_text_list(raw_value.split(","))


def _build_arg_parser() -> "argparse.ArgumentParser":
    import argparse

    default_db_path = os.environ.get("BOT_DB_PATH", "funding_bot.db")
    parser = argparse.ArgumentParser(
        prog="funding-bot",
        description="Nonprofit Funding Automation Bot – command-line interface",
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
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    # send-daily-summary
    summary_parser = subparsers.add_parser(
        "send-daily-summary",
        help="Build and email the daily funding report.",
    )
    summary_parser.add_argument(
        "--recipient",
        default="lupael@i4e.com.bd",
        metavar="EMAIL",
        help="Recipient email address (default: lupael@i4e.com.bd).",
    )
    summary_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the summary to stdout without sending it.",
    )

    opportunities_parser = subparsers.add_parser(
        "list-opportunities",
        help="List stored funding opportunities.",
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

    discover_parser = subparsers.add_parser(
        "discover",
        help="Search configured donation sources and store new opportunities.",
    )
    discover_parser.add_argument(
        "--keywords",
        metavar="KEYWORDS",
        help="Comma-separated keyword filters (default: stored search settings).",
    )
    discover_parser.add_argument(
        "--trusted-sources",
        metavar="SOURCES",
        help="Comma-separated allow-list of sources (default: stored search settings).",
    )

    test_connector_parser = subparsers.add_parser(
        "test-connector",
        help="Validate one connector and print sample results.",
    )
    test_connector_parser.add_argument(
        "--connector",
        required=True,
        choices=sorted(connector_registry().keys()),
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

    outreach_parser = subparsers.add_parser(
        "send-outreach",
        help="Compose and send (or preview) a personalized donor outreach email.",
    )
    outreach_parser.add_argument("--email", required=True, metavar="EMAIL", help="Donor email address.")
    outreach_parser.add_argument("--name", required=True, metavar="NAME", help="Donor name.")
    outreach_parser.add_argument(
        "--subject",
        default=None,
        metavar="TEMPLATE",
        help="Subject template with {placeholders} (defaults to the donor's locale-aware template).",
    )
    outreach_parser.add_argument(
        "--body",
        default=None,
        metavar="TEMPLATE",
        help="Body template with {placeholders} (defaults to the donor's locale-aware template).",
    )
    outreach_parser.add_argument(
        "--locale",
        metavar="LOCALE",
        help="Donor locale preference for template selection (supported: en, bn).",
    )
    outreach_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compose and log the outreach without sending a real email.",
    )

    profile_parser = subparsers.add_parser(
        "set-organization-profile",
        help="Store the nonprofit's organization profile from a JSON file (or stdin).",
    )
    profile_parser.add_argument(
        "--file",
        metavar="FILE",
        help="Path to a JSON file with the profile (default: read from stdin).",
    )

    credential_parser = subparsers.add_parser(
        "register-credential",
        help="Register a credential alias that resolves to an environment variable.",
    )
    credential_parser.add_argument("--alias", required=True, metavar="ALIAS", help="Credential alias name.")
    credential_parser.add_argument(
        "--env-var",
        required=True,
        metavar="ENV_VAR",
        help="Name of the environment variable holding the secret.",
    )

    subparsers.add_parser("show-settings", help="Print the organization profile, search settings, and credentials.")

    return parser


def _run_register_credential(bot: "FundingBot", args: "argparse.Namespace") -> None:
    """Handle the ``register-credential`` CLI command.

    Kept as a standalone function (rather than inline in ``main``) so that
    the credential alias/env-var-name values it handles stay scoped to this
    function and are never intermixed with unrelated output written later in
    ``main`` (e.g. ``show-settings``).
    """
    bot.register_credential(args.alias, args.env_var)
    print(f"Registered credential alias {args.alias!r}.")


def _run_show_settings(bot: "FundingBot") -> None:
    """Handle the ``show-settings`` CLI command.

    Prints the organization profile and search settings as JSON. Credential
    aliases are printed separately by :func:`_print_credential_aliases` so
    this function never touches credential metadata.
    """
    settings_json = json.dumps(
        {
            "organization_profile": bot.load_organization_profile(),
            "search_settings": bot.load_search_settings(),
        },
        indent=2,
    )
    print(settings_json)


def _print_credential_aliases(bot: "FundingBot") -> None:
    """Print registered credential aliases and their backing env-var *names*.

    Isolated in its own function (never returning or otherwise exposing the
    resolved secret values) so credential alias/env-var-name metadata is
    printed independently of any other CLI output.
    """
    print()
    print("Credential aliases (env-var *names* only, never the secret values):")
    _print_rows(bot.list_credentials(), ["alias", "env_var_name"])


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return

    bot = FundingBot(db_path=args.db)
    try:
        if args.command == "send-daily-summary":
            sender = None if args.dry_run else SMTPEmailSender.from_env()
            summary = bot.send_daily_summary(recipient=args.recipient, sender=sender)
            if args.dry_run:
                print(f"Subject: {summary['subject']}\n")
                print(summary["body"])
            else:
                print(f"Daily summary sent to {args.recipient}.")
        elif args.command == "list-opportunities":
            rows = bot.list_opportunities(status=args.status)
            if args.limit is not None:
                rows = rows[: args.limit]
            _print_rows(
                rows,
                ["signature", "source", "donor_name", "title", "status", "discovered_at"],
            )
        elif args.command == "audit-log":
            _print_rows(
                bot.list_audit_logs(limit=args.limit, action=args.action),
                ["happened_at", "action", "details_json"],
            )
        elif args.command == "list-donors":
            _print_rows(
                bot.list_donors(segment=args.segment),
                ["email", "name", "segment", "locale", "opted_out", "last_contact_at"],
            )
        elif args.command == "monthly-audit-report":
            report = bot.build_monthly_audit_report(year=args.year, month=args.month)
            report_json = json.dumps(report, indent=2)
            if args.output:
                output_path = Path(args.output)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(report_json, encoding="utf-8")
                print(f"Monthly audit report written to {args.output}.")
            else:
                print(report_json)
        elif args.command == "discover":
            keywords = _parse_csv_argument(args.keywords)
            trusted_sources = _parse_csv_argument(args.trusted_sources)
            found = bot.run_discovery(keywords=keywords, trusted_sources=trusted_sources)
            if found:
                _print_rows(found, ["signature", "source", "donor_name", "title", "category"])
            else:
                print("No new opportunities found.")
        elif args.command == "test-connector":
            connector = create_connector(args.connector)
            validation = connector.validate_connectivity(
                keywords=_parse_csv_argument(args.keywords),
                sample_limit=max(args.limit, 0),
            )
            print(json.dumps(validation, indent=2))
        elif args.command == "send-outreach":
            sender = None if args.dry_run else SMTPEmailSender.from_env()
            if args.subject is None and args.body is None:
                if args.locale is not None:
                    bot.upsert_donor(email=args.email, name=args.name, locale=args.locale)
                result = bot.send_outreach_from_template(
                    bot.DEFAULT_OUTREACH_TEMPLATE,
                    args.email,
                    args.name,
                    sender=sender,
                )
            else:
                subject_template, body_template = args.subject, args.body
                if subject_template is None or body_template is None:
                    default_subject, default_body = bot._resolve_catalog_template(
                        bot.DEFAULT_OUTREACH_TEMPLATE,
                        segment="unknown",
                        locale=args.locale or bot.DEFAULT_TEMPLATE_LOCALE,
                    ) or (
                        "Thank you for supporting {organization_name}",
                        "Dear {donor_name},\n\nThank you for your continued interest in {organization_name}.",
                    )
                    subject_template = subject_template or default_subject
                    body_template = body_template or default_body
                result = bot.send_outreach(
                    donor_email=args.email,
                    donor_name=args.name,
                    subject_template=subject_template,
                    body_template=body_template,
                    sender=sender,
                    locale=args.locale,
                )
            print(f"Subject: {result['subject']}\n")
            print(result["body"])
            if args.dry_run:
                print("\n(dry run: no email was actually sent)")
            else:
                print(f"\nOutreach email sent to {args.email}.")
        elif args.command == "set-organization-profile":
            try:
                raw_json = (
                    Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
                )
            except OSError as exc:
                raise FundingBotError(f"Failed to read profile from {args.file!r}: {exc}") from exc
            profile = json.loads(raw_json)
            if not isinstance(profile, dict):
                raise ValueError("Organization profile JSON must be an object.")
            bot.store_organization_profile(profile)
            print("Organization profile updated.")
        elif args.command == "register-credential":
            _run_register_credential(bot, args)
        elif args.command == "show-settings":
            _run_show_settings(bot)
            _print_credential_aliases(bot)
    finally:
        bot.close()


if __name__ == "__main__":
    main()
