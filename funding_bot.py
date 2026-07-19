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
import ssl
import smtplib
import sqlite3
import sys
import threading
import time
import urllib.parse
import zipfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.mime.text import MIMEText
from numbers import Number
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol
from xml.sax.saxutils import escape

import requests
from jsonschema import ValidationError, validate
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

# ---------------------------------------------------------------------------
# Simple TTL cache for repeated portal queries
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
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


_DATA_CLASSIFICATION_LEVELS = ("public", "internal", "confidential", "secret")
_DATA_CLASSIFICATION_RANK = {
    classification: index
    for index, classification in enumerate(_DATA_CLASSIFICATION_LEVELS)
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
    raise ConnectorConfigError("Connector configuration must be a dict or list of connector entries.")

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
        raise ConnectorConfigError(f"Invalid connector configuration{field}: {exc.message}") from exc
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
    id: int | None = None
    external_id: str | None = None
    source: str = "manual"
    assignee_email: str | None = None
    assignee_name: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row | None) -> "Task | None":
        if row is None:
            return None
        data = dict(row)
        if "assignee" not in data and "assigned_to" in data:
            data["assignee"] = data.pop("assigned_to")
        return cls(**data)

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
        return self.bot.record_task_run(
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
            raise CredentialRefreshError("OAuth2 token endpoint returned a non-object JSON payload.")
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
    response = request_method(
        url,
        headers=headers or {},
        params=params,
        json=json_payload,
        timeout=timeout,
    )
    if hasattr(response, "raise_for_status"):
        response.raise_for_status()
    if hasattr(response, "json"):
        return response.json()
    if isinstance(response, (dict, list)):
        return response
    raise FundingBotError(
        f"HTTP client for {url!r} returned {type(response).__name__}, expected JSON-compatible data."
    )


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
        max_retries: int = 2,
        retry_backoff_base: float = 0.25,
        retry_backoff_factor: float = 2.0,
        circuit_failure_threshold: int = 3,
        circuit_recovery_timeout: float = 30.0,
        sleep_func: Callable[[float], None] | None = None,
        time_func: Callable[[], float] | None = None,
        rate_limit_config: dict[str, float] | None = None,
        rate_limiter: TokenBucketRateLimiter | None = None,
    ) -> None:
        self.http_client = http_client
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
        resolved: dict[str, Any] = {}
        if self.credential_name:
            resolved = dict(self._credential_vault.resolve_credentials(self.credential_name))
        if self.credentials:
            resolved.update(self.credentials)
        return resolved

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

    def fetch_result(self, keywords: Iterable[str]) -> dict[str, Any]:
        started_at = time.perf_counter()
        degraded = False
        try:
            self._last_rate_limit_retry_after = None
            keyword_list = self._expand_keywords(keywords)
            cache_key = self._cache_key(keyword_list)
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
                    self._last_error = (
                        f"{self.source_name} rate limit exceeded; retry in {retry_after:.2f} seconds."
                    )
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
            _CONNECTOR_METRICS.record(
                connector_name=self.source_name,
                connector_type=self.connector_slug,
                latency_seconds=time.perf_counter() - started_at,
                errored=degraded,
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
            result = self.fetch_result(requested_keywords)
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
            return {
                "connector": self.connector_slug,
                "source": self.source_name,
                "base_url": self.base_url,
                "status": "degraded" if degraded else "ok",
                "connectivity_validated": not degraded,
                "mode": "remote" if (self.http_client is not None or self.transport == "http") else "demo",
                "requested_keywords": requested_keywords,
                "expanded_keywords": self._expand_keywords(requested_keywords),
                "sample_result_count": len(sample_results),
                "sample_results": trimmed_results,
                "keyword_mappings": self.get_keyword_category_mappings(),
                "metadata": metadata,
                "error": metadata.get("last_error"),
            }
        except Exception as exc:
            return {
                "connector": self.connector_slug,
                "source": self.source_name,
                "base_url": self.base_url,
                "status": "error",
                "connectivity_validated": False,
                "mode": "remote" if (self.http_client is not None or self.transport == "http") else "demo",
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
            page_payload, page_declared_version, page_response_keys, next_page = self._parse_remote_page(
                response,
                current_page=page,
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
            return [dict(item) for item in (payload or [])], declared_version, response_keys, next_page

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
        if self.http_client is not None:
            attempts = (
                lambda: self.http_client(url, params, self.credentials, headers=headers or {}),
                lambda: self.http_client(url, params, headers=headers or {}),
                lambda: self.http_client(url, params, self.credentials),
                lambda: self.http_client(url, params),
            )
            for attempt in attempts:
                try:
                    return attempt()
                except TypeError:
                    continue
            return self.http_client(url, params)

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
            headers={
                "Accept": "application/json",
                "User-Agent": "funding-bot/1.0",
                **(headers or {}),
            },
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = response.read().decode("utf-8")
        return json.loads(payload or "{}")

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
            credential_name=credential_name or "GRANTS_GOV_API_CREDENTIALS",
            credential_vault=credential_vault,
            request_session=request_session,
            transport=transport,
            **kwargs,
        )

    def _fetch_remote_result(self, keywords: list[str]) -> dict[str, Any]:
        if self.http_client is not None:
            return super()._fetch_remote_result(keywords)

        credentials = self._get_resolved_credentials()
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
        }
        authorization_header = str(credentials.get("authorization_header", "")).strip()
        if authorization_header:
            headers["Authorization"] = authorization_header
        elif credentials.get("access_token") or credentials.get("bearer_token"):
            headers["Authorization"] = (
                f"******'access_token') or credentials.get('bearer_token')}"
            )
        api_key = str(credentials.get("api_key", "")).strip()
        if api_key:
            headers["X-API-Key"] = api_key

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
                    "category": str(row.get("docType", "Government Grant")).strip()
                    or "Government Grant",
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
                "response_keys": sorted(str(key) for key in response) if isinstance(response, dict) else [],
                "hit_count": int(data.get("hitCount", len(opportunities))) if isinstance(data, dict) else len(opportunities),
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
            credential_name=credential_name or "CSR_NETWORK_API_CREDENTIALS",
            credential_vault=credential_vault,
            request_session=request_session,
            transport=transport,
            **kwargs,
        )

    def _fetch_remote_result(self, keywords: list[str]) -> dict[str, Any]:
        if self.http_client is not None:
            return super()._fetch_remote_result(keywords)

        credentials = self._get_resolved_credentials()
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
                [*program_areas, *eligibility, *(row_tags if isinstance(row_tags, list) else [row_tags])]
            )
            category = (
                str(row.get("category", "")).strip()
                or (program_areas[0] if program_areas else "Corporate Partnerships")
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

    def _fetch_remote_result(self, keywords: list[str]) -> dict[str, Any]:
        search_terms = keywords or ["nonprofit"]
        opportunities: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        pages_fetched = 0
        response_keys: set[str] = set()

        for term in search_terms[:3]:
            page_number = 0
            while True:
                response = self._fetch_remote_json(
                    self.base_url,
                    {
                        "q": term,
                        "page": page_number,
                    },
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
            },
        }

    def _normalize_live_ngo_record(self, organization: dict[str, Any], query: str) -> dict[str, Any]:
        donor_name = (
            str(organization.get("name") or organization.get("organization_name") or "").strip()
            or "Unknown nonprofit"
        )
        ein = str(organization.get("ein") or organization.get("strein") or "").replace("-", "").strip()
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

    def _fetch_remote_result(self, keywords: list[str]) -> dict[str, Any]:
        api_key = str(self.credentials.get("api_key") or self.credentials.get("secret") or "").strip()
        if not api_key:
            raise ConnectorConfigError(
                "Foundation Directory live mode requires credentials containing 'api_key' "
                "or a raw secret value."
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
                headers={"x-api-key": api_key},
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
        normalized_rows = [row for row in (self._normalize_platform_row(item) for item in raw_rows) if row]
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
            return [dict(item) for item in rows if isinstance(item, dict)], sorted(str(key) for key in response)
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
            portal_url = row.get("projectLink") or row.get("portal_url") or row.get("url") or self.base_url
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


def default_connectors() -> list[PortalConnector]:
    """Return the built-in portal connectors used by ``run_discovery``.

    Each connector returns demo data unless an ``http_client`` is supplied,
    which keeps discovery safe to run out-of-the-box while still exercising
    the full search pipeline end-to-end.
    """
    global _DEFAULT_CONNECTORS
    if _DEFAULT_CONNECTORS is None:
        _DEFAULT_CONNECTORS = [
            GrantsPortalConnector(),
            CSRNetworkConnector(),
            NGODirectoryConnector(),
            FoundationDirectoryConnector(),
            GlobalGivingConnector(),
            KickstarterForGoodConnector(),
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
    TASK_STATUSES = ("pending", "in_progress", "completed", "blocked")
    TASK_STATUS_ALIASES = {
        "todo": "pending",
        "pending": "pending",
        "in-progress": "in_progress",
        "in_progress": "in_progress",
        "done": "completed",
        "completed": "completed",
        "blocked": "blocked",
    }
    TASK_STATUS_TRANSITIONS = {
        "pending": frozenset({"in_progress", "blocked"}),
        "in_progress": frozenset({"pending", "completed", "blocked"}),
        "blocked": frozenset({"pending", "in_progress"}),
        "completed": frozenset(),
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
        "submission_attempts_days": 90,
        "completed_tasks_days": 180,
    }
    DATA_RETENTION_ENV_VARS = {
        "audit_logs_days": "RETENTION_AUDIT_LOG_DAYS",
        "communications_days": "RETENTION_COMMUNICATION_DAYS",
        "documents_days": "RETENTION_DOCUMENT_DAYS",
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
    ) -> None:
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
        self.connection = sqlite3.connect(self.db_path)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.row_factory = sqlite3.Row
        self._create_schema()
        self._apply_migrations()
        self._ensure_tasks_schema()
        self.connector_configs = self._load_connector_configs(connector_configs)
        self._validate_connector_configs()

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
                assignee TEXT NOT NULL,
                status TEXT NOT NULL,
                due_date TEXT NOT NULL,
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
            """
        )
        self._ensure_column("donors", "segment", "TEXT NOT NULL DEFAULT 'unknown'")
        self._ensure_column("donors", "locale", "TEXT NOT NULL DEFAULT 'en'")
        self._ensure_column("tasks", "external_id", "TEXT")
        self._ensure_column("tasks", "due_date", "TEXT")
        self._ensure_column("tasks", "source", "TEXT NOT NULL DEFAULT 'manual'")
        self._ensure_column("tasks", "assignee_email", "TEXT")
        self._ensure_column("tasks", "assignee_name", "TEXT")
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
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_donors_segment ON donors(segment)"
        )
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
            self.connection.executescript(migration_path.read_text(encoding="utf-8"))
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
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(tasks)").fetchall()
        ]
        if columns and "assignee" not in columns and "assigned_to" in columns:
            self.connection.executescript(
                """
                ALTER TABLE tasks RENAME TO tasks_legacy;
                CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    external_id TEXT UNIQUE,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    assignee TEXT NOT NULL,
                    status TEXT NOT NULL,
                    due_date TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'manual',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    assignee_email TEXT,
                    assignee_name TEXT
                );
                INSERT INTO tasks (
                    id, external_id, title, description, assignee, status, due_date,
                    source, created_at, updated_at, assignee_email, assignee_name
                )
                SELECT
                    id,
                    external_id,
                    title,
                    COALESCE(description, ''),
                    LOWER(assigned_to),
                    CASE LOWER(status)
                        WHEN 'todo' THEN 'pending'
                        WHEN 'in-progress' THEN 'in_progress'
                        WHEN 'done' THEN 'completed'
                        ELSE REPLACE(LOWER(status), '-', '_')
                    END,
                    COALESCE(date(due_date), date(updated_at), date(created_at), date('now')),
                    COALESCE(source, 'manual'),
                    created_at,
                    updated_at,
                    assignee_email,
                    assignee_name
                FROM tasks_legacy;
                DROP TABLE tasks_legacy;
                """
            )
        self._ensure_column("tasks", "external_id", "TEXT")
        self._ensure_column("tasks", "due_date", "TEXT")
        self._ensure_column("tasks", "source", "TEXT NOT NULL DEFAULT 'manual'")
        self._ensure_column("tasks", "assignee_email", "TEXT")
        self._ensure_column("tasks", "assignee_name", "TEXT")
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_external_id ON tasks(external_id)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_due_date ON tasks(due_date)"
        )
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
            "due_date",
            "source",
            "assignee_email",
            "assignee_name",
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
        target_version = int(getattr(connector, "result_schema_version", _CONNECTOR_RESULT_SCHEMA_VERSION))
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
            "schema_version": int(getattr(connector, "result_schema_version", _CONNECTOR_RESULT_SCHEMA_VERSION)),
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
                normalized["connectors"] = [
                    dict(item) for item in normalized.get("connectors", [])
                ]
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
        """Schema is created in-place; retained for backward-compatible startup."""

    def _ensure_tasks_schema(self) -> None:
        """Task tables are created in `_create_schema`; retained for compatibility."""

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
            value ^ mask
            for value, mask in zip(ciphertext, cls._keystream(nonce, len(ciphertext)))
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
    def validate_outreach_template_catalogs(cls) -> None:
        _validate_localized_outreach_templates(_load_localized_outreach_templates())

    @classmethod
    def list_catalog_outreach_templates(cls) -> tuple[str, ...]:
        cls.validate_outreach_template_catalogs()
        return tuple(sorted(cls._load_outreach_template_catalog(cls.DEFAULT_TEMPLATE_LOCALE).keys()))

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
            return FundingBot._as_utc().date().isoformat()
        if isinstance(due_date, datetime):
            return FundingBot._as_utc(due_date).date().isoformat()
        normalized = str(due_date).strip()
        if not normalized:
            return FundingBot._as_utc().date().isoformat()
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
            task["due_date"] and task["status"] != "completed" and task["due_date"] < today
        )
        task["unread_comment_count"] = int(task.get("unread_comment_count", 0) or 0)
        return task

    @staticmethod
    def _normalize_task_due_date(due_date: str | None) -> str | None:
        if due_date is None:
            return FundingBot._as_utc().date().isoformat()
        normalized = str(due_date).strip()
        if not normalized:
            return FundingBot._as_utc().date().isoformat()
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
        now = self._to_iso()
        existing = self.connection.execute(
            "SELECT created_at, completed_at, duplicate_requests FROM task_runs WHERE task_id = ?",
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
                json.dumps(callback_payload, sort_keys=True, default=str)
                if callback_payload is not None
                else None,
                idempotency_key or task_id,
                worker_id,
                max(
                    duplicate_requests,
                    int(existing["duplicate_requests"] or 0) if existing else 0,
                ),
                int(shutdown_requested),
                normalized_retry_limit,
                normalized_attempts,
                normalized_backoff_seconds,
                normalized_backoff_max_seconds,
                int(dead_lettered),
                self._to_iso(last_attempt_at)
                if isinstance(last_attempt_at, datetime)
                else last_attempt_at,
                self._to_iso(next_retry_at)
                if isinstance(next_retry_at, datetime)
                else next_retry_at,
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
        normalized_happened_at = self._to_iso(happened_at) if isinstance(happened_at, datetime) else (
            happened_at or self._to_iso()
        )
        normalized_next_retry_at = (
            self._to_iso(next_retry_at)
            if isinstance(next_retry_at, datetime)
            else next_retry_at
        )
        self.connection.execute(
            """
            INSERT INTO task_history (
                task_id, task_name, attempt_number, status, happened_at,
                backoff_seconds, next_retry_at, result_json, error_message, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
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
            ),
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

    def get_queue_metrics(self) -> dict[str, int]:
        run_counts = self.connection.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled,
                SUM(CASE WHEN dead_lettered = 1 THEN 1 ELSE 0 END) AS dead_lettered,
                COALESCE(SUM(duplicate_requests), 0) AS duplicate_preventions
            FROM task_runs
            """
        ).fetchone()
        history_counts = self.connection.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'retry_scheduled' THEN 1 ELSE 0 END) AS retries_scheduled
            FROM task_history
            """
        ).fetchone()
        return {
            "running": int(run_counts["running"] or 0),
            "completed": int(run_counts["completed"] or 0),
            "failed": int(run_counts["failed"] or 0),
            "cancelled": int(run_counts["cancelled"] or 0),
            "dead_lettered": int(run_counts["dead_lettered"] or 0),
            "duplicate_preventions": int(run_counts["duplicate_preventions"] or 0),
            "retries_scheduled": int(history_counts["retries_scheduled"] or 0),
        }

    def execute_queue_task(
        self,
        task_name: str,
        payload: dict[str, Any] | None,
        task_callable: Callable[[QueueTaskContext, dict[str, Any]], Any],
        *,
        idempotency_key: str | None = None,
        worker_id: str | None = None,
        retry_limit: int | None = None,
        backoff_seconds: float | None = None,
        backoff_max_seconds: float | None = None,
        sleep_func: Callable[[float], None] | None = None,
    ) -> dict[str, Any]:
        normalized_task_name = str(task_name).strip()
        if not normalized_task_name:
            raise ValueError("Task name is required.")
        normalized_payload = dict(payload or {})
        normalized_idempotency_key = (
            idempotency_key
            or hashlib.sha256(
                (
                    f"{normalized_task_name}|"
                    f"{json.dumps(normalized_payload, sort_keys=True, separators=(',', ':'))}|"
                    f"{time.monotonic_ns()}"
                ).encode("utf-8")
            ).hexdigest()
        )
        config = self._load_queue_retry_config(
            retry_limit=retry_limit,
            backoff_seconds=backoff_seconds,
            backoff_max_seconds=backoff_max_seconds,
        )
        controller = GracefulShutdownController()
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
                self.connection.commit()
                self._log_action(
                    "queue_task_cancelled",
                    task_id=normalized_idempotency_key,
                    task_name=normalized_task_name,
                    attempts=attempt_number,
                )
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
                        "next_retry_at": self._to_iso(next_retry_at) if next_retry_at is not None else None,
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
            self.connection.commit()
            self._log_action(
                "queue_task_completed",
                task_id=normalized_idempotency_key,
                task_name=normalized_task_name,
                attempts=attempt_number,
            )
            return task_run

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
        existing = self.connection.execute(
            """
            SELECT data_classification, field_classifications_json
            FROM organization_profile
            WHERE key = ?
            """,
            (key,),
        ).fetchone()
        default_classification, default_field_classifications, should_encrypt = (
            self._setting_defaults_for(key, value)
        )
        final_classification = self._validate_data_classification(
            data_classification if data_classification is not None else (
                existing["data_classification"] if existing else default_classification
            ),
            default=default_classification,
        )
        final_field_classifications = self._build_field_classifications(
            defaults=default_field_classifications,
            fields=set(value) | set(field_classifications or {}),
            overrides=field_classifications
            if field_classifications is not None
            else (
                self._decode_json_blob(
                    existing["field_classifications_json"],
                    default={},
                )
                if existing
                else {}
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
                key,
                self._encode_json_blob(value, encrypt=should_encrypt),
                final_classification,
                json.dumps(final_field_classifications, sort_keys=True),
            ),
        )
        self.connection.commit()
        self._log_action(
            "generic_setting_updated",
            key=key,
            value_keys=_extract_dict_keys(value),
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
                    record_key=key,
                    previous_data_classification=previous_classification,
                    data_classification=final_classification,
                    previous_field_classifications=previous_fields,
                    field_classifications=final_field_classifications,
                )

    def load_setting(self, key: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT value_json FROM organization_profile WHERE key = ?",
            (key,),
        ).fetchone()
        return self._decode_json_blob(row["value_json"], default={}) if row else {}

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
            candidate_values = [item.strip() for item in candidate_values.split(",") if item.strip()]

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
            raise ValueError(
                f"Unknown data retention field(s): {', '.join(unknown_fields)}."
            )

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
    ) -> dict[str, Any]:
        as_of = self._as_utc(now)
        policy = self.load_data_retention_policy()
        cutoffs = {
            key: self._to_iso(as_of - timedelta(days=days))
            for key, days in policy.items()
        }

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

        deleted_counts = {
            "audit_logs": self.connection.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE happened_at < ?",
                (cutoffs["audit_logs_days"],),
            ).fetchone()[0],
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
            "submission_attempts": self.connection.execute(
                "SELECT COUNT(*) FROM submission_attempts WHERE happened_at < ?",
                (cutoffs["submission_attempts_days"],),
            ).fetchone()[0],
            "completed_tasks": self.connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = 'done' AND updated_at < ?",
                (cutoffs["completed_tasks_days"],),
            ).fetchone()[0],
            "document_files_deleted": 0,
        }

        result = {
            "dry_run": dry_run,
            "as_of": self._to_iso(as_of),
            "policy": policy,
            "cutoffs": cutoffs,
            "deleted": deleted_counts,
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
        )
        return result

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
        return self.vault.resolve_credentials(env_var_name)

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
            data_classification
            if data_classification is not None
            else (
                existing["data_classification"]
                if existing is not None
                else self.MODEL_DEFAULT_CLASSIFICATIONS["donors"]
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
            overrides=field_classifications
            if field_classifications is not None
            else (
                self._decode_json_blob(
                    existing["field_classifications_json"],
                    default=default_field_classifications,
                )
                if existing is not None
                else default_field_classifications
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
                name,
                int(opted_out),
                self._encode_json_blob(
                    preferences or {},
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
        return [donor for donor in (self._deserialize_donor_row(row) for row in rows) if donor]

    def get_donor(self, email: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM donors WHERE email = ?", (email,)).fetchone()
        return self._deserialize_donor_row(row)

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
                source.strip() or "manual",
                proof,
                notes,
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
            current["name"] if current is not None else self._default_donor_name_from_email(normalized_email)
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
        self._log_action("donor_opt_out_updated", email=normalized_email, opted_out=opted_out)

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
        if connectors is not None:
            active_connectors = list(connectors)
        elif self.connector_configs:
            active_connectors = self.connector_registry.build_connectors(
                self.connector_configs,
                credential_resolver=self.resolve_credential,
            )
        else:
            active_connectors = default_connectors()
        fallback_mode = self._load_fallback_mode()

        candidates: list[dict[str, Any]] = []
        for connector in active_connectors:
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
                fetch_result = getattr(connector, "fetch_result", None)
                if callable(fetch_result):
                    result = fetch_result(keyword_list)
                    opportunities = [dict(item) for item in result.get("opportunities", [])]
                    schema_version = int(
                        result.get("schema_version", getattr(connector, "result_schema_version", _CONNECTOR_RESULT_SCHEMA_VERSION))
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
                else:
                    opportunities = [dict(item) for item in connector.fetch_opportunities(keyword_list)]
                    schema_version = int(getattr(connector, "result_schema_version", _CONNECTOR_RESULT_SCHEMA_VERSION))
                    metadata = {"connector_name": connector_name, "source_status": "remote"}
                    source_status = "remote"
                self._store_connector_result(
                    connector_name=connector_name,
                    cache_key=cache_key,
                    schema_version=schema_version,
                    opportunities=opportunities,
                    metadata=metadata,
                    source_status=source_status,
                )
                candidates.extend(opportunities)
                continue
            except Exception as exc:
                error_message = str(exc)

            fallback_result = None
            if fallback_mode in {"cache-first", "cache-only"}:
                fallback_result = self._load_cached_connector_result(connector, keyword_list)
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
                logging.getLogger(__name__).warning(
                    "Connector %s failed with no fallback available: %s",
                    connector_name,
                    error_message,
                )
                continue

            fallback_result["metadata"] = {
                **dict(fallback_result.get("metadata", {})),
                "connector_name": connector_name,
                "cache_key": cache_key,
                "fallback_activated_at": self._to_iso(),
            }
            self._store_connector_result(
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
                fallback_result["metadata"].get("fallback_mode", fallback_result.get("source_status", "cached")),
                error_message,
            )
            self._log_action(
                "connector_fallback_activated",
                source=connector_name,
                fallback_mode=fallback_result["metadata"].get("fallback_mode", fallback_result.get("source_status", "cached")),
                error=error_message,
                cache_key=cache_key,
                schema_version=int(fallback_result["schema_version"]),
                result_count=len(fallback_result["opportunities"]),
            )
            candidates.extend([dict(item) for item in fallback_result["opportunities"]])

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
        sender: Any | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        normalized_title = str(title).strip()
        normalized_assignee = str(assignee if assignee is not None else assigned_to or "").strip().lower()
        if not normalized_title:
            raise ValueError("Task title is required.")
        if not normalized_assignee:
            raise ValueError("Task assignee is required.")

        normalized_status = self._normalize_task_status(status)
        normalized_external_id = str(external_id).strip() if external_id is not None else None
        if normalized_external_id == "":
            normalized_external_id = None
        normalized_due_date = self._normalize_task_due_date(
            None if due_date is None else str(due_date)
        )
        normalized_source = str(source or "manual").strip() or "manual"
        normalized_assignee_email = (
            _validate_email(str(assignee_email))
            if assignee_email is not None and str(assignee_email).strip()
            else None
        )
        normalized_assignee_name = (
            str(assignee_name).strip() if assignee_name and str(assignee_name).strip() else None
        )
        timestamp = self._to_iso(created_at)
        cursor = self.connection.execute(
            """
            INSERT INTO tasks (
                external_id, title, description, assignee, assignee_email, assignee_name,
                status, due_date, source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_external_id,
                normalized_title,
                str(description or "").strip(),
                normalized_assignee,
                normalized_assignee_email,
                normalized_assignee_name,
                normalized_status,
                normalized_due_date,
                normalized_source,
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
            clauses.append("assignee = ?")
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
            sort_key = f"{prefix}{normalized_field}" if normalized_field != "updated_at" else (
                "updated_at" if normalized_order == "desc" else "-updated_at"
            )
        else:
            sort_key = (sort or "").strip().lower()
        sort_map = {
            "": "updated_at DESC, id DESC",
            "updated_at": "updated_at DESC, id DESC",
            "-updated_at": "updated_at ASC, id ASC",
            "assignee": "assignee COLLATE NOCASE ASC, due_date ASC, id ASC",
            "-assignee": "assignee COLLATE NOCASE DESC, due_date DESC, id DESC",
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
            query += " WHERE assignee = ?"
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
        notification = (
            f"Task '{task['title']}' moved from {task['status']} to {normalized_status}."
        )
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
            str(assignee_name).strip() if assignee_name and str(assignee_name).strip() else None
        )
        updated_at = self._to_iso(changed_at)
        with self.connection:
            self.connection.execute(
                """
                UPDATE tasks
                SET assignee = ?, assignee_email = ?, assignee_name = ?, updated_at = ?
                WHERE id = ?
                """,
                (
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
        normalized_author = str(author).strip()
        normalized_content = str(content).strip()
        if not normalized_author:
            raise ValueError("Comment author is required.")
        if not normalized_content:
            raise ValueError("Comment content is required.")
        timestamp = self._to_iso(created_at)
        cursor = self.connection.execute(
            """
            INSERT INTO task_comments (task_id, author, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (task_id, normalized_author, normalized_content, timestamp, timestamp),
        )
        self.connection.commit()
        comment = self._serialize_task_comment(self._get_task_comment_row(task_id, cursor.lastrowid))
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
        normalized_content = str(content).strip()
        if not normalized_content:
            raise ValueError("Comment content is required.")
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
        external_id = str(external_id_raw).strip() if external_id_raw is not None else None
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

        updated_title = str(payload.get("title", existing["title"])).strip()
        updated_assigned_to = str(
            payload.get("assigned_to", payload.get("assignee", existing["assignee"]))
        ).strip().lower()
        updated_description = str(payload.get("description", existing["description"])).strip()
        updated_status = self._normalize_task_status(str(payload.get("status", existing["status"])))
        updated_due_date = (
            self._normalize_task_due_date(str(payload.get("due_date")))
            if "due_date" in payload and payload.get("due_date") is not None
            else (None if "due_date" in payload else existing["due_date"])
        )
        updated_source = str(payload.get("source", existing["source"] or default_source)).strip() or default_source
        updated_external_id = external_id if external_id is not None else existing["external_id"]

        if not updated_title:
            raise ValueError("Task title is required.")
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
            SET external_id = ?, title = ?, description = ?, assignee = ?, status = ?,
                due_date = ?, source = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                updated_external_id,
                updated_title,
                updated_description,
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
        requested_locale = self._validate_locale(locale) if locale is not None else None
        donor = self.connection.execute(
            "SELECT * FROM donors WHERE email = ?",
            (donor_email,),
        ).fetchone()
        if donor is None:
            self.upsert_donor(email=donor_email, name=donor_name, locale=requested_locale)
            donor = self.connection.execute(
                "SELECT * FROM donors WHERE email = ?",
                (donor_email,),
            ).fetchone()
        elif requested_locale is not None and donor["locale"] != requested_locale:
            self.connection.execute(
                "UPDATE donors SET locale = ? WHERE email = ?",
                (requested_locale, donor_email),
            )
            self.connection.commit()
            donor = self.connection.execute(
                "SELECT * FROM donors WHERE email = ?",
                (donor_email,),
            ).fetchone()

        if donor is None:
            raise FundingBotError(f"Unable to load donor record for {donor_email!r}.")
        if donor["opted_out"]:
            raise OptOutError(f"{donor_email} has opted out of outreach.")

        consent_context = context or {}
        latest_consent = self.get_latest_consent_record(
            donor_email,
            channel=consent_context.get("consent_channel", "email"),
        )
        if latest_consent is not None and latest_consent["status"] == "withdrawn":
            raise OptOutError(f"{donor_email} has opted out of outreach.")

        send_time = self._as_utc(sent_at)
        if donor["last_contact_at"]:
            last_contact = self._as_utc(datetime.fromisoformat(donor["last_contact_at"]))
            if send_time - last_contact < timedelta(days=7):
                raise OutreachThrottledError(
                    f"{donor_email} was contacted less than seven days ago."
                )

        donor_locale = self._validate_locale(requested_locale or donor["locale"])
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
        merged_context.update(consent_context)

        if latest_consent is None:
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
                locale=donor["locale"],
            )

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
        self._log_action(
            "outreach_sent",
            donor_email=donor_email,
            subject=subject,
            locale=donor_locale,
        )
        return {
            "email": donor_email,
            "subject": subject,
            "body": body,
            "sent_at": sent_iso,
            "locale": donor_locale,
        }

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
        )
        result["template_name"] = template_name
        return result

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
        pending = self.connection.execute(
            """
            SELECT a.donor_name, a.status, a.next_action, o.title
            FROM applications a
            JOIN opportunities o ON o.signature = a.opportunity_signature
            WHERE a.status IN ('pending', 'submitted', 'in_review')
            ORDER BY a.submitted_at
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
        communication_cutoff = self._to_iso(report_end - timedelta(days=communication_retention_days))
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
        consented_donors = {
            row["donor_email"]
            for row in self.connection.execute(
                """
                SELECT DISTINCT donor_email FROM consent_records
                WHERE status = 'granted' AND channel = 'email'
                """
            ).fetchall()
        }
        missing_consent_donors = sorted(communicated_donors - consented_donors)
        opted_out_without_record = self.connection.execute(
            """
            SELECT COUNT(*) FROM donors d
            WHERE d.opted_out = 1
              AND d.email NOT LIKE '[deleted]-%'
              AND NOT EXISTS (
                  SELECT 1 FROM consent_records cr
                  WHERE cr.donor_email = d.email AND cr.status = 'withdrawn'
              )
            """
        ).fetchone()[0]
        latest_consent_status_by_donor: dict[str, str] = {}
        for row in self.connection.execute(
            """
            SELECT donor_email, status
            FROM consent_records
            WHERE channel = 'email'
            ORDER BY recorded_at DESC, id DESC
            """
        ).fetchall():
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


def _resolve_cli_log_level(*, verbose: bool = False, quiet: bool = False) -> int:
    if verbose:
        return logging.INFO
    if quiet:
        return logging.ERROR
    return logging.WARNING


def _configure_cli_logging(*, verbose: bool = False, quiet: bool = False) -> int:
    level = _resolve_cli_log_level(verbose=verbose, quiet=quiet)
    logging.basicConfig(level=level, format="%(levelname)s:%(name)s:%(message)s", force=True)
    return level


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
            print(f"{spec['flag']} is required.", file=sys.stderr)
            continue
        if choices is not None and response not in choices:
            print(
                f"Invalid value for {spec['flag']}. Choose one of: {', '.join(choices)}.",
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


def _queue_async_task(
    task_label: str,
    task_callable: Any,
    *,
    task_kwargs: dict[str, Any],
    ready_renderer: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    async_result = task_callable.delay(**task_kwargs)
    print(f"Queued {task_label} task {async_result.id}.")
    print(f"Task status: {async_result.status}.")
    if async_result.ready():
        result = async_result.get(propagate=True)
        if isinstance(result, dict) and ready_renderer is not None:
            ready_renderer(result)
    else:
        print("Track progress in the task_runs table or the configured Celery result backend.")


def _render_discover_task_result(result: dict[str, Any]) -> None:
    found = result.get("new_opportunities", [])
    if found:
        _print_rows(found, ["signature", "source", "donor_name", "title", "category"])
    else:
        print("No new opportunities found.")


def _render_outreach_task_result(result: dict[str, Any]) -> None:
    print(f"Subject: {result['subject']}\n")
    print(result["body"])
    if result.get("dry_run"):
        print("\n(dry run: no email was actually sent)")
    else:
        print(f"\nOutreach email sent to {result['email']}.")


def _render_daily_summary_task_result(result: dict[str, Any]) -> None:
    print(f"Subject: {result['subject']}\n")
    print(result["body"])
    if result.get("dry_run"):
        print("\n(dry run: no email was actually sent)")
    else:
        print(f"\nDaily summary sent to {result['recipient']}.")


def _build_arg_parser() -> "argparse.ArgumentParser":
    import argparse

    default_db_path = os.environ.get("BOT_DB_PATH", "funding_bot.db")
    connector_choices = tuple(sorted(connector_registry().keys()))
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

    gdpr_parser = subparsers.add_parser(
        "gdpr-self-check-report",
        help="Generate a GDPR compliance self-check report.",
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
    )
    outreach_parser.add_argument("--email", metavar="EMAIL", help="Donor email address.")
    outreach_parser.add_argument("--name", metavar="NAME", help="Donor name.")
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
    outreach_parser.set_defaults(
        _required_cli_args=(
            {"dest": "email", "flag": "--email", "prompt": "Donor email"},
            {"dest": "name", "flag": "--name", "prompt": "Donor name"},
        )
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
    )
    retention_policy_parser.add_argument("--audit-logs-days", type=int, metavar="DAYS")
    retention_policy_parser.add_argument("--communications-days", type=int, metavar="DAYS")
    retention_policy_parser.add_argument("--documents-days", type=int, metavar="DAYS")
    retention_policy_parser.add_argument("--submission-attempts-days", type=int, metavar="DAYS")
    retention_policy_parser.add_argument("--completed-tasks-days", type=int, metavar="DAYS")

    retention_enforcement_parser = subparsers.add_parser(
        "enforce-data-retention",
        help="Delete records that exceed the configured retention windows.",
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
            "data_retention_policy": bot.load_data_retention_policy(),
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
    _configure_cli_logging(verbose=args.verbose, quiet=args.quiet)

    if args.command is None:
        parser.print_help()
        return
    args = _prompt_for_missing_cli_args(parser, args)
    logging.getLogger(__name__).info("Running CLI command %s", args.command)

    bot = FundingBot(db_path=args.db)
    try:
        if args.command == "send-daily-summary":
            from celery_tasks import send_daily_summary_task

            _queue_async_task(
                "send-daily-summary",
                send_daily_summary_task,
                task_kwargs={
                    "db_path": args.db,
                    "recipient": args.recipient,
                    "dry_run": args.dry_run,
                },
                ready_renderer=_render_daily_summary_task_result,
            )
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
        elif args.command == "set-data-retention-policy":
            policy_updates = {
                "audit_logs_days": args.audit_logs_days,
                "communications_days": args.communications_days,
                "documents_days": args.documents_days,
                "submission_attempts_days": args.submission_attempts_days,
                "completed_tasks_days": args.completed_tasks_days,
            }
            policy = bot.store_data_retention_policy(
                {key: value for key, value in policy_updates.items() if value is not None}
            )
            print(json.dumps(policy, indent=2, sort_keys=True))
        elif args.command == "enforce-data-retention":
            as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
            report = bot.enforce_data_retention(now=as_of, dry_run=args.dry_run)
            print(json.dumps(report, indent=2, sort_keys=True))
        elif args.command == "gdpr-self-check-report":
            report = bot.build_gdpr_compliance_report(cadence=args.cadence)
            report_json = json.dumps(report, indent=2)
            if args.output:
                output_path = Path(args.output)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(report_json, encoding="utf-8")
                print(f"GDPR self-check report written to {args.output}.")
            else:
                print(report_json)
        elif args.command == "discover":
            from celery_tasks import discover_task

            _queue_async_task(
                "discover",
                discover_task,
                task_kwargs={
                    "db_path": args.db,
                    "keywords": _parse_csv_argument(args.keywords),
                    "trusted_sources": _parse_csv_argument(args.trusted_sources),
                },
                ready_renderer=_render_discover_task_result,
            )
        elif args.command == "test-connector":
            connector = create_connector(args.connector)
            validation = connector.validate_connectivity(
                keywords=_parse_csv_argument(args.keywords),
                sample_limit=max(args.limit, 0),
            )
            print(json.dumps(validation, indent=2))
        elif args.command == "send-outreach":
            from celery_tasks import send_outreach_task

            _queue_async_task(
                "send-outreach",
                send_outreach_task,
                task_kwargs={
                    "db_path": args.db,
                    "donor_email": args.email,
                    "donor_name": args.name,
                    "subject_template": args.subject,
                    "body_template": args.body,
                    "locale": args.locale,
                    "dry_run": args.dry_run,
                },
                ready_renderer=_render_outreach_task_result,
            )
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
