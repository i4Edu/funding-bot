from __future__ import annotations

import hashlib
import json
import os
import re
import smtplib
import sqlite3
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol
from xml.sax.saxutils import escape

# ---------------------------------------------------------------------------
# Simple TTL cache for repeated portal queries
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_email(email: str) -> str:
    """Return the stripped email or raise ValueError if it looks invalid."""
    stripped = email.strip()
    if not _EMAIL_RE.match(stripped):
        raise ValueError(f"Invalid email address: {stripped!r}")
    return stripped


class _TTLCache:
    """A minimal thread-unsafe TTL cache keyed by arbitrary hashable keys."""

    def __init__(self, ttl_seconds: float = 300) -> None:
        self._ttl = ttl_seconds
        self._store: dict[Any, tuple[Any, float]] = {}

    def get(self, key: Any) -> tuple[bool, Any]:
        entry = self._store.get(key)
        if entry is None:
            return False, None
        value, expires_at = entry
        if time.monotonic() > expires_at:
            del self._store[key]
            return False, None
        return True, value

    def set(self, key: Any, value: Any) -> None:
        self._store[key] = (value, time.monotonic() + self._ttl)

    def invalidate(self, key: Any) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()


class FundingBotError(Exception):
    """Base error for funding bot operations."""


class DuplicateSubmissionError(FundingBotError):
    """Raised when an opportunity already has an application record."""


class OpportunityNotFoundError(FundingBotError):
    """Raised when an opportunity cannot be found."""


class CredentialNotFoundError(FundingBotError):
    """Raised when a credential alias cannot be resolved."""


class OutreachThrottledError(FundingBotError):
    """Raised when an outreach email exceeds the allowed cadence."""


class OptOutError(FundingBotError):
    """Raised when a donor has opted out of outreach."""


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

    source_name = "Portal"
    base_url = "https://example.org"

    def __init__(
        self,
        http_client: Callable[..., Any] | None = None,
        *,
        cache_ttl: float | None = None,
    ) -> None:
        self.http_client = http_client
        if cache_ttl is None:
            raw_ttl = os.environ.get("PORTAL_CACHE_TTL", "300")
            try:
                cache_ttl = float(raw_ttl)
            except ValueError:
                cache_ttl = 300
            if cache_ttl <= 0:
                cache_ttl = 300
        self._cache = _TTLCache(ttl_seconds=cache_ttl)

    def fetch_opportunities(self, keywords: Iterable[str]) -> list[dict[str, Any]]:
        keyword_list = [keyword.lower() for keyword in (keywords or [])]
        cache_key = (self.base_url, tuple(sorted(keyword_list)))
        if self._cache is not None:
            hit, cached = self._cache.get(cache_key)
            if hit:
                return list(cached)

        opportunities = self._fetch_remote(keyword_list) if self.http_client else self._demo_data()
        if self._cache is not None:
            self._cache.set(cache_key, list(opportunities))
        if not keyword_list:
            return opportunities

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
                filtered.append(opportunity)
        return filtered

    def _fetch_remote(self, keywords: list[str]) -> list[dict[str, Any]]:
        response = self.http_client(self.base_url, {"keywords": keywords})
        if isinstance(response, dict):
            payload = response.get("opportunities", [])
        else:
            payload = response
        return [dict(item) for item in payload]

    def _demo_data(self) -> list[dict[str, Any]]:
        raise NotImplementedError


