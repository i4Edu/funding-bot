from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
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


@dataclass(frozen=True)
class DatabaseQueryMonitorConfig:
    slow_query_threshold_seconds: float = 0.25

    @classmethod
    def from_env(cls) -> "DatabaseQueryMonitorConfig":
        return cls(
            slow_query_threshold_seconds=_read_float_env(
                "FUNDING_BOT_DB_SLOW_QUERY_THRESHOLD_SECONDS",
                0.25,
                minimum=0.0,
            )
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


_QUERY_BUCKETS_SECONDS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_QUERY_PREFIX_RE = re.compile(r"^(?:--[^\n]*\n|\s+|/\*.*?\*/)+", re.DOTALL)


def _statement_label(query: str) -> str:
    normalized = _QUERY_PREFIX_RE.sub("", query or "").lstrip()
    if not normalized:
        return "unknown"
    token = normalized.split(None, 1)[0].strip().strip("();").lower()
    return token or "unknown"


def _query_status(error: BaseException | None) -> str:
    if error is None:
        return "success"
    message = str(error).lower()
    if "timeout" in message or "timed out" in message or "database is locked" in message:
        return "timeout"
    return "error"


def _empty_query_metric() -> dict[str, Any]:
    return {
        "count": 0,
        "success": 0,
        "error": 0,
        "timeout": 0,
        "slow": 0,
        "sum_duration_seconds": 0.0,
        "max_duration_seconds": 0.0,
        "bucket_counts": [0 for _ in _QUERY_BUCKETS_SECONDS],
        "in_flight": 0,
    }


def _snapshot_metric(metric: dict[str, Any]) -> dict[str, Any]:
    count = int(metric["count"])
    average = float(metric["sum_duration_seconds"]) / count if count else 0.0
    snapshot = {
        "count": count,
        "success": int(metric["success"]),
        "error": int(metric["error"]),
        "timeout": int(metric["timeout"]),
        "slow": int(metric["slow"]),
        "sum_duration_seconds": float(metric["sum_duration_seconds"]),
        "average_duration_seconds": average,
        "max_duration_seconds": float(metric["max_duration_seconds"]),
        "bucket_counts": list(metric["bucket_counts"]),
        "in_flight": int(metric["in_flight"]),
    }
    return snapshot


class DatabaseQueryMonitor:
    def __init__(self, config: DatabaseQueryMonitorConfig | None = None) -> None:
        self.config = config or DatabaseQueryMonitorConfig.from_env()
        self._lock = threading.Lock()
        self._summary = _empty_query_metric()
        self._statements: dict[str, dict[str, Any]] = {}

    def _metric_bucket(self, statement: str) -> dict[str, Any]:
        bucket = self._statements.get(statement)
        if bucket is None:
            bucket = _empty_query_metric()
            self._statements[statement] = bucket
        return bucket

    def begin(self, query: str) -> str:
        statement = _statement_label(query)
        with self._lock:
            self._summary["in_flight"] += 1
            self._metric_bucket(statement)["in_flight"] += 1
        return statement

    def finish(self, statement: str, duration_seconds: float, error: BaseException | None = None) -> None:
        status = _query_status(error)
        with self._lock:
            for metric in (self._summary, self._metric_bucket(statement)):
                metric["in_flight"] = max(int(metric["in_flight"]) - 1, 0)
                metric["count"] += 1
                metric[status] += 1
                metric["sum_duration_seconds"] += duration_seconds
                metric["max_duration_seconds"] = max(
                    float(metric["max_duration_seconds"]), duration_seconds
                )
                if duration_seconds >= self.config.slow_query_threshold_seconds:
                    metric["slow"] += 1
                for index, bucket_limit in enumerate(_QUERY_BUCKETS_SECONDS):
                    if duration_seconds <= bucket_limit:
                        metric["bucket_counts"][index] += 1
                        break

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            statements = {
                statement: _snapshot_metric(metric)
                for statement, metric in sorted(self._statements.items())
            }
            summary = _snapshot_metric(self._summary)
        return {
            "slow_query_threshold_seconds": self.config.slow_query_threshold_seconds,
            "buckets": list(_QUERY_BUCKETS_SECONDS),
            "summary": summary,
            "statements": statements,
        }


class InstrumentedCursor:
    def __init__(self, cursor: Any, monitor: DatabaseQueryMonitor) -> None:
        self._cursor = cursor
        self._monitor = monitor

    def _record(self, query: str, operation: Any) -> Any:
        statement = self._monitor.begin(query)
        started_at = time.perf_counter()
        try:
            result = operation()
        except BaseException as exc:
            self._monitor.finish(statement, time.perf_counter() - started_at, exc)
            raise
        self._monitor.finish(statement, time.perf_counter() - started_at, None)
        return result

    def execute(self, query: str, parameters: Any = ()) -> Any:
        return self._record(query, lambda: self._cursor.execute(query, parameters))

    def executemany(self, query: str, seq_of_parameters: Any) -> Any:
        return self._record(query, lambda: self._cursor.executemany(query, seq_of_parameters))

    def executescript(self, query: str) -> Any:
        return self._record(query, lambda: self._cursor.executescript(query))

    def __iter__(self) -> Any:
        return iter(self._cursor)

    def __enter__(self) -> "InstrumentedCursor":
        self._cursor.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        return self._cursor.__exit__(exc_type, exc, tb)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class InstrumentedConnection:
    def __init__(self, connection: Any, monitor: DatabaseQueryMonitor) -> None:
        self._connection = connection
        self._monitor = monitor

    @property
    def raw_connection(self) -> Any:
        return self._connection

    def _record(self, query: str, operation: Any) -> Any:
        statement = self._monitor.begin(query)
        started_at = time.perf_counter()
        try:
            result = operation()
        except BaseException as exc:
            self._monitor.finish(statement, time.perf_counter() - started_at, exc)
            raise
        self._monitor.finish(statement, time.perf_counter() - started_at, None)
        return result

    def execute(self, query: str, parameters: Any = ()) -> Any:
        return self._record(query, lambda: self._connection.execute(query, parameters))

    def executemany(self, query: str, seq_of_parameters: Any) -> Any:
        return self._record(query, lambda: self._connection.executemany(query, seq_of_parameters))

    def executescript(self, query: str) -> Any:
        return self._record(query, lambda: self._connection.executescript(query))

    def cursor(self, *args: Any, **kwargs: Any) -> InstrumentedCursor:
        return InstrumentedCursor(self._connection.cursor(*args, **kwargs), self._monitor)

    def __enter__(self) -> "InstrumentedConnection":
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        return self._connection.__exit__(exc_type, exc, tb)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


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


class _PersistentConnectionProxy:
    def __init__(self, raw_connection: Any) -> None:
        self._raw_connection = raw_connection

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw_connection, name)

    def __enter__(self) -> "_PersistentConnectionProxy":
        return self

    def __exit__(self, exc_type: Any, exc: Any, _tb: Any) -> bool:
        if exc_type is None:
            self._raw_connection.commit()
        else:
            self._raw_connection.rollback()
        return False

    def close(self) -> None:
        self._raw_connection.close()


