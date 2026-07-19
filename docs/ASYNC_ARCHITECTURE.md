# Async architecture

Funding Bot now supports an async discovery path for connector I/O and opportunity persistence.

## Core pieces

- `FundingBot.run_discovery_async(...)` batches connector fetches and persists discoveries through async database sessions.
- `AsyncDatabaseSession` wraps the shared SQLite connection in an async context manager so async discovery code can `await` database reads/writes safely.
- `_BasePortalConnector.fetch_result_async(...)` and `_default_http_json_client_async(...)` use `asyncio` and `aiohttp` for non-blocking connector calls.
- `ConnectorBatchScheduler` groups connector requests into configurable batches and coalesces duplicate connector+keyword requests inside the same scheduling window.
- `BatchProcessingMetricsRegistry` records batch counts, coalesced requests, batch sizes, and batch duration metrics. The Prometheus `/metrics` endpoint exposes these values.

## Batch scheduling

`FundingBot.run_discovery_async(..., batch_size=...)` accepts an explicit batch size. When omitted, Funding Bot reads:

- `FUNDING_BOT_CONNECTOR_BATCH_SIZE`
- `CONNECTOR_BATCH_SIZE`

If neither is set, the default batch size is `5`.

## Request coalescing

The scheduler builds a stable request key from the connector identity plus its normalized cache key. When the same request appears multiple times in one batch submission, Funding Bot executes it once and shares the result with all waiting callers.

## Sync compatibility

Existing synchronous entry points still work:

- `FundingBot.run_discovery(...)`
- `FundingBot.deduplicate(...)`
- `FundingBot.discover_opportunities(...)`

These methods bridge into the async implementation so existing callers do not need to change immediately.
