import asyncio
import sys
import types
import unittest
from unittest import mock

if "pyotp" not in sys.modules:
    sys.modules["pyotp"] = types.SimpleNamespace(TOTP=object)

if "observability" not in sys.modules:

    class _Span:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def set_attribute(self, *args, **kwargs):
            return None

    sys.modules["observability"] = types.SimpleNamespace(
        capture_current_context=lambda: {},
        configure_tracing=lambda *args, **kwargs: None,
        current_trace_id=lambda: None,
        ensure_slo_schema=lambda *args, **kwargs: None,
        inject_context=lambda *args, **kwargs: None,
        record_slo_event=lambda *args, **kwargs: None,
        render_slo_prometheus=lambda *args, **kwargs: [],
        set_span_error=lambda *args, **kwargs: None,
        start_span=lambda *args, **kwargs: _Span(),
        summarize_slos=lambda *args, **kwargs: {},
    )

if "opentelemetry.trace" not in sys.modules:
    trace_module = types.SimpleNamespace(
        SpanKind=types.SimpleNamespace(CLIENT="client", INTERNAL="internal")
    )
    sys.modules["opentelemetry.trace"] = trace_module
    sys.modules.setdefault("opentelemetry", types.SimpleNamespace(trace=trace_module))

if "warehouse_exports" not in sys.modules:

    class _ArchiveManager:
        @classmethod
        def from_env(cls):
            return cls()

    sys.modules["warehouse_exports"] = types.SimpleNamespace(
        ArchiveManager=_ArchiveManager,
        WarehouseExportService=lambda *_args, **_kwargs: types.SimpleNamespace(
            export=lambda *_a, **_k: {}
        ),
    )

from funding_bot import (
    ConnectorBatchRequest,
    ConnectorBatchScheduler,
    FundingBot,
    _BasePortalConnector,
)


class AsyncTestConnector(_BasePortalConnector):
    connector_slug = "async-test"
    source_name = "Async Test"
    base_url = "https://async.example.org"

    def __init__(self, *args, **kwargs):
        self.calls = []
        super().__init__(*args, **kwargs)

    def _demo_data(self):
        return []


class AsyncBatchingTests(unittest.TestCase):
    def setUp(self):
        FundingBot.reset_batch_metrics()
        FundingBot.reset_connector_metrics()

    def test_batch_scheduler_coalesces_duplicate_requests(self):
        async def fake_http_client(_url, payload, _credentials=None, session=None):
            connector.calls.append({"page": payload["page"], "session": session is not None})
            return {
                "schema_version": 2,
                "results": [
                    {
                        "source": "Async Test",
                        "donor_name": "Demo Donor",
                        "title": "Async Education Grant",
                        "portal_url": "https://async.example.org/opportunity",
                        "summary": "Education support",
                        "category": "Education",
                        "tags": ["education"],
                    }
                ],
                "has_more": False,
            }

        connector = AsyncTestConnector(async_http_client=fake_http_client, transport="http")
        scheduler = ConnectorBatchScheduler(batch_size=5)

        async def run_test():
            request = ConnectorBatchRequest(connector=connector, keywords=("education",))
            results = await scheduler.submit_many([request, request])
            self.assertEqual(2, len(results))
            self.assertEqual("Async Education Grant", results[0]["opportunities"][0]["title"])
            self.assertEqual(results[0], results[1])

        asyncio.run(run_test())

        self.assertEqual(1, len(connector.calls))
        metrics = FundingBot.batch_metrics_snapshot()
        self.assertEqual(2, metrics["scheduled_requests"])
        self.assertEqual(1, metrics["coalesced_requests"])
        self.assertEqual(1, metrics["batches_total"])

    def test_run_discovery_async_batches_and_persists_results(self):
        async def grants_client(_url, payload, _credentials=None, session=None):
            return {
                "schema_version": 2,
                "results": [
                    {
                        "source": "Async Grants",
                        "donor_name": "Grant Donor",
                        "title": f"Grant Result {payload['page']}",
                        "portal_url": "https://async.example.org/grants",
                        "summary": "Education grant",
                        "category": "Education",
                        "tags": ["education"],
                    }
                ],
                "has_more": False,
            }

        async def csr_client(_url, payload, _credentials=None, session=None):
            return {
                "schema_version": 2,
                "results": [
                    {
                        "source": "Async CSR",
                        "donor_name": "CSR Donor",
                        "title": f"CSR Result {payload['page']}",
                        "portal_url": "https://async.example.org/csr",
                        "summary": "Education CSR support",
                        "category": "Education",
                        "tags": ["education", "csr"],
                    }
                ],
                "has_more": False,
            }

        grants = AsyncTestConnector(
            async_http_client=grants_client,
            transport="http",
            source_name="Async Grants",
            base_url="https://async.example.org/grants-api",
        )
        csr = AsyncTestConnector(
            async_http_client=csr_client,
            transport="http",
            source_name="Async CSR",
            base_url="https://async.example.org/csr-api",
        )
        with mock.patch.object(FundingBot, "_apply_migrations", lambda self: None):
            bot = FundingBot(trusted_sources={"Async Grants", "Async CSR"})
        bot.connection.execute("""
            CREATE TABLE IF NOT EXISTS funnel_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stage TEXT NOT NULL,
                entity_key TEXT NOT NULL,
                connector_name TEXT,
                opportunity_signature TEXT,
                task_id INTEGER,
                communication_id INTEGER,
                event_type TEXT,
                success INTEGER NOT NULL DEFAULT 1,
                happened_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """)
        bot.connection.execute("""
            CREATE TABLE IF NOT EXISTS connector_call_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                connector_name TEXT NOT NULL,
                connector_type TEXT NOT NULL,
                operation TEXT NOT NULL,
                source_status TEXT NOT NULL,
                latency_seconds REAL NOT NULL,
                cost_usd REAL NOT NULL DEFAULT 0,
                errored INTEGER NOT NULL DEFAULT 0,
                request_count INTEGER NOT NULL DEFAULT 1,
                happened_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """)
        bot.connection.commit()
        try:
            found = asyncio.run(
                bot.run_discovery_async(
                    [grants, csr],
                    keywords=["education"],
                    batch_size=1,
                )
            )
            self.assertEqual(2, len(found))
            self.assertEqual(2, len(bot.list_opportunities()))
            metrics = FundingBot.batch_metrics_snapshot()
            self.assertEqual(2, metrics["scheduled_requests"])
            self.assertEqual(2, metrics["batches_total"])
            self.assertEqual(1, metrics["max_batch_size"])
            prometheus = "\n".join(FundingBot.render_batch_metrics_prometheus())
            self.assertIn("funding_bot_connector_batches_total 2", prometheus)
        finally:
            bot.close()