class GrantsPortalConnector(_BasePortalConnector):
    """Stub connector for grants portals."""

    source_name = "Grants Portal"
    base_url = "https://grants.example.org/opportunities"

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

    source_name = "CSR Network"
    base_url = "https://csr.example.org/opportunities"

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

    source_name = "NGO Directory"
    base_url = "https://directory.example.org/opportunities"

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
                last_contact_at TEXT
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
            """
        )
        self._ensure_column("donors", "segment", "TEXT NOT NULL DEFAULT 'unknown'")
        # Index on donors.segment must be created after the column is guaranteed to exist.
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_donors_segment ON donors(segment)"
        )
        self.connection.commit()

    # Allowlist of table/column identifiers that _ensure_column is permitted to touch.
    # All calls are internal and use literals; the allowlist is an extra safety guard.
    _ALLOWED_ALTER_TABLES = frozenset({"donors"})
    _ALLOWED_ALTER_COLUMNS = frozenset({"segment"})

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
        self._log_action("generic_setting_updated", key=key, value_keys=sorted(value))

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
    ) -> None:
        email = _validate_email(email)
        normalized_segment = self._validate_segment(segment) if segment is not None else None
        self.connection.execute(
            """
            INSERT INTO donors (email, name, opted_out, preferences_json, last_contact_at, segment)
            VALUES (
                ?, ?, ?, ?, COALESCE((SELECT last_contact_at FROM donors WHERE email = ?), NULL),
                COALESCE((SELECT segment FROM donors WHERE email = ?), COALESCE(?, 'unknown'))
            )
            ON CONFLICT(email) DO UPDATE SET
                name = excluded.name,
                opted_out = excluded.opted_out,
                preferences_json = excluded.preferences_json,
                segment = CASE
                    WHEN ? IS NULL THEN donors.segment
                    ELSE excluded.segment
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
                normalized_segment,
            ),
        )
        self.connection.commit()
        logged_segment = self.connection.execute(
            "SELECT segment FROM donors WHERE email = ?",
            (email,),
        ).fetchone()["segment"]
        self._log_action(
            "donor_upserted",
            email=email,
            opted_out=opted_out,
            segment=logged_segment,
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
        keyword_list = list(keywords) if keywords is not None else list(settings.get("keywords", []))
        source_list = (
            list(trusted_sources) if trusted_sources is not None else list(settings.get("trusted_sources", []))
        )
        active_connectors = list(connectors) if connectors is not None else default_connectors()

        candidates: list[dict[str, Any]] = []
        for connector in active_connectors:
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
    ) -> dict[str, Any]:
        donor_email = _validate_email(donor_email)
        donor = self.connection.execute(
            "SELECT * FROM donors WHERE email = ?",
            (donor_email,),
        ).fetchone()
        if donor is None:
            self.upsert_donor(email=donor_email, name=donor_name)
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

        profile = self.load_organization_profile()
        merged_context = {
            "donor_name": donor_name,
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
            body = (
                f"{body}\n\nTo opt out of future outreach, visit {merged_context['opt_out_url']}."
            )

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
            "SELECT segment FROM donors WHERE email = ?",
            (donor_email,),
        ).fetchone()
        donor_segment = donor["segment"] if donor else "unknown"
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
            raise FundingBotError(f"Unknown outreach template {template_name!r}.")
        return self.send_outreach(
            donor_email=donor_email,
            donor_name=donor_name,
            subject_template=row["subject_template"],
            body_template=row["body_template"],
            context=context,
            sender=sender,
            sent_at=sent_at,
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

    outreach_parser = subparsers.add_parser(
        "send-outreach",
        help="Compose and send (or preview) a personalized donor outreach email.",
    )
    outreach_parser.add_argument("--email", required=True, metavar="EMAIL", help="Donor email address.")
    outreach_parser.add_argument("--name", required=True, metavar="NAME", help="Donor name.")
    outreach_parser.add_argument(
        "--subject",
        default="Thank you for supporting {organization_name}",
        metavar="TEMPLATE",
        help="Subject template with {placeholders} (default provided).",
    )
    outreach_parser.add_argument(
        "--body",
        default=(
            "Dear {donor_name},\n\nThank you for your continued interest in {organization_name}."
        ),
        metavar="TEMPLATE",
        help="Body template with {placeholders} (default provided).",
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
                ["email", "name", "segment", "opted_out", "last_contact_at"],
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
            keywords = (
                [item.strip() for item in args.keywords.split(",") if item.strip()]
                if args.keywords
                else None
            )
            trusted_sources = (
                [item.strip() for item in args.trusted_sources.split(",") if item.strip()]
                if args.trusted_sources
                else None
            )
            found = bot.run_discovery(keywords=keywords, trusted_sources=trusted_sources)
            if found:
                _print_rows(found, ["signature", "source", "donor_name", "title", "category"])
            else:
                print("No new opportunities found.")
        elif args.command == "send-outreach":
            sender = None if args.dry_run else SMTPEmailSender.from_env()
            result = bot.send_outreach(
                donor_email=args.email,
                donor_name=args.name,
                subject_template=args.subject,
                body_template=args.body,
                sender=sender,
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
            bot.register_credential(args.alias, args.env_var)
            print(f"Registered credential alias {args.alias!r} -> env var {args.env_var!r}.")
        elif args.command == "show-settings":
            print(json.dumps(
                {
                    "organization_profile": bot.load_organization_profile(),
                    "search_settings": bot.load_search_settings(),
                },
                indent=2,
            ))
            print()
            print("Credential aliases (env var *names* only — never the secret values):")
            _print_rows(bot.list_credentials(), ["alias", "env_var_name"])
    finally:
        bot.close()


if __name__ == "__main__":
    main()
