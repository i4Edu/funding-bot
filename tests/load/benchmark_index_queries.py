from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from funding_bot import FundingBot

BENCHMARK_QUERIES = (
    (
        "donor-directory",
        "SELECT email, name FROM donors ORDER BY name COLLATE NOCASE ASC, email ASC LIMIT 50",
        (),
    ),
    (
        "donor-email-lookup",
        "SELECT email, name FROM donors WHERE email = ?",
        ("donor-0100@example.org",),
    ),
    (
        "task-assignee-status",
        (
            "SELECT id, title FROM tasks WHERE assigned_to = ? AND status = ? "
            "ORDER BY due_date ASC, id ASC LIMIT 50"
        ),
        ("staff", "todo"),
    ),
    (
        "task-status-created-at",
        (
            "SELECT id, title FROM tasks WHERE created_at >= ? AND status = ? "
            "ORDER BY created_at DESC LIMIT 50"
        ),
        ("2026-01-01T00:00:00+00:00", "todo"),
    ),
    (
        "connector-response-lookup",
        (
            "SELECT source_status, fetched_at FROM connector_result_cache "
            "WHERE connector_name = ? AND cache_key = ?"
        ),
        ("Grants Portal", "cache-0001"),
    ),
    (
        "connector-response-status",
        (
            "SELECT id, connector_name FROM connector_result_cache WHERE source_status = ? "
            "ORDER BY fetched_at DESC LIMIT 50"
        ),
        ("remote",),
    ),
)


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * 0.95)))
    return ordered[index] if ordered else 0.0


def _seed(bot: FundingBot, *, donors: int, tasks: int, connector_cache_entries: int) -> None:
    now = datetime(2026, 7, 19, tzinfo=timezone.utc)
    donor_rows = [
        (
            f"donor-{index:04d}@example.org",
            f"Donor {index % 250:03d}",
            1 if index % 11 == 0 else 0,
            json.dumps({"segment": "institutional" if index % 2 == 0 else "corporate"}),
            (now - timedelta(days=index % 30)).isoformat(),
            "en",
            "institutional" if index % 2 == 0 else "corporate",
            "secret",
            "{}",
        )
        for index in range(donors)
    ]
    bot.connection.executemany(
        """
        INSERT OR REPLACE INTO donors (
            email, name, opted_out, preferences_json, last_contact_at, locale,
            segment, data_classification, field_classifications_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        donor_rows,
    )

    task_rows = []
    task_statuses = ("todo", "in-progress", "blocked", "done")
    assignees = ("staff", "admin", "auditor")
    for index in range(tasks):
        created_at = now - timedelta(minutes=index)
        task_rows.append(
            (
                f"benchmark-{index:04d}",
                f"Benchmark task {index}",
                "Task seeded for index benchmarks.",
                assignees[index % len(assignees)],
                assignees[index % len(assignees)],
                task_statuses[index % len(task_statuses)],
                (now.date() + timedelta(days=index % 14)).isoformat(),
                "benchmark",
                created_at.isoformat(),
                created_at.isoformat(),
            )
        )
    bot.connection.executemany(
        """
        INSERT OR REPLACE INTO tasks (
            external_id, title, description, assignee, assigned_to, status,
            due_date, source, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        task_rows,
    )

    cache_rows = []
    statuses = ("remote", "cached", "default", "degraded")
    for index in range(connector_cache_entries):
        fetched_at = now - timedelta(minutes=index)
        cache_rows.append(
            (
                "Grants Portal" if index % 2 == 0 else "CSR Network",
                f"cache-{index:04d}",
                2,
                fetched_at.isoformat(),
                statuses[index % len(statuses)],
                json.dumps({"seed": True, "index": index}, sort_keys=True),
                json.dumps([{"title": f"Opportunity {index}"}], sort_keys=True),
                "internal",
            )
        )
    bot.connection.executemany(
        """
        INSERT OR REPLACE INTO connector_result_cache (
            connector_name, cache_key, schema_version, fetched_at,
            source_status, metadata_json, result_json, data_classification
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        cache_rows,
    )
    bot.connection.commit()


def _benchmark(bot: FundingBot, *, iterations: int) -> None:
    print("Query benchmark summary")
    for name, query, params in BENCHMARK_QUERIES:
        durations: list[float] = []
        for _ in range(5):
            bot.connection.execute(query, params).fetchall()
        for _ in range(iterations):
            started_at = time.perf_counter()
            bot.connection.execute(query, params).fetchall()
            durations.append((time.perf_counter() - started_at) * 1000)
        print(
            f"- {name}: avg_ms={statistics.fmean(durations):.3f}, "
            f"p95_ms={_p95(durations):.3f}, iterations={iterations}"
        )

    print("\nRepresentative EXPLAIN QUERY PLAN output")
    for plan in bot.explain_indexed_queries():
        print(f"- {plan['name']}: uses_index={plan['uses_index']}, indexes={plan['indexes']}")
        for detail in plan["plan"]:
            print(f"    {detail}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark indexed SQLite query paths.")
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--donors", type=int, default=1000)
    parser.add_argument("--tasks", type=int, default=2000)
    parser.add_argument("--connector-cache-entries", type=int, default=1000)
    parser.add_argument("--iterations", type=int, default=50)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db_path)
    if args.reset and db_path.exists():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    bot = FundingBot(db_path=str(db_path))
    try:
        _seed(
            bot,
            donors=args.donors,
            tasks=args.tasks,
            connector_cache_entries=args.connector_cache_entries,
        )
        _benchmark(bot, iterations=args.iterations)
    finally:
        bot.close()


if __name__ == "__main__":
    main()
