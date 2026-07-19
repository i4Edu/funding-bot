from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest


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
