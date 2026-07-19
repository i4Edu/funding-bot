import os
import time
import unittest
import unittest.mock
from datetime import datetime, timezone

from jsonschema import validate

from funding_bot import (
    CSRNetworkConnector,
    DuplicateSubmissionError,
    FoundationDirectoryConnector,
    FundingBot,
    GlobalGivingConnector,
    GrantsPortalConnector,
    KickstarterForGoodConnector,
    NGODirectoryConnector,
)

CONNECTOR_OPPORTUNITY_SCHEMA = {
    "type": "object",
    "required": [
        "source",
        "donor_name",
        "title",
        "portal_url",
        "summary",
        "category",
        "tags",
    ],
    "properties": {
        "source": {"type": "string", "minLength": 1},
        "donor_name": {"type": "string", "minLength": 1},
        "title": {"type": "string", "minLength": 1},
        "portal_url": {"type": "string", "minLength": 1},
        "summary": {"type": "string"},
        "category": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": True,
}

CONNECTOR_RESULT_SCHEMA = {
    "type": "object",
    "required": ["schema_version", "opportunities", "metadata"],
    "properties": {
        "schema_version": {"type": "integer", "minimum": 2},
        "opportunities": {
            "type": "array",
            "items": CONNECTOR_OPPORTUNITY_SCHEMA,
        },
        "metadata": {
            "type": "object",
            "required": ["connector_name", "source_status"],
            "properties": {
                "connector_name": {"type": "string", "minLength": 1},
                "source_status": {"type": "string", "minLength": 1},
            },
            "additionalProperties": True,
        },
    },
    "additionalProperties": True,
}

DEGRADED_RESULT_SCHEMA = {
    "type": "object",
    "required": ["schema_version", "opportunities", "metadata"],
    "properties": {
        "schema_version": {"type": "integer", "minimum": 2},
        "opportunities": {
            "type": "array",
            "maxItems": 0,
        },
        "metadata": {
            "type": "object",
            "required": [
                "connector_name",
                "source_status",
                "degraded_reason",
            ],
            "properties": {
                "connector_name": {"type": "string", "minLength": 1},
                "source_status": {"const": "degraded"},
                "degraded_reason": {"type": "string", "minLength": 1},
                "last_error": {"type": ["string", "null"]},
                "retry_after_seconds": {"type": ["number", "null"]},
            },
            "additionalProperties": True,
        },
    },
    "additionalProperties": True,
}


class RecordingBrowserClient:
    def __init__(self, failures_before_success=0):
        self.failures_before_success = failures_before_success
        self.calls = []

    def submit(self, portal_url, credentials, form_data, attachments):
        self.calls.append(
            {
                "portal_url": portal_url,
                "credentials": dict(credentials),
                "form_data": dict(form_data),
                "attachments": list(attachments),
            }
        )
        if len(self.calls) <= self.failures_before_success:
            raise RuntimeError("mock browser timeout")
        return f"mock-ref-{len(self.calls)}"


class IntegrationWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.report_date = datetime(2026, 7, 19, 9, 0, tzinfo=timezone.utc)
        self.env_patch = unittest.mock.patch.dict(
            os.environ,
            {"PORTAL_CREDENTIALS": '{"username": "demo@example.org", "password": "secret"}'},
            clear=False,
        )
        self.env_patch.start()
        self.bot = FundingBot(
            db_path=":memory:",
            trusted_sources={"Grants Portal", "CSR Network"},
        )
        self.bot.store_organization_profile(
            {
                "name": "i4Edu",
                "mission": "Expand equitable learning access.",
                "summary_recipient": "ops@example.org",
            }
        )
        self.bot.store_search_settings(
            keywords=["education"],
            trusted_sources=["Grants Portal"],
        )
        self.bot.register_credential("grants-portal", "PORTAL_CREDENTIALS")

    def tearDown(self):
        self.bot.close()
        self.env_patch.stop()

    def _grants_payload(self, *, title="Education Innovation Grant"):
        return {
            "schema_version": 2,
            "opportunities": [
                {
                    "source": "Grants Portal",
                    "donor_name": "Global Education Fund",
                    "title": title,
                    "portal_url": "https://grants.example.org/opportunities/education-innovation",
                    "summary": "Supports nonprofit education pilots with strong local impact.",
                    "category": "Education",
                    "tags": ["education", "innovation"],
                }
            ],
        }

    def test_discover_apply_and_summary_pipeline_uses_mocked_dependencies(self):
        connector_calls = []

        def http_client(_url, payload, _credentials=None):
            connector_calls.append(dict(payload))
            return self._grants_payload()

        connector = GrantsPortalConnector(http_client=http_client, page_size=5, cache_ttl=60)
        browser = RecordingBrowserClient()
        deliveries = []

        started_at = time.perf_counter()
        found = self.bot.run_discovery([connector], discovered_at=self.report_date)
        cached_run_started_at = time.perf_counter()
        cached_found = self.bot.run_discovery([connector], discovered_at=self.report_date)
        cached_run_duration = time.perf_counter() - cached_run_started_at

        with unittest.mock.patch.object(self.bot, "_utcnow", return_value=self.report_date):
            application = self.bot.submit_application_via_browser(
                found[0]["signature"],
                credential_alias="grants-portal",
                browser_client=browser,
                form_data={"project_name": "Literacy Lab"},
                attachments=["proposal.pdf"],
            )
        summary = self.bot.send_daily_summary(
            sender=lambda to_addr, subject, body: deliveries.append(
                {"recipient": to_addr, "subject": subject, "body": body}
            ),
            report_date=self.report_date,
        )
        total_duration = time.perf_counter() - started_at

        self.assertEqual(1, len(found))
        self.assertEqual([], cached_found)
        self.assertEqual(1, len(connector_calls))
        self.assertIn("education", connector_calls[0]["keywords"])
        self.assertEqual("submitted", application["status"])
        self.assertEqual("Await donor review", application["next_action"])
        self.assertEqual(1, len(browser.calls))
        self.assertEqual("demo@example.org", browser.calls[0]["credentials"]["username"])
        self.assertEqual(["proposal.pdf"], browser.calls[0]["attachments"])
        self.assertEqual(1, len(deliveries))
        self.assertEqual("ops@example.org", deliveries[0]["recipient"])
        self.assertIn("- New Opportunities Found: 1", summary["body"])
        self.assertIn("- Applications Submitted: 1", summary["body"])
        self.assertIn("- Pending Applications: 1", summary["body"])
        self.assertIn("Education Innovation Grant", summary["body"])
        self.assertLess(total_duration, 2.0)
        self.assertLess(cached_run_duration, 0.5)
        audit_actions = [row["action"] for row in self.bot.list_audit_logs(limit=10)]
        self.assertIn("daily_summary_sent", audit_actions)
        self.assertIn("application_recorded", audit_actions)

    def test_pipeline_failure_records_pending_application_and_prevents_duplicates(self):
        connector = GrantsPortalConnector(
            http_client=lambda *_args, **_kwargs: self._grants_payload()
        )
        found = self.bot.run_discovery([connector], discovered_at=self.report_date)

        with unittest.mock.patch.object(self.bot, "_utcnow", return_value=self.report_date):
            application = self.bot.submit_application_via_browser(
                found[0]["signature"],
                credential_alias="grants-portal",
                browser_client=RecordingBrowserClient(failures_before_success=3),
                form_data={"project_name": "Literacy Lab"},
                attachments=["proposal.pdf"],
                max_retries=3,
            )

        self.assertEqual("pending", application["status"])
        self.assertIn("Retry failed browser submission", application["next_action"])
        attempts = self.bot.connection.execute(
            "SELECT COUNT(*) AS count FROM submission_attempts WHERE opportunity_signature = ?",
            (found[0]["signature"],),
        ).fetchone()
        self.assertEqual(3, attempts["count"])

        summary = self.bot.build_daily_summary(
            recipient="ops@example.org",
            report_date=self.report_date,
        )
        self.assertIn("Pending Applications: 1", summary["body"])
        self.assertIn("Retry failed browser submission", summary["body"])

        with self.assertRaises(DuplicateSubmissionError):
            self.bot.submit_application(
                found[0]["signature"],
                submission_reference="duplicate-ref",
                status="submitted",
                next_action="Should not be accepted",
            )

    def test_discovery_uses_cached_connector_results_when_remote_fetch_fails(self):
        seed_connector = GrantsPortalConnector(
            http_client=lambda *_args, **_kwargs: self._grants_payload(
                title="Cached Education Grant"
            ),
            page_size=5,
        )
        seeded = self.bot.run_discovery([seed_connector], discovered_at=self.report_date)
        self.assertEqual(1, len(seeded))

        def failing_http_client(*_args, **_kwargs):
            raise TimeoutError("connector timeout")

        fallback_connector = GrantsPortalConnector(http_client=failing_http_client, page_size=5)
        started_at = time.perf_counter()
        with unittest.mock.patch.dict(
            os.environ, {"PORTAL_FALLBACK_MODE": "cache-first"}, clear=False
        ):
            fallback_found = self.bot.run_discovery(
                [fallback_connector],
                discovered_at=self.report_date,
            )
        fallback_duration = time.perf_counter() - started_at

        self.assertEqual([], fallback_found)
        self.assertLess(fallback_duration, 1.0)
        cache_row = self.bot.connection.execute(
            """
            SELECT source_status, metadata_json
            FROM connector_result_cache
            WHERE connector_name = ?
            """,
            ("Grants Portal",),
        ).fetchone()
        self.assertEqual("cached", cache_row["source_status"])
        self.assertIn("fallback_mode", cache_row["metadata_json"])
        audit_actions = [row["action"] for row in self.bot.list_audit_logs(limit=10)]
        self.assertIn("connector_fallback_activated", audit_actions)


class ConnectorContractTests(unittest.TestCase):
    def test_all_portal_connectors_return_the_expected_response_schema(self):
        connector_cases = (
            ("grants-portal", GrantsPortalConnector(), ["education"]),
            ("csr-network", CSRNetworkConnector(), ["digital learning"]),
            ("ngo-directory", NGODirectoryConnector(), ["literacy"]),
            ("foundation-directory", FoundationDirectoryConnector(), ["foundation"]),
            ("globalgiving", GlobalGivingConnector(), ["stem"]),
            ("kickstarter-for-good", KickstarterForGoodConnector(), ["assistive tech"]),
        )

        for connector_name, connector, keywords in connector_cases:
            with self.subTest(connector=connector_name):
                started_at = time.perf_counter()
                result = connector.fetch_result(keywords)
                duration = time.perf_counter() - started_at

                validate(instance=result, schema=CONNECTOR_RESULT_SCHEMA)
                self.assertGreaterEqual(len(result["opportunities"]), 1)
                self.assertEqual(connector.source_name, result["metadata"]["connector_name"])
                self.assertIn(result["metadata"]["source_status"], {"demo", "remote"})
                self.assertLess(duration, 0.5)

    def test_degraded_connector_results_follow_the_expected_error_contract(self):
        connector_cases = (
            (
                "grants-portal",
                GrantsPortalConnector(
                    http_client=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                        TimeoutError("down")
                    ),
                    max_retries=0,
                ),
            ),
            (
                "csr-network",
                CSRNetworkConnector(
                    http_client=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                        ConnectionError("down")
                    ),
                    max_retries=0,
                ),
            ),
            (
                "ngo-directory",
                NGODirectoryConnector(
                    http_client=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("down")),
                    max_retries=0,
                ),
            ),
            (
                "foundation-directory",
                FoundationDirectoryConnector(
                    http_client=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                        TimeoutError("down")
                    ),
                    credentials={"api_key": "test-key"},
                    max_retries=0,
                ),
            ),
            (
                "globalgiving",
                GlobalGivingConnector(
                    http_client=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                        TimeoutError("down")
                    ),
                    max_retries=0,
                ),
            ),
            (
                "kickstarter-for-good",
                KickstarterForGoodConnector(
                    http_client=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                        TimeoutError("down")
                    ),
                    max_retries=0,
                ),
            ),
        )

        for connector_name, connector in connector_cases:
            with self.subTest(connector=connector_name):
                result = connector.fetch_result(["education"])
                validate(instance=result, schema=DEGRADED_RESULT_SCHEMA)
                self.assertEqual([], result["opportunities"])
                self.assertIn(
                    result["metadata"]["degraded_reason"],
                    {"connector_error", "circuit_open", "rate_limit_exceeded"},
                )


if __name__ == "__main__":
    unittest.main()
