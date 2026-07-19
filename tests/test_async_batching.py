import asyncio
import unittest

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
        bot = FundingBot(trusted_sources={"Async Grants", "Async CSR"})
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
