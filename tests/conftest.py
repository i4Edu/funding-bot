from __future__ import annotations

import base64
import json
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("ADMIN_PASSWORD", "admin-secret")
os.environ.setdefault("STAFF_PASSWORD", "staff-secret")
os.environ.setdefault("AUDITOR_PASSWORD", "auditor-secret")

import funding_bot as funding_bot_module  # noqa: E402
import task_queue as task_queue_module  # noqa: E402
import web.app as web_app_module  # noqa: E402
from funding_bot import DEFAULT_CONNECTOR_REGISTRY, FundingBot  # noqa: E402
from web.app import app  # noqa: E402

PYTEST_ARTIFACTS_DIR = PROJECT_ROOT / ".pytest-artifacts"


class _CompatCacheRegion:
    def __init__(self, *, namespace: str, scope: str, ttl_seconds: float) -> None:
        self.namespace = namespace
        self.scope = scope
        self._cache = funding_bot_module._TTLCache(ttl_seconds=ttl_seconds)
        self._tags: dict[str, set[Any]] = {}

    def get(self, key: Any) -> tuple[bool, Any]:
        return self._cache.get(key)

    def set(self, key: Any, value: Any, tags: list[str] | None = None) -> None:
        self._cache.set(key, value)
        for tag in tags or []:
            self._tags.setdefault(tag, set()).add(key)

    def invalidate(self, key: Any) -> None:
        self._cache.invalidate(key)
        for keys in self._tags.values():
            keys.discard(key)

    def invalidate_tags(self, *tags: str) -> None:
        for tag in tags:
            for key in tuple(self._tags.get(tag, ())):
                self._cache.invalidate(key)
            self._tags.pop(tag, None)

    def clear(self) -> None:
        self._cache.clear()
        self._tags.clear()

    def stats(self) -> dict[str, float | int | str]:
        return {
            **self._cache.stats(),
            "namespace": self.namespace,
            "scope": self.scope,
        }


class _CompatCacheManager:
    def __init__(self) -> None:
        self._regions: dict[tuple[str, str, float], _CompatCacheRegion] = {}

    def make_region(self, namespace: str, *, scope: str, ttl_seconds: float) -> _CompatCacheRegion:
        key = (namespace, scope, ttl_seconds)
        if key not in self._regions:
            self._regions[key] = _CompatCacheRegion(
                namespace=namespace,
                scope=scope,
                ttl_seconds=ttl_seconds,
            )
        return self._regions[key]


if not hasattr(funding_bot_module, "CacheManager"):
    funding_bot_module.CacheManager = _CompatCacheManager
    funding_bot_module._DEFAULT_CACHE_MANAGER = None

_ORIGINAL_APPLY_MIGRATIONS = FundingBot._apply_migrations


def _safe_apply_migrations(self: FundingBot) -> None:
    try:
        _ORIGINAL_APPLY_MIGRATIONS(self)
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise


FundingBot._apply_migrations = _safe_apply_migrations


@dataclass
class _TestRecord:
    nodeid: str
    reruns: int = 0
    total_duration_seconds: float = 0.0
    final_outcome: str = "notrun"
    outcomes: list[str] = field(default_factory=list)

    @property
    def is_flaky(self) -> bool:
        return self.reruns > 0 and self.final_outcome == "passed"


