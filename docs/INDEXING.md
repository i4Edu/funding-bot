# Database Indexing Strategy

This project uses targeted SQLite indexes for read-heavy donor, task, and connector-cache workflows.

## Indexed paths

| Table | Index | Purpose |
| --- | --- | --- |
| `donors` | `idx_donors_email` | Fast donor lookups by email. |
| `donors` | `idx_donors_name_email` | Covers donor directory ordering and name-first lookups. |
| `tasks` | `idx_tasks_status` | Existing status filter used by task counts and filtered lists. |
| `tasks` | `idx_tasks_created_at_status` | Covers created-at sorting plus status filtering. |
| `tasks` | `idx_tasks_status_created_at` | Optimizes the common `WHERE status = ? ORDER BY created_at DESC` task view. |
| `tasks` | `idx_tasks_assigned_to_status` | Optimizes task lists filtered by assignee and status. |
| `connector_result_cache` | `idx_connector_result_cache_lookup` | Existing lookup path for connector/cache-key fetches. |
| `connector_result_cache` | `idx_connector_result_cache_status_fetched_at` | Optimizes recent connector-response monitoring grouped by cache status. |

## Migration

Index creation is applied in two layers:

1. `migrations/002_add_query_indexes.sql` adds the portable indexes for upgraded databases.
2. `FundingBot._ensure_query_indexes()` backfills runtime indexes that depend on later schema upgrades, such as `idx_tasks_assigned_to_status`.

## Representative EXPLAIN plans

The application exposes `FundingBot.explain_indexed_queries()` and `FundingBot.get_database_index_metrics()` to capture representative `EXPLAIN QUERY PLAN` output for:

- donor directory ordering
- donor email lookups
- task assignee/status filters
- task created-at/status filters
- connector cache lookups
- connector response status history

You can inspect this data through:

- `GET /health/database`
- `GET /metrics`
- Python API usage:

```python
from funding_bot import FundingBot

bot = FundingBot(db_path="funding_bot.db")
print(bot.get_database_index_metrics())
bot.close()
```

## Monitoring queries

Useful direct SQL checks:

```sql
PRAGMA index_list('donors');
PRAGMA index_list('tasks');
PRAGMA index_list('connector_result_cache');

SELECT source_status, COUNT(*) AS total
FROM connector_result_cache
GROUP BY source_status
ORDER BY source_status;

EXPLAIN QUERY PLAN
SELECT id, title
FROM tasks
WHERE created_at >= '2026-01-01T00:00:00+00:00' AND status = 'todo'
ORDER BY created_at DESC
LIMIT 25;
```

## Benchmarking

Run the benchmark helper against a dedicated local database file:

```bash
python tests/load/benchmark_index_queries.py \
  --db-path .test-artifacts/index-benchmark.db \
  --donors 1000 \
  --tasks 2000 \
  --connector-cache-entries 1000 \
  --iterations 50
```

The benchmark prints average and p95 latencies alongside the representative query plans.
