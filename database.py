from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # pragma: no cover - exercised indirectly in environments with SQLAlchemy installed
    from sqlalchemy import create_engine, event
    from sqlalchemy.pool import QueuePool, StaticPool
except ImportError:  # pragma: no cover - fallback for minimal environments
    create_engine = None
    event = None
    QueuePool = None
    StaticPool = None


@dataclass(frozen=True)
class DatabasePoolConfig:
    pool_size: int = 5
    max_overflow: int = 10
    pool_timeout_seconds: float = 30.0
    pool_recycle_seconds: float = 1800.0
    pre_ping: bool = True

    @classmethod
    def from_env(cls) -> "DatabasePoolConfig":
        return cls(
            pool_size=_read_int_env("FUNDING_BOT_DB_POOL_SIZE", 5, minimum=1),
            max_overflow=_read_int_env("FUNDING_BOT_DB_MAX_OVERFLOW", 10, minimum=0),
            pool_timeout_seconds=_read_float_env("FUNDING_BOT_DB_POOL_TIMEOUT_SECONDS", 30.0, minimum=1.0),
            pool_recycle_seconds=_read_float_env("FUNDING_BOT_DB_POOL_RECYCLE_SECONDS", 1800.0, minimum=0.0),
            pre_ping=_read_bool_env("FUNDING_BOT_DB_POOL_PRE_PING", True),
        )


def _read_int_env(name: str, default: int, *, minimum: int | None = None) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        parsed = int(raw_value)
    except ValueError:
        return default
    if minimum is not None and parsed < minimum:
        return default
    return parsed


def _read_float_env(name: str, default: float, *, minimum: float | None = None) -> float:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        parsed = float(raw_value)
    except ValueError:
        return default
    if minimum is not None and parsed < minimum:
        return default
    return parsed


def _read_bool_env(name: str, default: bool) -> bool:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


class DatabasePoolMonitor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._metrics = {
            "connects": 0,
            "checkouts": 0,
            "checkins": 0,
            "invalidations": 0,
            "closes": 0,
        }

    def attach(self, engine: Any) -> None:
        if event is None:
            return
        event.listen(engine, "connect", self._on_connect)
        event.listen(engine, "checkout", self._on_checkout)
        event.listen(engine, "checkin", self._on_checkin)
        event.listen(engine, "invalidate", self._on_invalidate)
        event.listen(engine, "close", self._on_close)

    def _increment(self, key: str) -> None:
        with self._lock:
            self._metrics[key] += 1

    def _on_connect(self, *_args: Any, **_kwargs: Any) -> None:
        self._increment("connects")

    def _on_checkout(self, *_args: Any, **_kwargs: Any) -> None:
        self._increment("checkouts")

    def _on_checkin(self, *_args: Any, **_kwargs: Any) -> None:
        self._increment("checkins")

    def _on_invalidate(self, *_args: Any, **_kwargs: Any) -> None:
        self._increment("invalidations")

    def _on_close(self, *_args: Any, **_kwargs: Any) -> None:
        self._increment("closes")

    def snapshot(self, pool: Any | None, *, enabled: bool, backend: str) -> dict[str, Any]:
        with self._lock:
            metrics = dict(self._metrics)
        snapshot = {
            "enabled": enabled,
            "backend": backend,
            "size": 0,
            "checked_in": 0,
            "checked_out": 0,
            "overflow": 0,
            "status": "disabled" if not enabled else "ok",
            **metrics,
        }
        if pool is None:
            return snapshot
        for name, method_name in (
            ("size", "size"),
            ("checked_in", "checkedin"),
            ("checked_out", "checkedout"),
            ("overflow", "overflow"),
        ):
            method = getattr(pool, method_name, None)
            if callable(method):
                try:
                    snapshot[name] = int(method())
                except Exception:
                    snapshot[name] = 0
        status = getattr(pool, "status", None)
        if callable(status):
            try:
                snapshot["pool_status"] = str(status())
            except Exception:
                snapshot["pool_status"] = "unavailable"
        return snapshot


