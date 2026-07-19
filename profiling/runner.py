from __future__ import annotations

import argparse
import cProfile
import html
import json
import pstats
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from flask import g

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from funding_bot import FundingBot, GrantsPortalConnector  # noqa: E402
from web.app import (
    _dashboard_context,
    _task_dashboard_context,
)
from web.app import app as flask_app  # noqa: E402
from web.app import (
    list_donors,
    list_opportunities,
)

REPORT_TIMESTAMP = datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc)
BASELINE_PATH = PROJECT_ROOT / "profiling" / "baselines.json"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "profiling" / "reports" / "latest"


@dataclass(slots=True)
class OperationDefinition:
    name: str
    description: str
    metadata: dict[str, Any]
    factory: Callable[[], Callable[[], None]]


@dataclass(slots=True)
class BenchmarkResult:
    name: str
    description: str
    iterations: int
    metadata: dict[str, Any]
    durations_seconds: list[float]
    mean_seconds: float
    median_seconds: float
    p95_seconds: float
    min_seconds: float
    max_seconds: float
    cprofile_stats_path: str | None = None
    cprofile_text_path: str | None = None
    flamegraph_svg_path: str | None = None
    baseline_seconds: float | None = None
    max_allowed_seconds: float | None = None
    regression_detected: bool = False
    notes: list[str] = field(default_factory=list)


class RegressionError(RuntimeError):
    """Raised when a benchmark exceeds its configured baseline."""


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = ratio * (len(ordered) - 1)
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _slugify(name: str) -> str:
    return name.strip().lower().replace(" ", "-").replace("_", "-")


def _build_deduplication_payload(*, unique_records: int = 1600, duplicates_per_record: int = 2) -> list[dict[str, Any]]:
    opportunities: list[dict[str, Any]] = []
    for index in range(unique_records):
        source = "Grants Portal" if index % 2 == 0 else "CSR Network"
        record = {
            "source": source,
            "donor_name": f"Donor {index % 40}",
            "title": f"Education Opportunity {index}",
            "portal_url": f"https://example.org/opportunities/{index}",
            "summary": "Funding for education, literacy, and digital learning programs.",
            "tags": ["education", "csr", "literacy"],
            "category": "Education",
        }
        for _ in range(duplicates_per_record):
            opportunities.append(dict(record))
    return opportunities