class DatabaseManager:
    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        config: DatabasePoolConfig | None = None,
        query_monitor_config: DatabaseQueryMonitorConfig | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self.config = config or DatabasePoolConfig.from_env()
        self.monitor = DatabasePoolMonitor()
        self.query_monitor = DatabaseQueryMonitor(query_monitor_config)
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
            self.connection = InstrumentedConnection(connection, self.query_monitor)

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
        raw_connection = self.engine.raw_connection()
        self.connection = _PersistentConnectionProxy(raw_connection)
        sqlite_connection = self.driver_connection
        sqlite_connection.execute("PRAGMA foreign_keys = ON")
        sqlite_connection.execute("PRAGMA busy_timeout = 5000")
        sqlite_connection.row_factory = sqlite3.Row
        self.connection = InstrumentedConnection(self.connection, self.query_monitor)
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
        raw_connection = getattr(self.connection, "raw_connection", None)
        if raw_connection is not None:
            connection = raw_connection
        else:
            connection = self.connection
        for attribute in ("driver_connection", "connection"):
            candidate = getattr(connection, attribute, None)
            if isinstance(candidate, sqlite3.Connection):
                return candidate
        if isinstance(connection, sqlite3.Connection):
            return connection
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

    def get_query_metrics(self) -> dict[str, Any]:
        snapshot = self.query_monitor.snapshot()
        return {
            "backend": self._backend,
            "db_path": self.db_path,
            **snapshot,
        }

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.engine is not None:
            self.engine.dispose()
            self.engine = None