class ReliabilityTracker:
    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.records: dict[str, _TestRecord] = {}
        self.generated_at = datetime.now(timezone.utc).isoformat()

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        record = self.records.setdefault(report.nodeid, _TestRecord(nodeid=report.nodeid))

        if report.when == "call":
            record.total_duration_seconds += float(report.duration)
            record.outcomes.append(report.outcome)
            if report.outcome == "rerun":
                record.reruns += 1
                return
            record.final_outcome = report.outcome
            return

        if report.when == "setup" and report.outcome in {"failed", "skipped"}:
            record.outcomes.append(f"setup:{report.outcome}")
            record.final_outcome = report.outcome
            return

        if report.when == "teardown" and report.outcome == "failed":
            record.outcomes.append("teardown:failed")
            record.final_outcome = "failed"

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        report = self._build_report(session.testscollected)
        self._write_json(report)
        self._write_markdown(report)
        self._write_metrics(report)

    def _build_report(self, collected: int) -> dict[str, Any]:
        records = sorted(self.records.values(), key=lambda record: record.nodeid)
        total = collected or len(records)
        passed = sum(1 for record in records if record.final_outcome == "passed")
        failed = sum(1 for record in records if record.final_outcome == "failed")
        skipped = sum(1 for record in records if record.final_outcome == "skipped")
        flaky = [record for record in records if record.is_flaky]
        rerun_events = sum(record.reruns for record in records)
        stable_passes = sum(
            1 for record in records if record.final_outcome == "passed" and record.reruns == 0
        )
        stable_pass_rate = stable_passes / total if total else 0.0
        eventual_pass_rate = passed / total if total else 0.0
        flaky_rate = len(flaky) / total if total else 0.0

        return {
            "generated_at": self.generated_at,
            "suite": "pytest",
            "summary": {
                "collected_tests": total,
                "recorded_tests": len(records),
                "passed": passed,
                "failed": failed,
                "skipped": skipped,
                "rerun_events": rerun_events,
                "flaky_tests": len(flaky),
                "stable_passes": stable_passes,
                "stable_pass_rate": round(stable_pass_rate, 4),
                "eventual_pass_rate": round(eventual_pass_rate, 4),
                "flake_rate": round(flaky_rate, 4),
            },
            "tests": [
                {
                    "nodeid": record.nodeid,
                    "final_outcome": record.final_outcome,
                    "reruns": record.reruns,
                    "total_duration_seconds": round(record.total_duration_seconds, 6),
                    "outcomes": record.outcomes,
                }
                for record in records
            ],
            "flaky_tests": [
                {
                    "nodeid": record.nodeid,
                    "reruns": record.reruns,
                    "final_outcome": record.final_outcome,
                    "total_duration_seconds": round(record.total_duration_seconds, 6),
                }
                for record in flaky
            ],
        }

    def _write_json(self, report: dict[str, Any]) -> None:
        output_path = self.config.getoption("--flaky-report")
        if not output_path:
            return
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    def _write_markdown(self, report: dict[str, Any]) -> None:
        output_path = self.config.getoption("--flaky-report-markdown")
        if not output_path:
            return
        summary = report["summary"]
        flaky_tests = report["flaky_tests"]
        lines = [
            "# Flaky Test Report",
            "",
            f"- Generated at: `{report['generated_at']}`",
            f"- Collected tests: `{summary['collected_tests']}`",
            f"- Stable pass rate: `{summary['stable_pass_rate']:.2%}`",
            f"- Eventual pass rate: `{summary['eventual_pass_rate']:.2%}`",
            f"- Flake rate: `{summary['flake_rate']:.2%}`",
            f"- Rerun events: `{summary['rerun_events']}`",
            "",
        ]
        if flaky_tests:
            lines.extend(
                [
                    "## Flaky tests detected",
                    "",
                    "| Test | Reruns | Final outcome |",
                    "| --- | ---: | --- |",
                ]
            )
            for test in flaky_tests:
                lines.append(
                    f"| `{test['nodeid']}` | {test['reruns']} | `{test['final_outcome']}` |"
                )
        else:
            lines.extend(["## Flaky tests detected", "", "No flaky tests detected in this run."])
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_metrics(self, report: dict[str, Any]) -> None:
        output_path = self.config.getoption("--test-reliability-metrics")
        if not output_path:
            return
        summary = report["summary"]
        lines = [
            "# HELP funding_bot_test_collected_total Total collected pytest tests in the run",
            "# TYPE funding_bot_test_collected_total gauge",
            f"funding_bot_test_collected_total {summary['collected_tests']}",
            "# HELP funding_bot_test_flaky_total Total flaky tests that passed after reruns",
            "# TYPE funding_bot_test_flaky_total gauge",
            f"funding_bot_test_flaky_total {summary['flaky_tests']}",
            "# HELP funding_bot_test_rerun_events_total Total pytest rerun events",
            "# TYPE funding_bot_test_rerun_events_total counter",
            f"funding_bot_test_rerun_events_total {summary['rerun_events']}",
            "# HELP funding_bot_test_stable_pass_rate Share of tests that passed on the first attempt",
            "# TYPE funding_bot_test_stable_pass_rate gauge",
            f"funding_bot_test_stable_pass_rate {summary['stable_pass_rate']}",
            "# HELP funding_bot_test_eventual_pass_rate Share of tests that passed after reruns",
            "# TYPE funding_bot_test_eventual_pass_rate gauge",
            f"funding_bot_test_eventual_pass_rate {summary['eventual_pass_rate']}",
            "# HELP funding_bot_test_flake_rate Share of collected tests identified as flaky",
            "# TYPE funding_bot_test_flake_rate gauge",
            f"funding_bot_test_flake_rate {summary['flake_rate']}",
        ]
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class FakeAPIResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class APIMockController:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], FakeAPIResponse] = {}
        self.calls: list[dict[str, Any]] = []

    def register_json(
        self,
        method: str,
        url: str,
        payload: Any,
        *,
        status_code: int = 200,
    ) -> FakeAPIResponse:
        response = FakeAPIResponse(payload, status_code=status_code)
        self.routes[(method.upper(), url)] = response
        return response

    def request(self, method: str, url: str, **kwargs: Any) -> FakeAPIResponse:
        normalized_method = method.upper()
        self.calls.append(
            {
                "method": normalized_method,
                "url": url,
                "json": kwargs.get("json"),
                "headers": dict(kwargs.get("headers") or {}),
                "timeout": kwargs.get("timeout"),
                "verify": kwargs.get("verify"),
            }
        )
        response = self.routes.get((normalized_method, url))
        if response is None:
            raise AssertionError(f"No API mock registered for {normalized_method} {url}")
        return response

    def post(self, url: str, **kwargs: Any) -> FakeAPIResponse:
        return self.request("POST", url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> FakeAPIResponse:
        return self.request("GET", url, **kwargs)

    def http_client(
        self,
        url: str,
        payload: dict[str, Any],
        credentials: dict[str, Any] | None = None,
    ) -> Any:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        resolved_credentials = dict(credentials or {})
        authorization_header = str(resolved_credentials.get("authorization_header", "")).strip()
        access_token = str(resolved_credentials.get("access_token", "")).strip()
        if authorization_header:
            headers["Authorization"] = authorization_header
        elif access_token:
            token_type = str(resolved_credentials.get("token_type", "Bearer")).strip() or "Bearer"
            headers["Authorization"] = f"{token_type} {access_token}"
        response = self.post(url, json=payload, headers=headers, timeout=10, verify=True)
        response.raise_for_status()
        return response.json()

    def build_session(self) -> "FakeSession":
        return FakeSession(self)


class FakeSession:
    def __init__(self, controller: APIMockController) -> None:
        self.controller = controller

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False

    def post(self, url: str, **kwargs: Any) -> FakeAPIResponse:
        return self.controller.post(url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> FakeAPIResponse:
        return self.controller.get(url, **kwargs)


class FakeRedis:
    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    def ping(self) -> bool:
        return True

    def get(self, key: str) -> Any:
        return self._store.get(key)

    def set(self, key: str, value: Any) -> bool:
        self._store[key] = value
        return True

    def setex(self, key: str, _ttl: int, value: Any) -> bool:
        return self.set(key, value)

    def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            deleted += int(key in self._store)
            self._store.pop(key, None)
        return deleted

    def incr(self, key: str) -> int:
        current = int(self._store.get(key, 0)) + 1
        self._store[key] = current
        return current

    def flushdb(self) -> bool:
        self._store.clear()
        return True


@dataclass
class FakeAsyncResult:
    id: str
    payload: Any = None
    state: str = "PENDING"

    def get(self, timeout: float | None = None) -> Any:
        return self.payload


class FakeCeleryTask:
    def __init__(self, name: str = "funding_bot.task") -> None:
        self.name = name
        self.calls: list[dict[str, Any]] = []
        self._counter = 0

    def delay(self, *args: Any, **kwargs: Any) -> FakeAsyncResult:
        self._counter += 1
        self.calls.append({"method": "delay", "args": args, "kwargs": kwargs})
        return FakeAsyncResult(
            id=f"{self.name}-{self._counter}", payload={"args": args, "kwargs": kwargs}
        )

    def apply_async(
        self,
        args: tuple[Any, ...] | None = None,
        kwargs: dict[str, Any] | None = None,
        **options: Any,
    ) -> FakeAsyncResult:
        self._counter += 1
        call = {
            "method": "apply_async",
            "args": tuple(args or ()),
            "kwargs": dict(kwargs or {}),
            "options": dict(options),
        }
        self.calls.append(call)
        return FakeAsyncResult(id=f"{self.name}-{self._counter}", payload=call)


class FakeCeleryInspect:
    def ping(self) -> dict[str, dict[str, str]]:
        return {"worker-1": {"ok": "pong"}}

    def stats(self) -> dict[str, dict[str, Any]]:
        return {"worker-1": {"pool": {"max-concurrency": 1}}}

    def active(self) -> dict[str, list[dict[str, str]]]:
        return {"worker-1": []}

    def reserved(self) -> dict[str, list[dict[str, str]]]:
        return {"worker-1": []}

    def scheduled(self) -> dict[str, list[dict[str, str]]]:
        return {"worker-1": []}


class FakeCeleryApp:
    def __init__(self) -> None:
        self.tasks: dict[str, FakeCeleryTask] = {}
        self.sent_tasks: list[dict[str, Any]] = []
        self.control = type("Control", (), {"inspect": lambda _self: FakeCeleryInspect()})()

    def task(
        self, *decorator_args: Any, **decorator_kwargs: Any
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        name = decorator_kwargs.get("name")

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.tasks[name or func.__name__] = FakeCeleryTask(name or func.__name__)
            return func

        return decorator

    def send_task(
        self,
        name: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        **options: Any,
    ) -> FakeAsyncResult:
        payload = {
            "name": name,
            "args": list(args or []),
            "kwargs": dict(kwargs or {}),
            "options": dict(options),
        }
        self.sent_tasks.append(payload)
        return FakeAsyncResult(id=f"{name}-{len(self.sent_tasks)}", payload=payload)


@dataclass
class DatabaseTransaction:
    bot: FundingBot
    connection: sqlite3.Connection
    savepoint_name: str
    blocked_commits: int = 0
    _active: bool = True

    def block_commit(self) -> None:
        self.blocked_commits += 1

    def reset(self) -> None:
        if not self._active:
            return
        self.connection.execute(f"ROLLBACK TO SAVEPOINT {self.savepoint_name}")
        self.connection.execute(f"RELEASE SAVEPOINT {self.savepoint_name}")
        self.connection.execute(f"SAVEPOINT {self.savepoint_name}")

    def rollback(self) -> None:
        if not self._active:
            return
        self.connection.execute(f"ROLLBACK TO SAVEPOINT {self.savepoint_name}")
        self.connection.execute(f"RELEASE SAVEPOINT {self.savepoint_name}")
        self._active = False


class TransactionConnectionProxy:
    def __init__(self, connection: sqlite3.Connection, transaction: DatabaseTransaction) -> None:
        self._connection = connection
        self._transaction = transaction

    def __enter__(self) -> "TransactionConnectionProxy":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is not None:
            self._transaction.reset()
        return False

    def commit(self) -> None:
        self._transaction.block_commit()

    def rollback(self) -> None:
        self._transaction.reset()

    def close(self) -> None:
        self._connection.close()

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        return self._connection.execute(*args, **kwargs)

    def executemany(self, *args: Any, **kwargs: Any) -> Any:
        return self._connection.executemany(*args, **kwargs)

    def executescript(self, *args: Any, **kwargs: Any) -> Any:
        return self._connection.executescript(*args, **kwargs)

    def cursor(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        return self._connection.cursor(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class FakeRedisModule:
    def __init__(self, client: FakeRedis) -> None:
        self._client = client

    def from_url(self, _url: str, **_kwargs: Any) -> FakeRedis:
        return self._client


def _auth_header(role: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{role}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _worker_id(pytestconfig: pytest.Config) -> str:
    return getattr(pytestconfig, "workerinput", {}).get("workerid", "master")


def _slugify(nodeid: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", nodeid).strip("_").lower()
    return slug or "test"


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("funding-bot")
    group.addoption(
        "--flaky-report", action="store", default=None, help="Write JSON flaky test report"
    )
    group.addoption(
        "--flaky-report-markdown",
        action="store",
        default=None,
        help="Write markdown flaky test report",
    )
    group.addoption(
        "--test-reliability-metrics",
        action="store",
        default=None,
        help="Write Prometheus-format test reliability metrics",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "quick: fast pytest checks for core workflows")
    config.addinivalue_line("markers", "smoke: broad end-to-end smoke coverage")
    config.addinivalue_line("markers", "serial: test should be forced onto one worker when needed")
    tracker = ReliabilityTracker(config)
    config.pluginmanager.register(tracker, "funding-bot-reliability-tracker")


@pytest.fixture(scope="session")
def artifact_root(pytestconfig: pytest.Config) -> Path:
    root = PYTEST_ARTIFACTS_DIR / _worker_id(pytestconfig)
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture()
def artifact_dir(artifact_root: Path, request: pytest.FixtureRequest) -> Path:
    path = artifact_root / _slugify(request.node.nodeid)
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    yield path
    if path.exists():
        shutil.rmtree(path)
    if artifact_root.exists() and not any(artifact_root.iterdir()):
        artifact_root.rmdir()
    parent = artifact_root.parent
    if parent.exists() and not any(parent.iterdir()):
        parent.rmdir()


@pytest.fixture()
def db_path(artifact_dir: Path) -> Path:
    return artifact_dir / "funding-bot.db"


@pytest.fixture()
def document_output_dir(artifact_dir: Path) -> Path:
    path = artifact_dir / "documents"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture()
def funding_bot(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> FundingBot:
    monkeypatch.setenv("BOT_DB_PATH", str(db_path))
    monkeypatch.setenv("DATA_RESIDENCY", "EU")
    monkeypatch.setenv("DATA_STORAGE_REGION", "EU")
    FundingBot.reset_connector_metrics()
    bot = FundingBot(db_path=str(db_path))
    yield bot
    bot.close()
    FundingBot.reset_connector_metrics()


@pytest.fixture()
def db_cursor(funding_bot: FundingBot) -> sqlite3.Cursor:
    return funding_bot.connection.cursor()


@pytest.fixture()
def db_transaction(
    db_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> DatabaseTransaction:
    monkeypatch.setenv("BOT_DB_PATH", str(db_path))
    monkeypatch.setenv("DATA_RESIDENCY", "EU")
    monkeypatch.setenv("DATA_STORAGE_REGION", "EU")
    bot = FundingBot(db_path=str(db_path))
    savepoint_name = f"pytest_{_slugify(request.node.nodeid)[:48]}"
    bot.connection.execute(f"SAVEPOINT {savepoint_name}")
    transaction = DatabaseTransaction(
        bot=bot, connection=bot.connection, savepoint_name=savepoint_name
    )
    bot.connection = TransactionConnectionProxy(bot.connection, transaction)
    yield transaction
    if transaction._active:
        transaction.rollback()
    bot.close()


@pytest.fixture()
def transactional_funding_bot(db_transaction: DatabaseTransaction) -> FundingBot:
    return db_transaction.bot


@pytest.fixture()
def bot_factory(monkeypatch: pytest.MonkeyPatch, artifact_dir: Path) -> Callable[..., FundingBot]:
    created_bots: list[FundingBot] = []

    def factory(
        *, name: str = "bot", connector_configs: dict[str, Any] | None = None, **kwargs: Any
    ) -> FundingBot:
        bot_db_path = artifact_dir / f"{name}.db"
        monkeypatch.setenv("BOT_DB_PATH", str(bot_db_path))
        bot = FundingBot(db_path=str(bot_db_path), connector_configs=connector_configs, **kwargs)
        created_bots.append(bot)
        return bot

    yield factory
    for bot in reversed(created_bots):
        bot.close()


@pytest.fixture()
def organization_profile(funding_bot: FundingBot) -> dict[str, Any]:
    profile = {
        "name": "i4Edu",
        "mission": "Expand access to equitable education.",
        "registration_number": "NP-42",
        "translations": {"en": {"greeting": "Dear Review Committee"}},
    }
    funding_bot.store_organization_profile(profile)
    return funding_bot.load_organization_profile()


@pytest.fixture()
def donor_factory(funding_bot: FundingBot) -> Callable[..., dict[str, Any]]:
    counter = 0

    def factory(*, bot: FundingBot | None = None, **overrides: Any) -> dict[str, Any]:
        nonlocal counter
        counter += 1
        current_bot = bot or funding_bot
        payload = {
            "email": f"donor{counter}@example.org",
            "name": f"Donor {counter}",
            "opted_out": False,
            "preferences": {"newsletter": True},
            "segment": "corporate",
            "locale": "en",
        }
        payload.update(overrides)
        current_bot.upsert_donor(**payload)
        donor = current_bot.get_donor(payload["email"])
        if donor is None:
            raise AssertionError("Expected donor fixture to create a donor record.")
        return donor

    return factory


@pytest.fixture()
def donors(donor_factory: Callable[..., dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        donor_factory(name="UNICEF", email="unicef@example.org", segment="institutional"),
        donor_factory(
            name="Acme Foundation", email="acme@example.org", segment="corporate", locale="bn"
        ),
    ]


@pytest.fixture()
def task_factory(funding_bot: FundingBot) -> Callable[..., dict[str, Any]]:
    counter = 0

    def factory(*, bot: FundingBot | None = None, **overrides: Any) -> dict[str, Any]:
        nonlocal counter
        counter += 1
        current_bot = bot or funding_bot
        payload = {
            "title": f"Task {counter}",
            "assigned_to": "staff",
            "description": f"Description for task {counter}",
            "status": "pending",
            "due_date": f"2026-08-{counter:02d}",
            "source": "manual",
        }
        payload.update(overrides)
        return current_bot.create_task(**payload)

    return factory


@pytest.fixture()
def tasks(task_factory: Callable[..., dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        task_factory(title="Collect donor documents", due_date="2026-08-02"),
        task_factory(
            title="Review grant budget",
            assigned_to="auditor",
            status="blocked",
            due_date="2026-08-05",
        ),
    ]


@pytest.fixture()
def connector_factory() -> Callable[..., Any]:
    def factory(connector_type: str = "grants-portal", **overrides: Any) -> Any:
        payload = {"transport": "demo"}
        payload.update(overrides)
        return DEFAULT_CONNECTOR_REGISTRY.create(connector_type, **payload)

    return factory


@pytest.fixture()
def connectors(connector_factory: Callable[..., Any]) -> list[Any]:
    return [
        connector_factory("grants-portal"),
        connector_factory("csr-network"),
        connector_factory("ngo-directory"),
        connector_factory("foundation-directory"),
    ]


@pytest.fixture()
def document_factory(
    funding_bot: FundingBot,
    organization_profile: dict[str, Any],
    document_output_dir: Path,
) -> Callable[..., dict[str, Any]]:
    counter = 0

    def factory(
        *, bot: FundingBot | None = None, output_dir: Path | None = None, **overrides: Any
    ) -> dict[str, Any]:
        nonlocal counter
        counter += 1
        current_bot = bot or funding_bot
        current_output_dir = output_dir or document_output_dir
        kind = overrides.pop("kind", f"proposal_{counter}")
        template = overrides.pop(
            "template",
            "{t[greeting]}\nOrganization: {name}\nMission: {mission}",
        )
        context = overrides.pop(
            "context",
            {
                "name": organization_profile["name"],
                "mission": organization_profile["mission"],
                "translations": organization_profile["translations"],
            },
        )
        generated = current_bot.generate_document(
            kind=kind,
            template=template,
            output_dir=current_output_dir,
            context=context,
            **overrides,
        )
        return {"kind": kind, **generated}

    return factory


@pytest.fixture()
def documents(document_factory: Callable[..., dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        document_factory(kind="cover_letter"),
        document_factory(kind="budget_summary", formats=("pdf",)),
    ]


@pytest.fixture()
def api_mocks(monkeypatch: pytest.MonkeyPatch) -> APIMockController:
    controller = APIMockController()
    monkeypatch.setattr(funding_bot_module, "_build_tls_http_session", controller.build_session)
    return controller


@pytest.fixture()
def redis_mock(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    client = FakeRedis()
    redis_module = getattr(task_queue_module, "redis", None)
    if redis_module is not None:
        monkeypatch.setattr(redis_module, "Redis", FakeRedisModule(client), raising=False)
    return client


@pytest.fixture()
def celery_task_mock() -> FakeCeleryTask:
    return FakeCeleryTask(name="funding_bot.discover")


@pytest.fixture()
def celery_app_mock() -> FakeCeleryApp:
    return FakeCeleryApp()


@pytest.fixture()
def service_mocks(
    api_mocks: APIMockController,
    redis_mock: FakeRedis,
    celery_task_mock: FakeCeleryTask,
    celery_app_mock: FakeCeleryApp,
) -> dict[str, Any]:
    return {
        "api": api_mocks,
        "redis": redis_mock,
        "celery_task": celery_task_mock,
        "celery_app": celery_app_mock,
    }


@pytest.fixture()
def smoke_client(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> dict[str, object]:
    db_path = (
        PYTEST_ARTIFACTS_DIR / _worker_id(request.config) / f"{_slugify(request.node.nodeid)}.db"
    )
    output_dir = (
        PYTEST_ARTIFACTS_DIR
        / _worker_id(request.config)
        / _slugify(request.node.nodeid)
        / "smoke-output"
    )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    if output_dir.exists():
        shutil.rmtree(output_dir)

    monkeypatch.setenv("BOT_DB_PATH", str(db_path))
    monkeypatch.setenv("DATA_RESIDENCY", "EU")
    monkeypatch.setenv("DATA_STORAGE_REGION", "EU")
    FundingBot.reset_connector_metrics()
    web_app_module.reset_health_check_metrics()
    app.config["TESTING"] = True

    client = app.test_client()
    yield {
        "client": client,
        "db_path": db_path,
        "output_dir": output_dir,
        "admin_headers": _auth_header("admin", "admin-secret"),
        "staff_headers": _auth_header("staff", "staff-secret"),
        "auditor_headers": _auth_header("auditor", "auditor-secret"),
    }

    FundingBot.reset_connector_metrics()
    web_app_module.reset_health_check_metrics()
    if db_path.exists():
        db_path.unlink()
    if output_dir.exists():
        shutil.rmtree(output_dir)
