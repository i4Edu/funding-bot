# Profiling Funding Bot

Funding Bot ships with repeatable profiling utilities for the three most common performance hot paths:

- `FundingBot.deduplicate()`
- connector API fetches (`fetch_result()` on the built-in connector stack)
- dashboard summary and task-board queries

The profiler uses the standard-library `cProfile` for deterministic call stats and `py-spy` for SVG flame graphs.

## Install tooling

```bash
pip install -r requirements.txt
```

## Generate local reports

```bash
python scripts/profile_operations.py \
  --iterations 5 \
  --output-dir profiling/reports/local \
  --compare-baseline profiling/baselines.json
```

Outputs:

- `profiling/reports/local/metrics.json` — structured timings and regression status
- `profiling/reports/local/index.html` — HTML summary dashboard
- `profiling/reports/local/*.prof` — raw `cProfile` dumps
- `profiling/reports/local/*.txt` — top cumulative `cProfile` frames

## Generate flame graphs

```bash
python scripts/profile_operations.py \
  --iterations 5 \
  --output-dir profiling/reports/local \
  --with-flamegraphs
```

This adds one SVG flame graph per operation:

- `deduplication.svg`
- `connector-calls.svg`
- `dashboard-queries.svg`

## Fail on regressions

```bash
python scripts/profile_operations.py \
  --iterations 5 \
  --output-dir profiling/reports/ci \
  --compare-baseline profiling/baselines.json \
  --check-regressions \
  --with-flamegraphs
```

A regression fails when an operation's mean runtime exceeds the committed baseline plus its configured tolerance. Baselines live in `profiling/baselines.json`.

## CI behavior

The `profiling-regression` GitHub Actions job:

1. installs `requirements.txt`
2. runs `tests.test_profiling_tools`
3. generates HTML/JSON/`cProfile`/SVG reports
4. fails the workflow if a baseline is exceeded
5. uploads the generated profiling reports as a workflow artifact

Use the committed baselines for coarse regression detection, then inspect the HTML summary and SVG flame graphs to understand where the extra time was spent.
