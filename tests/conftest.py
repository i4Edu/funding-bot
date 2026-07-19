from __future__ import annotations

import base64
import json
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from funding_bot import FundingBot  # noqa: E402
from scripts.mock_connector_server import create_server  # noqa: E402
from web.app import app  # noqa: E402

SEED_TIMESTAMP = datetime(2026, 7, 19, 9, 0, tzinfo=timezone.utc)

os.environ.setdefault("ADMIN_PASSWORD", "admin-secret")
os.environ.setdefault("STAFF_PASSWORD", "staff-secret")
os.environ.setdefault("AUDITOR_PASSWORD", "auditor-secret")


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


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("funding-bot")
    group.addoption("--flaky-report", action="store", default=None, help="Write JSON flaky test report")
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
    tracker = ReliabilityTracker(config)
    config.pluginmanager.register(tracker, "funding-bot-reliability-tracker")
    config.addinivalue_line("markers", "smoke: marks end-to-end smoke coverage")
    config.addinivalue_line("markers", "quick: marks the fast smoke subset")


def _auth_header(role: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{role}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _shared_memory_uri(nodeid: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", nodeid.lower()).strip("-") or "test"
    return f"file:{slug}-{uuid4().hex}?mode=memory&cache=shared"


def _seed_test_database(bot: FundingBot) -> dict[str, object]:
    bot.store_organization_profile(
        {
            "name": "i4Edu",
            "mission": "Expand access to equitable education.",
            "contact_email": "ops@i4edu.example.org",
        }
    )
    bot.store_search_settings(
        keywords=["education", "digital learning", "csr"],
        trusted_sources=["Grants Portal", "CSR Network"],
    )
    opportunities = bot.discover_opportunities(
        [
            {
                "source": "Grants Portal",
                "donor_name": "Global Education Fund",
                "title": "Seeded Education Innovation Grant",
                "portal_url": "https://example.org/opportunities/seeded-education",
                "summary": "Seeded funding opportunity for education pilots.",
                "tags": ["education", "innovation"],
                "category": "Education",
            },
            {
                "source": "CSR Network",
                "donor_name": "Mock Corporate Giving",
                "title": "Seeded Digital Learning Fund",
                "portal_url": "https://example.org/opportunities/seeded-csr",
                "summary": "Seeded CSR opportunity for digital learning programs.",
                "tags": ["csr", "digital learning"],
                "category": "Corporate Partnerships",
            },
        ],
        keywords=["education", "digital learning", "csr"],
        discovered_at=SEED_TIMESTAMP,
    )
    bot.submit_application(
        opportunities[0]["signature"],
        submission_reference="seeded-submission-001",
        status="submitted",
        next_action="Await donor review",
        submitted_at=SEED_TIMESTAMP,
    )
    bot.upsert_donor(
        email="seeded-donor@example.org",
        name="Seeded Donor",
        segment="institutional",
        locale="en",
    )
    task = bot.create_task(
        title="Seeded follow-up task",
        description="Review the seeded grant opportunity.",
        assignee="staff",
        status="pending",
        due_date="2026-07-25",
        source="pytest_seed",
    )
    review = bot.submit_translation_review(
        locale="bn",
        translation_key="dashboard.metric.seeded",
        source_text="Seeded metric label",
        translated_text="সিডেড মেট্রিক লেবেল",
        submitted_by_role="admin",
        created_at=SEED_TIMESTAMP,
    )
    return {
        "organization_name": "i4Edu",
        "opportunity_signatures": [item["signature"] for item in opportunities],
        "task_id": task["id"],
        "translation_review_id": review["id"],
        "counts": {
            "opportunities": len(opportunities),
            "tasks": 1,
            "donors": 1,
            "translation_reviews": 1,
        },
    }


@pytest.fixture()
def bot_factory(request: pytest.FixtureRequest):
    db_path = _shared_memory_uri(request.node.nodeid)
    created_bots: list[FundingBot] = []
    keeper = FundingBot(
        db_path=db_path,
        trusted_sources={"Grants Portal", "CSR Network"},
    )
    created_bots.append(keeper)

    def create_bot(**kwargs: object) -> FundingBot:
        kwargs.setdefault("db_path", db_path)
        kwargs.setdefault("trusted_sources", {"Grants Portal", "CSR Network"})
        bot = FundingBot(**kwargs)
        created_bots.append(bot)
        return bot

    create_bot.db_path = db_path  # type: ignore[attr-defined]
    yield create_bot

    for bot in reversed(created_bots):
        bot.close()


@pytest.fixture()
def seeded_database(bot_factory):
    bot = bot_factory()
    seed_data = _seed_test_database(bot)
    return {"db_path": bot_factory.db_path, **seed_data}


@pytest.fixture()
def app_client(monkeypatch: pytest.MonkeyPatch, bot_factory):
    monkeypatch.setenv("BOT_DB_PATH", bot_factory.db_path)
    monkeypatch.setenv("DATA_RESIDENCY", "EU")
    monkeypatch.setenv("DATA_STORAGE_REGION", "EU")
    FundingBot.reset_connector_metrics()
    app.config["TESTING"] = True

    yield {
        "client": app.test_client(),
        "db_path": bot_factory.db_path,
        "admin_headers": _auth_header("admin", "admin-secret"),
        "staff_headers": _auth_header("staff", "staff-secret"),
        "auditor_headers": _auth_header("auditor", "auditor-secret"),
    }

    FundingBot.reset_connector_metrics()


@pytest.fixture()
def seeded_app_client(app_client, seeded_database):
    return {**app_client, "seed_data": seeded_database}


@pytest.fixture()
def mock_connector_server(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FUNDING_BOT_ALLOW_INSECURE_CONNECTOR_URLS", "1")
    server = create_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base_url = f"http://{host}:{port}"
    try:
        yield {
            "base_url": base_url,
            "health_url": f"{base_url}/health",
            "grants_portal_url": f"{base_url}/grants-portal",
            "csr_network_url": f"{base_url}/csr-network",
            "sandbox_url": f"{base_url}/sandbox",
        }
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
