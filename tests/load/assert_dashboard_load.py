from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path


def _load_aggregated_stats(csv_prefix: str) -> dict[str, str]:
    stats_path = Path(f"{csv_prefix}_stats.csv")
    with stats_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if row.get("Name") == "Aggregated":
                return row
    raise RuntimeError(f"Could not find aggregated Locust stats in {stats_path}.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate dashboard load-test thresholds.")
    parser.add_argument("--csv-prefix", required=True)
    parser.add_argument("--max-failures", type=int, default=0)
    parser.add_argument("--max-p95-ms", type=float, default=750.0)
    parser.add_argument("--min-rps", type=float, default=5.0)
    args = parser.parse_args()

    stats = _load_aggregated_stats(args.csv_prefix)
    request_count = int(float(stats["Request Count"]))
    failure_count = int(float(stats["Failure Count"]))
    median_ms = float(stats["Median Response Time"])
    avg_ms = float(stats["Average Response Time"])
    p95_ms = float(stats["95%"])
    requests_per_second = float(stats["Requests/s"])

    print(
        "Dashboard load-test summary: "
        f"requests={request_count}, failures={failure_count}, "
        f"median_ms={median_ms:.2f}, avg_ms={avg_ms:.2f}, "
        f"p95_ms={p95_ms:.2f}, rps={requests_per_second:.2f}"
    )

    failures: list[str] = []
    if request_count <= 0:
        failures.append("no requests were recorded")
    if failure_count > args.max_failures:
        failures.append(f"failure count {failure_count} exceeded {args.max_failures}")
    if p95_ms > args.max_p95_ms:
        failures.append(f"p95 {p95_ms:.2f}ms exceeded {args.max_p95_ms:.2f}ms")
    if requests_per_second < args.min_rps:
        failures.append(
            f"throughput {requests_per_second:.2f} req/s was below {args.min_rps:.2f} req/s"
        )

    if failures:
        for failure in failures:
            print(f"LOAD TEST CHECK FAILED: {failure}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