def _seed_dashboard_bot(
    bot: FundingBot,
    *,
    opportunities: int = 240,
    tasks: int = 360,
    donors: int = 32,
    translation_reviews: int = 24,
) -> None:
    bot.store_organization_profile(
        {
            "name": "i4Edu",
            "mission": "Expand access to equitable education.",
            "contact_email": "ops@i4edu.example.org",
            "website": "https://i4edu.example.org",
        }
    )
    bot.store_search_settings(
        keywords=["education", "digital learning", "csr"],
        trusted_sources=["Grants Portal", "CSR Network"],
    )

    discovered = bot.deduplicate(
        [
            {
                "source": "Grants Portal" if index % 2 == 0 else "CSR Network",
                "donor_name": f"Donor {index % 24}",
                "title": f"Education Opportunity {index}",
                "portal_url": f"https://example.org/opportunities/{index}",
                "summary": "Funding for equitable education and digital learning.",
                "tags": ["education", "digital learning", "csr"],
                "category": "Education",
            }
            for index in range(opportunities)
        ],
        keywords=["education", "digital learning", "csr"],
        trusted_sources=["Grants Portal", "CSR Network"],
        discovered_at=REPORT_TIMESTAMP,
    )

    for index, opportunity in enumerate(discovered[: max(1, opportunities // 5)]):
        bot.submit_application(
            opportunity["signature"],
            submission_reference=f"submission-{index}",
            status="submitted" if index % 2 == 0 else "in_review",
            next_action="Await donor review",
            submitted_at=REPORT_TIMESTAMP - timedelta(hours=index),
        )

    for index in range(donors):
        bot.send_outreach(
            donor_email=f"donor-{index}@example.org",
            donor_name=f"Donor {index}",
            subject_template="Support {organization_name}",
            body_template="Hello {donor_name},\n\n{mission}",
            context={"opt_out_url": "https://i4edu.example.org/opt-out"},
            sent_at=REPORT_TIMESTAMP - timedelta(days=index % 10),
        )

    statuses = ["pending", "in_progress", "done", "blocked"]
    assignees = ["admin", "staff", "auditor"]
    for index in range(tasks):
        due_date = (REPORT_TIMESTAMP.date() + timedelta(days=(index % 14) - 5)).isoformat()
        bot.create_task(
            title=f"Profiled task {index}",
            assignee=assignees[index % len(assignees)],
            description="Synthetic task for dashboard profiling.",
            status=statuses[index % len(statuses)],
            due_date=due_date,
            source="profiling_seed",
        )

    for index in range(translation_reviews):
        review = bot.submit_translation_review(
            locale="bn" if index % 2 == 0 else "en",
            translation_key=f"dashboard.metric.{index}",
            source_text=f"Dashboard metric label {index}",
            translated_text=f"Translated metric label {index}",
            submitted_by_role="admin",
            created_at=REPORT_TIMESTAMP - timedelta(minutes=index),
        )
        if index % 3 == 1:
            bot.review_translation(
                review["id"],
                status="approved",
                reviewed_by_role="auditor",
                reviewer_notes="Looks good.",
                reviewed_at=REPORT_TIMESTAMP - timedelta(minutes=index - 1),
            )
        elif index % 3 == 2:
            bot.review_translation(
                review["id"],
                status="rejected",
                reviewed_by_role="auditor",
                reviewer_notes="Needs terminology update.",
                reviewed_at=REPORT_TIMESTAMP - timedelta(minutes=index - 1),
            )


def build_operation_registry() -> dict[str, OperationDefinition]:
    dedup_payload = _build_deduplication_payload()

    def deduplication_factory() -> Callable[[], None]:
        def run() -> None:
            bot = FundingBot(trusted_sources={"Grants Portal", "CSR Network"})
            try:
                bot.deduplicate(
                    dedup_payload,
                    keywords=["education", "csr", "literacy"],
                    trusted_sources=["Grants Portal", "CSR Network"],
                    discovered_at=REPORT_TIMESTAMP,
                )
            finally:
                bot.close()

        return run

    page_size = 75
    total_pages = 4

    def fake_http_client(
        url: str,
        payload: dict[str, Any],
        credentials: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        del url, credentials, headers
        page = int(payload.get("page", 1))
        opportunities = []
        for offset in range(page_size):
            index = ((page - 1) * page_size) + offset
            opportunities.append(
                {
                    "source": "Grants Portal",
                    "donor_name": f"Agency {index % 15}",
                    "title": f"STEM Program {index}",
                    "portal_url": f"https://example.org/grants/{index}",
                    "summary": "Remote API payload used for profiling connector pagination.",
                    "category": "Education",
                    "tags": ["education", "youth", "innovation"],
                }
            )
        return {
            "schema_version": 2,
            "opportunities": opportunities,
            "next_page": page + 1 if page < total_pages else None,
        }

    def connector_factory() -> Callable[[], None]:
        def run() -> None:
            connector = GrantsPortalConnector(
                http_client=fake_http_client,
                transport="http",
                page_size=page_size,
                max_retries=0,
            )
            connector.fetch_result(["education", "youth", "innovation"])

        return run

    def dashboard_factory() -> Callable[[], None]:
        def run() -> None:
            bot = FundingBot(trusted_sources={"Grants Portal", "CSR Network"})
            try:
                _seed_dashboard_bot(bot)
                with flask_app.test_request_context("/dashboard"):
                    g.current_role = "admin"
                    g._bot = bot
                    _dashboard_context()
                    _task_dashboard_context(
                        {
                            "assignee": None,
                            "status": None,
                            "due_date_before": None,
                            "due_date_after": None,
                            "sort": "updated_at",
                        }
                    )
                    list_opportunities().get_json()
                    list_donors().get_json()
            finally:
                bot.close()

        return run

    return {
        "deduplication": OperationDefinition(
            name="deduplication",
            description="FundingBot.deduplicate() on a mixed trusted-source opportunity batch.",
            metadata={
                "records": len(dedup_payload),
                "unique_records": len(dedup_payload) // 2,
                "keywords": ["education", "csr", "literacy"],
            },
            factory=deduplication_factory,
        ),
        "connector_calls": OperationDefinition(
            name="connector_calls",
            description="Paginated connector fetch_result() calls over a synthetic remote API.",
            metadata={
                "pages": total_pages,
                "page_size": page_size,
                "connector": "GrantsPortalConnector",
            },
            factory=connector_factory,
        ),
        "dashboard_queries": OperationDefinition(
            name="dashboard_queries",
            description="Dashboard summary/task queries plus opportunities/donors JSON serialization.",
            metadata={
                "opportunities": 240,
                "tasks": 360,
                "donors": 32,
                "translation_reviews": 24,
            },
            factory=dashboard_factory,
        ),
    }


def load_baselines(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def apply_baselines(results: list[BenchmarkResult], baselines: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    operations = baselines.get("operations", {})
    for result in results:
        baseline = operations.get(result.name)
        if not baseline:
            result.notes.append("No committed baseline configured.")
            continue
        baseline_seconds = float(baseline["baseline_seconds"])
        factor = float(baseline.get("max_regression_factor", 1.0))
        allowance = float(baseline.get("allowed_overhead_seconds", 0.0))
        max_allowed = baseline_seconds * factor + allowance
        result.baseline_seconds = baseline_seconds
        result.max_allowed_seconds = max_allowed
        result.regression_detected = result.mean_seconds > max_allowed
        if result.regression_detected:
            message = (
                f"{result.name} mean {result.mean_seconds:.4f}s exceeded allowed "
                f"{max_allowed:.4f}s (baseline {baseline_seconds:.4f}s)"
            )
            result.notes.append(message)
            failures.append(message)
    return failures


def render_html_report(results: list[BenchmarkResult], output_dir: Path) -> Path:
    rows: list[str] = []
    for result in results:
        status = "REGRESSION" if result.regression_detected else "OK"
        rows.append(
            "<tr>"
            f"<td>{html.escape(result.name)}</td>"
            f"<td>{html.escape(result.description)}</td>"
            f"<td>{result.iterations}</td>"
            f"<td>{result.mean_seconds:.4f}</td>"
            f"<td>{result.median_seconds:.4f}</td>"
            f"<td>{result.p95_seconds:.4f}</td>"
            f"<td>{'' if result.baseline_seconds is None else f'{result.baseline_seconds:.4f}'}</td>"
            f"<td>{'' if result.max_allowed_seconds is None else f'{result.max_allowed_seconds:.4f}'}</td>"
            f"<td>{status}</td>"
            f"<td>{_artifact_links(result)}</td>"
            "</tr>"
        )

    generated_at = datetime.now(timezone.utc).isoformat()
    document = f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <title>Funding Bot profiling report</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 2rem; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #d0d7de; padding: 0.5rem; text-align: left; vertical-align: top; }}
    th {{ background: #f6f8fa; }}
    .notes {{ margin-top: 2rem; }}
    code {{ background: #f6f8fa; padding: 0.1rem 0.3rem; }}
  </style>
</head>
<body>
  <h1>Funding Bot profiling report</h1>
  <p>Generated at <code>{html.escape(generated_at)}</code>.</p>
  <table>
    <thead>
      <tr>
        <th>Operation</th>
        <th>Description</th>
        <th>Iterations</th>
        <th>Mean (s)</th>
        <th>Median (s)</th>
        <th>P95 (s)</th>
        <th>Baseline (s)</th>
        <th>Allowed max (s)</th>
        <th>Status</th>
        <th>Artifacts</th>
      </tr>
    </thead>
    <tbody>
      {''.join(rows)}
    </tbody>
  </table>
  <div class=\"notes\">
    <h2>Notes</h2>
    <ul>
      {''.join(_note_items(results))}
    </ul>
  </div>
</body>
</html>
"""
    output_path = output_dir / "index.html"
    output_path.write_text(document, encoding="utf-8")
    return output_path


def _artifact_links(result: BenchmarkResult) -> str:
    links: list[str] = []
    if result.cprofile_text_path:
        links.append(f'<a href="{html.escape(result.cprofile_text_path)}">stats.txt</a>')
    if result.cprofile_stats_path:
        links.append(f'<a href="{html.escape(result.cprofile_stats_path)}">profile.prof</a>')
    if result.flamegraph_svg_path:
        links.append(f'<a href="{html.escape(result.flamegraph_svg_path)}">flamegraph.svg</a>')
    return " | ".join(links)


def _note_items(results: list[BenchmarkResult]) -> list[str]:
    items: list[str] = []
    for result in results:
        items.append(
            f"<li><strong>{html.escape(result.name)}</strong>: "
            f"workload={html.escape(json.dumps(result.metadata, sort_keys=True))}</li>"
        )
        for note in result.notes:
            items.append(f"<li>{html.escape(note)}</li>")
    return items or ["<li>No notes recorded.</li>"]


def _result_to_dict(result: BenchmarkResult) -> dict[str, Any]:
    payload = asdict(result)
    payload["metadata"] = dict(result.metadata)
    return payload


def write_metrics_report(results: list[BenchmarkResult], output_dir: Path) -> Path:
    output_path = output_dir / "metrics.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "results": [_result_to_dict(result) for result in results],
    }
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return output_path


def benchmark_operation(
    definition: OperationDefinition,
    *,
    iterations: int,
    output_dir: Path,
) -> BenchmarkResult:
    durations: list[float] = []
    for _ in range(iterations):
        operation = definition.factory()
        started = time.perf_counter()
        operation()
        durations.append(time.perf_counter() - started)

    profiler = cProfile.Profile()
    profiled_operation = definition.factory()
    profiler.runcall(profiled_operation)

    slug = _slugify(definition.name)
    stats_path = output_dir / f"{slug}.prof"
    text_path = output_dir / f"{slug}.txt"
    profiler.dump_stats(str(stats_path))
    with text_path.open("w", encoding="utf-8") as handle:
        stats = pstats.Stats(profiler, stream=handle)
        stats.sort_stats("cumulative")
        stats.print_stats(40)

    return BenchmarkResult(
        name=definition.name,
        description=definition.description,
        iterations=iterations,
        metadata=dict(definition.metadata),
        durations_seconds=durations,
        mean_seconds=statistics.fmean(durations),
        median_seconds=statistics.median(durations),
        p95_seconds=percentile(durations, 0.95),
        min_seconds=min(durations),
        max_seconds=max(durations),
        cprofile_stats_path=stats_path.name,
        cprofile_text_path=text_path.name,
    )


def generate_flamegraph(operation_name: str, *, output_dir: Path) -> str:
    output_path = output_dir / f"{_slugify(operation_name)}.svg"
    command = [
        "py-spy",
        "record",
        "--format",
        "flamegraph",
        "--output",
        str(output_path),
        "--",
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "profile_operations.py"),
        "--worker-operation",
        operation_name,
    ]
    subprocess.run(command, check=True, cwd=str(PROJECT_ROOT))
    return output_path.name


def run_profile_suite(
    operation_names: list[str],
    *,
    iterations: int,
    output_dir: Path,
    baseline_path: Path | None,
    check_regressions: bool,
    with_flamegraphs: bool,
) -> list[BenchmarkResult]:
    registry = build_operation_registry()
    missing = [name for name in operation_names if name not in registry]
    if missing:
        raise ValueError(f"Unknown operations: {', '.join(missing)}")

    output_dir.mkdir(parents=True, exist_ok=True)
    results = [benchmark_operation(registry[name], iterations=iterations, output_dir=output_dir) for name in operation_names]

    if with_flamegraphs:
        for result in results:
            result.flamegraph_svg_path = generate_flamegraph(result.name, output_dir=output_dir)

    failures: list[str] = []
    if baseline_path is not None and baseline_path.exists():
        failures = apply_baselines(results, load_baselines(baseline_path))

    write_metrics_report(results, output_dir)
    render_html_report(results, output_dir)
    for result in results:
        print(
            f"{result.name}: mean={result.mean_seconds:.4f}s median={result.median_seconds:.4f}s "
            f"p95={result.p95_seconds:.4f}s"
        )

    if failures and check_regressions:
        raise RegressionError("; ".join(failures))
    return results


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    registry = build_operation_registry()
    parser = argparse.ArgumentParser(description="Profile Funding Bot hot paths.")
    parser.add_argument(
        "--operation",
        action="append",
        choices=sorted(registry),
        help="Operation(s) to profile. Defaults to all profiling targets.",
    )
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--compare-baseline", default=str(BASELINE_PATH))
    parser.add_argument("--check-regressions", action="store_true")
    parser.add_argument("--with-flamegraphs", action="store_true")
    parser.add_argument("--worker-operation", choices=sorted(registry))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    registry = build_operation_registry()
    if args.worker_operation:
        registry[args.worker_operation].factory()()
        return 0

    selected_operations = args.operation or list(registry)
    baseline_path = Path(args.compare_baseline) if args.compare_baseline else None
    run_profile_suite(
        selected_operations,
        iterations=max(1, int(args.iterations)),
        output_dir=Path(args.output_dir),
        baseline_path=baseline_path,
        check_regressions=bool(args.check_regressions),
        with_flamegraphs=bool(args.with_flamegraphs),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