class DatabaseManager:
    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        config: DatabasePoolConfig | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self.config = config or DatabasePoolConfig.from_env()
        self.monitor = DatabasePoolMonitor()
        self.engine = None
        self.connection = None
        self._enabled = create_engine is not None
        self._backend = "sqlite3"
        if self._enabled:
            self._initialize_sqlalchemy_connection()
        else:  # pragma: no cover - fallback only for environments missing SQLAlchemy
            connection = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                uri=self._uses_sqlite_uri(self.db_path),
            )
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.row_factory = sqlite3.Row
            self.connection = connection

    def _initialize_sqlalchemy_connection(self) -> None:
        assert create_engine is not None
        assert StaticPool is not None
        is_memory = self._is_in_memory_database(self.db_path)
        uses_uri = self._uses_sqlite_uri(self.db_path)
        if self.db_path == ":memory:":
            url = "sqlite:///:memory:"
        elif uses_uri:
            url = f"sqlite:///{self.db_path}"
        else:
            url = f"sqlite:///{Path(self.db_path).resolve()}"
        engine_kwargs: dict[str, Any] = {
            "connect_args": {
                "check_same_thread": False,
                **({"uri": True} if uses_uri else {}),
            },
            "future": True,
            "pool_pre_ping": self.config.pre_ping,
        }
        if is_memory:
            engine_kwargs["poolclass"] = StaticPool
        else:
            assert QueuePool is not None
            engine_kwargs.update(
                {
                    "poolclass": QueuePool,
                    "pool_size": self.config.pool_size,
                    "max_overflow": self.config.max_overflow,
                    "pool_timeout": self.config.pool_timeout_seconds,
                    "pool_recycle": self.config.pool_recycle_seconds,
                }
            )
        self.engine = create_engine(url, **engine_kwargs)
        self.monitor.attach(self.engine)
        self.connection = self.engine.raw_connection()
        sqlite_connection = self.driver_connection
        sqlite_connection.execute("PRAGMA foreign_keys = ON")
        sqlite_connection.execute("PRAGMA busy_timeout = 5000")
        sqlite_connection.row_factory = sqlite3.Row
        self._backend = "sqlalchemy"

    @staticmethod
    def _uses_sqlite_uri(db_path: str) -> bool:
        return db_path.startswith("file:")

    @classmethod
    def _is_in_memory_database(cls, db_path: str) -> bool:
        if db_path == ":memory:":
            return True
        if not cls._uses_sqlite_uri(db_path):
            return False
        return "mode=memory" in db_path

    @property
    def driver_connection(self) -> sqlite3.Connection:
        if self.connection is None:
            raise RuntimeError("Database connection is not initialized.")
        for attribute in ("driver_connection", "connection"):
            candidate = getattr(self.connection, attribute, None)
            if isinstance(candidate, sqlite3.Connection):
                return candidate
        if isinstance(self.connection, sqlite3.Connection):
            return self.connection
        raise RuntimeError("Unable to access the underlying sqlite3 connection.")

    def get_pool_metrics(self) -> dict[str, Any]:
        pool = getattr(self.engine, "pool", None)
        snapshot = self.monitor.snapshot(pool, enabled=self._enabled, backend=self._backend)
        snapshot.update(
            {
                "db_path": self.db_path,
                "pool_class": type(pool).__name__ if pool is not None else "sqlite3",
                "pool_size_configured": self.config.pool_size,
                "max_overflow_configured": self.config.max_overflow,
                "pool_timeout_seconds": self.config.pool_timeout_seconds,
                "pool_recycle_seconds": self.config.pool_recycle_seconds,
                "pre_ping": self.config.pre_ping,
            }
        )
        return snapshot

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.engine is not None:
            self.engine.dispose()
            self.engine = None
