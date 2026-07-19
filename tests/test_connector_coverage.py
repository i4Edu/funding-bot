import json
import os
import unittest
from unittest import mock

from funding_bot import (
    CSRNetworkConnector,
    ConnectionSecurityError,
    ConnectorConfigError,
    ConnectorRegistry,
    CrowdfundingConnector,
    GlobalGivingConnector,
    GrantsPortalConnector,
    KickstarterForGoodConnector,
    NGODirectoryConnector,
    TokenBucketRateLimiter,
    _BasePortalConnector,
    _default_http_json_client,
    create_connector,
    default_connectors,
)


class FakeClock:
    def __init__(self):
        self.current = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.current

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.current += seconds


class BrokenValidationConnector(GrantsPortalConnector):
    def fetch_result(self, keywords):
        raise RuntimeError("validation failed")


class MinimalConnector(_BasePortalConnector):
    connector_slug = "minimal"
    source_name = "Minimal Connector"
    base_url = "https://minimal.example.org"

    def _demo_data(self):
        return [
            {
                "source": self.source_name,
                "donor_name": "Demo Donor",
                "title": "Minimal Education Fund",
                "portal_url": "https://minimal.example.org/opportunity",
                "summary": "Education support",
                "category": "Education",
                "tags": ["education"],
            }
        ]


class ConnectorCoverageTests(unittest.TestCase):
    def test_demo_connectors_expand_keywords_and_return_expected_records(self):
        self.assertEqual(
            "Education Innovation Grant",
            GrantsPortalConnector().fetch_opportunities(["learning"])[0]["title"],
        )
        self.assertEqual(
            "CSR Digital Learning Fund",
            CSRNetworkConnector().fetch_opportunities(["edtech"])[0]["title"],
        )
        self.assertEqual(
            "Community Literacy Matching Grant",
            NGODirectoryConnector().fetch_opportunities(["community engagement"])[0]["title"],
        )
        self.assertEqual(
            "Community STEM Lab Campaign",
            GlobalGivingConnector().fetch_opportunities(["stem"])[0]["title"],
        )
        self.assertEqual(
            "Assistive Tech Makerspace Project",
            KickstarterForGoodConnector().fetch_opportunities(["assistive tech"])[0]["title"],
        )

    def test_fetch_result_uses_cache_and_supports_manual_invalidation(self):
        calls = []

        def fake_http_client(_url, payload, _credentials=None):
            calls.append(dict(payload))
            return {
                "schema_version": 2,
                "results": [
                    {
                        "source": "Grants Portal",
                        "donor_name": "Cache Donor",
                        "title": "Cached Education Grant",
                        "portal_url": "https://example.org/cache",
                        "summary": "Education funding",
                        "category": "Education",
                        "tags": ["education"],
                    }
                ],
            }

        connector = GrantsPortalConnector(http_client=fake_http_client, page_size=3)

        first = connector.fetch_result(["education"])
        second = connector.fetch_result(["education"])
        connector.invalidate_cache(["education"])
        third = connector.fetch_result(["education"])
        connector.invalidate_cache()
        metrics = connector.cache_metrics()

        self.assertEqual(1, len(first["opportunities"]))
        self.assertEqual(first["opportunities"], second["opportunities"])
        self.assertGreater(third["metadata"]["keyword_count"], 1)
        self.assertEqual(2, len(calls))
        self.assertEqual(1, metrics["hits"])
        self.assertEqual(2, metrics["misses"])
        self.assertEqual(0, metrics["size"])

    def test_connector_resolves_environment_defaults_and_invalid_values(self):
        with mock.patch.dict(
            os.environ,
            {
                "GRANTS_PORTAL_PAGE_SIZE": "invalid",
                "PORTAL_CACHE_TTL": "-1",
                "GRANTS_PORTAL_RATE_LIMIT_CAPACITY": "0",
                "GRANTS_PORTAL_RATE_LIMIT_REFILL_RATE": "-1",
            },
            clear=False,
        ):
            connector = GrantsPortalConnector()

        self.assertEqual(100, connector.page_size)
        self.assertEqual(300.0, connector.cache_metrics()["ttl_seconds"])
        self.assertEqual(5.0, connector.rate_limit_config["capacity"])
        self.assertEqual(1.0, connector.rate_limit_config["refill_rate"])

    def test_fetch_opportunities_gracefully_degrades_on_remote_error(self):
        connector = GrantsPortalConnector(
            http_client=lambda _url, _payload, _credentials=None: (_ for _ in ()).throw(
                ConnectionError("connector offline")
            ),
            max_retries=0,
        )

        result = connector.fetch_result(["education"])

        self.assertEqual([], connector.fetch_opportunities(["education"]))
        self.assertEqual("degraded", result["metadata"]["source_status"])
        self.assertEqual("connector_error", result["metadata"]["degraded_reason"])
        self.assertEqual("connector offline", result["metadata"]["last_error"])

    def test_validate_connectivity_reports_ok_degraded_and_error_states(self):
        ok = create_connector("csr-network").validate_connectivity(["edtech"])

        degraded_connector = GrantsPortalConnector(
            http_client=lambda _url, _payload, _credentials=None: (_ for _ in ()).throw(
                TimeoutError("temporary timeout")
            ),
            max_retries=0,
        )
        degraded = degraded_connector.validate_connectivity(["education"])
        failed = BrokenValidationConnector().validate_connectivity(["education"])

        self.assertEqual("ok", ok["status"])
        self.assertIn("corporate partnerships", ok["expanded_keywords"])
        self.assertEqual("degraded", degraded["status"])
        self.assertFalse(degraded["connectivity_validated"])
        self.assertEqual("error", failed["status"])
        self.assertEqual("validation failed", failed["error"])

    def test_parse_remote_page_handles_supported_payload_shapes(self):
        connector = GrantsPortalConnector(page_size=2)

        opportunities_payload = connector._parse_remote_page(
            {
                "opportunities": [{"title": "A"}],
                "schema_version": 2,
                "next_page": 4,
            },
            current_page=1,
        )
        items_payload = connector._parse_remote_page(
            {"items": [{"title": "B"}, {"title": "C"}], "total_pages": "2"},
            current_page=1,
        )
        has_more_payload = connector._parse_remote_page(
            {"results": [{"title": "D"}], "has_more": False},
            current_page=2,
        )
        list_payload = connector._parse_remote_page([{"title": "E"}, {"title": "F"}], current_page=1)

        self.assertEqual(4, opportunities_payload[3])
        self.assertEqual(2, items_payload[3])
        self.assertIsNone(has_more_payload[3])
        self.assertEqual(2, list_payload[3])

    def test_invoke_http_get_client_supports_multiple_custom_signatures(self):
        connector = GrantsPortalConnector(http_client=lambda url, params, headers=None: {"ok": headers})
        payload = connector._invoke_http_get_client(
            "https://example.org/search",
            {"keywords": ["education"]},
            headers={"X-Test": "1"},
        )
        self.assertEqual({"X-Test": "1"}, payload["ok"])

        connector = GrantsPortalConnector(
            http_client=lambda url, params, credentials: {"credentials": credentials}
        )
        payload = connector._invoke_http_get_client(
            "https://example.org/search",
            {"keywords": ["education"]},
        )
        self.assertEqual({}, payload["credentials"])

    def test_invoke_http_get_client_uses_urllib_when_no_http_client_is_supplied(self):
        response = mock.MagicMock()
        response.read.return_value = b'{"ok": true}'
        response.__enter__.return_value = response

        with mock.patch("funding_bot.urllib.request.urlopen", return_value=response) as urlopen:
            connector = GrantsPortalConnector()
            payload = connector._invoke_http_get_client(
                "https://example.org/search",
                {"keywords": ["education"], "page": 1},
            )

        request = urlopen.call_args.args[0]
        self.assertIn("keywords=education", request.full_url)
        self.assertEqual("GET", request.get_method())
        self.assertEqual({"ok": True}, payload)

    def test_schema_detection_and_migration_normalize_legacy_rows(self):
        connector = CSRNetworkConnector()
        legacy_rows = [
            {
                "funder": "Legacy Donor",
                "title": "Legacy CSR Grant",
                "link": "https://csr.example.org/legacy",
                "description": "Legacy payload",
                "type": "Corporate Partnerships",
                "topics": ["csr", "education"],
            }
        ]

        self.assertEqual(1, connector.detect_schema_version(legacy_rows))
        self.assertEqual(2, connector.detect_schema_version(legacy_rows, declared_version="2"))
        self.assertEqual(2, connector.detect_schema_version({"portal_url": "https://example.org"}))

        migrated = connector.migrate_result_payload(legacy_rows, 1)
        normalized = connector._normalize_current_record({"title": "Tags String", "tags": "a, b"})

        self.assertEqual("Legacy Donor", migrated[0]["donor_name"])
        self.assertEqual("https://csr.example.org/legacy", migrated[0]["portal_url"])
        self.assertEqual(["a", "b"], normalized["tags"])

    def test_call_with_retry_tracks_backoff_and_failure_metrics(self):
        clock = FakeClock()
        attempts = []

        def flaky_client(_url, _payload, _credentials=None):
            attempts.append(1)
            if len(attempts) < 3:
                raise TimeoutError("slow upstream")
            return {
                "opportunities": [
                    {
                        "source": "Grants Portal",
                        "donor_name": "Recovered",
                        "title": "Recovered Opportunity",
                        "portal_url": "https://grants.example.org/recovered",
                        "summary": "Recovered after retries",
                        "category": "Education",
                        "tags": ["education"],
                    }
                ]
            }

        connector = GrantsPortalConnector(
            http_client=flaky_client,
            max_retries=2,
            retry_backoff_base=1.0,
            retry_backoff_factor=2.0,
            sleep_func=clock.sleep,
            time_func=clock.monotonic,
        )

        rows = connector.fetch_opportunities(["education"])
        metrics = connector.get_failure_metrics()

        self.assertEqual("Recovered Opportunity", rows[0]["title"])
        self.assertEqual([1.0, 2.0], clock.sleeps)
        self.assertEqual(2, metrics["retry_attempts"])
        self.assertEqual(1, metrics["successful_requests"])
        self.assertEqual("closed", metrics["state"])

    def test_circuit_breaker_and_rate_limit_paths_are_reported(self):
        clock = FakeClock()
        connector = GrantsPortalConnector(
            http_client=lambda _url, _payload, _credentials=None: (_ for _ in ()).throw(
                ConnectionError("connector offline")
            ),
            max_retries=0,
            circuit_failure_threshold=1,
            circuit_recovery_timeout=5.0,
            sleep_func=clock.sleep,
            time_func=clock.monotonic,
        )

        first = connector.fetch_result(["education"])
        second = connector.fetch_result(["education"])
        health = connector.check_health()

        self.assertEqual("degraded", first["metadata"]["source_status"])
        self.assertEqual("circuit_open", second["metadata"]["degraded_reason"])
        self.assertEqual("open", health["state"])

        rate_clock = FakeClock()
        class AlwaysLimitedRateLimiter:
            def consume(self, tokens=1.0):
                return False, 1.5

            @property
            def available_tokens(self):
                return 0.0

        limited = CrowdfundingConnector(
            platform="globalgiving",
            transport="http",
            http_client=lambda _url, _payload, _credentials=None: {
                "projects": {"project": []}
            },
            time_func=rate_clock.monotonic,
            rate_limit_config={"capacity": 1.0, "refill_rate": 0.0},
            rate_limiter=AlwaysLimitedRateLimiter(),
        )
        rate_limited = limited.fetch_result(["education"])

        self.assertEqual("rate_limit_exceeded", rate_limited["metadata"]["degraded_reason"])
        self.assertIn("rate limit exceeded", rate_limited["metadata"]["last_error"])

    def test_token_bucket_rate_limiter_reports_retry_after_and_available_tokens(self):
        clock = FakeClock()
        limiter = TokenBucketRateLimiter(2, 0.5, time_func=clock.monotonic)

        self.assertEqual((True, 0.0), limiter.consume())
        self.assertEqual((True, 0.0), limiter.consume())
        self.assertEqual((False, 2.0), limiter.consume())
        clock.current = 2.0
        self.assertEqual((True, 0.0), limiter.consume())
        self.assertEqual(0.0, limiter.available_tokens)

    def test_crowdfunding_connector_extracts_and_normalizes_platform_rows(self):
        connector = CrowdfundingConnector(
            platform="globalgiving",
            transport="http",
            http_client=lambda _url, _payload, _credentials=None: {
                "projects": {
                    "project": [
                        {
                            "name": "Girls in STEM",
                            "owner_name": "i4Edu Partners",
                            "projectLink": "https://www.globalgiving.org/projects/girls-in-stem/",
                            "need": "Fund computer science classes.",
                            "themeName": "Education",
                            "country": "Bangladesh",
                        }
                    ]
                }
            },
        )

        rows = connector.fetch_opportunities(["stem"])
        extracted, keys = connector._extract_platform_rows(
            {"projects": {"project": [{"title": "A"}]}}
        )
        normalized_missing = connector._normalize_platform_row({})

        kickstarter = CrowdfundingConnector(platform="kickstarter")
        kickstarter_row = kickstarter._normalize_platform_row(
            {
                "title": "Assistive Tech Makerspace",
                "creator": {"name": "Creative Team"},
                "category": {"name": "Innovation"},
                "urls": {"web": {"project": "https://kickstarter.example/project"}},
                "blurb": "Inclusive tools",
            }
        )

        self.assertEqual("Girls in STEM", rows[0]["title"])
        self.assertEqual("i4Edu Partners", rows[0]["donor_name"])
        self.assertEqual([{"title": "A"}], extracted)
        self.assertEqual(["projects"], keys)
        self.assertIsNone(normalized_missing)
        self.assertEqual("Creative Team", kickstarter_row["donor_name"])
        self.assertEqual("https://kickstarter.example/project", kickstarter_row["portal_url"])

    def test_crowdfunding_connector_supports_response_lists_and_rejects_unknown_platform(self):
        connector = CrowdfundingConnector(
            platform="kickstarter",
            transport="http",
            http_client=lambda _url, _payload, _credentials=None: [
                {
                    "title": "Community Lab",
                    "creator": {"name": "Builder"},
                    "category_name": "Education",
                    "url": "https://kickstarter.example/community-lab",
                    "summary": "Community makerspace",
                }
            ],
        )

        rows = connector.fetch_opportunities(["community"])
        self.assertEqual("Community Lab", rows[0]["title"])

        with self.assertRaises(ValueError):
            CrowdfundingConnector(platform="unknown")

    def test_default_http_json_client_adds_headers_and_wraps_transport_errors(self):
        fake_response = mock.Mock()
        fake_response.json.return_value = {"ok": True}
        fake_response.raise_for_status.return_value = None
        fake_session = mock.MagicMock()
        fake_session.__enter__.return_value = fake_session
        fake_session.post.return_value = fake_response

        with mock.patch("funding_bot._build_tls_http_session", return_value=fake_session):
            payload = _default_http_json_client(
                "https://grants.example.org/opportunities",
                {"keywords": ["education"]},
                {"api_key": "secret"},
            )

        self.assertEqual({"ok": True}, payload)
        fake_session.post.assert_called_once()

        with self.assertRaises(ConnectionSecurityError):
            _default_http_json_client("http://grants.example.org/opportunities", {"keywords": []})

        broken_session = mock.MagicMock()
        broken_session.__enter__.return_value = broken_session
        broken_session.post.side_effect = Exception("boom")
        with mock.patch("funding_bot._build_tls_http_session", return_value=broken_session):
            with self.assertRaises(Exception):
                _default_http_json_client(
                    "https://grants.example.org/opportunities",
                    {"keywords": ["education"]},
                )

    def test_registry_validates_credentials_and_builds_enabled_connectors(self):
        registry = ConnectorRegistry()
        registry.register(
            "sandbox",
            MinimalConnector,
            credential_schema={
                "type": "object",
                "properties": {"api_key": {"type": "string", "minLength": 1}},
                "required": ["api_key"],
                "additionalProperties": False,
            },
        )

        self.assertEqual(["sandbox"], registry.discover())
        self.assertIsInstance(registry.create("sandbox"), MinimalConnector)

        with self.assertRaises(ConnectorConfigError):
            registry.create("missing")
        with self.assertRaises(ValueError):
            registry.register("  ", MinimalConnector)

        resolver = lambda alias: {"api_key": f"{alias}-token"}
        registry.validate_config(
            {
                "type": "sandbox",
                "base_url": "https://sandbox.example.org",
                "credential_alias": "demo",
            },
            credential_resolver=resolver,
        )

        with self.assertRaises(ConnectorConfigError):
            registry.validate_config({"type": "sandbox", "base_url": "http://sandbox.example.org"}, credential_resolver=resolver)
        with self.assertRaises(ConnectorConfigError):
            registry.validate_config({"type": "sandbox", "credentials": {}}, credential_resolver=resolver)

        connectors = registry.build_connectors(
            [
                {"type": "sandbox", "enabled": False},
                {
                    "type": "sandbox",
                    "credential_alias": "demo",
                    "base_url": "https://sandbox.example.org",
                    "settings": {"page_size": 7},
                },
            ],
            credential_resolver=resolver,
        )

        self.assertEqual(1, len(connectors))
        self.assertEqual(7, connectors[0].page_size)
        self.assertEqual("demo-token", connectors[0].credentials["api_key"])

    def test_builtin_connector_registry_helpers_return_expected_connectors(self):
        builtins = default_connectors()
        self.assertGreaterEqual(len(builtins), 5)
        self.assertTrue(
            {
                "grants-portal",
                "csr-network",
                "ngo-directory",
                "globalgiving",
                "kickstarter-for-good",
            }.issubset({connector.connector_slug for connector in builtins})
        )
        self.assertEqual("grants-portal", create_connector("grants-portal").connector_slug)

        with self.assertRaises(ConnectionSecurityError):
            create_connector("grants-portal", base_url="http://grants.example.org/opportunities")
        with self.assertRaises(Exception):
            create_connector("missing-connector")


if __name__ == "__main__":
    unittest.main()
