import json
import os
import urllib.error
import unittest
from unittest import mock

import requests

from funding_bot import (
    CSRNetworkConnector,
    CredentialNotFoundError,
    ConnectionSecurityError,
    ConnectorConfigError,
    ConnectorRegistry,
    CrowdfundingConnector,
    FoundationDirectoryConnector,
    FundingBotError,
    GlobalGivingConnector,
    GrantsPortalConnector,
    KickstarterForGoodConnector,
    NGODirectoryConnector,
    OAuth2ClientCredentialsVault,
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


class NoDemoConnector(_BasePortalConnector):
    connector_slug = "no-demo"
    source_name = "No Demo"
    base_url = "https://no-demo.example.org"


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

        connector = MinimalConnector(http_client=fake_http_client, transport="http", page_size=3)

        first = connector.fetch_result(["education"])
        second = connector.fetch_result(["education"])
        connector.invalidate_cache(["education"])
        third = connector.fetch_result(["education"])
        connector.invalidate_cache()
        metrics = connector.cache_metrics()

        self.assertEqual(1, len(first["opportunities"]))
        self.assertEqual(first["opportunities"], second["opportunities"])
        self.assertGreaterEqual(third["metadata"]["keyword_count"], 1)
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
            connector = GrantsPortalConnector(credential_name="")

        self.assertEqual(100, connector.page_size)
        self.assertEqual(300.0, connector.cache_metrics()["ttl_seconds"])
        self.assertEqual(5.0, connector.rate_limit_config["capacity"])
        self.assertEqual(1.0, connector.rate_limit_config["refill_rate"])

    def test_base_connector_helpers_cover_vault_and_request_session_branches(self):
        class StaticVault:
            def get_secret(self, name):
                return json.dumps({"token": f"{name}-token", "api_key": "vault-key"})

        oauth_vault = OAuth2ClientCredentialsVault(StaticVault())
        wrapped = MinimalConnector._wrap_credential_vault(oauth_vault)
        connector = MinimalConnector(
            credential_name="MINIMAL_SECRET",
            credential_vault=StaticVault(),
            credentials={"api_key": "inline-key"},
            request_session="session-object",
            request_timeout=0.1,
        )

        self.assertIs(wrapped, oauth_vault)
        self.assertEqual("session-object", connector._get_request_session())
        self.assertEqual(1.0, connector.request_timeout)
        self.assertEqual(
            {"api_key": "inline-key"},
            connector._get_resolved_credentials(),
        )

        with mock.patch("funding_bot.requests", None):
            with self.assertRaises(FundingBotError):
                MinimalConnector()._get_request_session()

    def test_base_connector_supports_empty_filters_and_missing_demo_implementation(self):
        connector = MinimalConnector()
        self.assertEqual(
            connector._demo_data(),
            connector._filter_opportunities(connector._demo_data(), None),
        )
        self.assertEqual(
            connector._demo_data(),
            connector.default_fallback_results(["education"]),
        )

        with self.assertRaises(NotImplementedError):
            NoDemoConnector()._demo_data()

    def test_fetch_opportunities_gracefully_degrades_on_remote_error(self):
        connector = MinimalConnector(
            http_client=lambda _url, _payload, _credentials=None: (_ for _ in ()).throw(
                ConnectionError("connector offline")
            ),
            transport="http",
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

    def test_fetch_remote_result_tracks_pagination_and_schema_migration(self):
        calls = []

        def fake_http_client(_url, payload, _credentials=None):
            calls.append(dict(payload))
            if payload["page"] == 1:
                return {
                    "items": [
                        {
                            "funder": "Legacy Donor",
                            "title": "Legacy Education Grant",
                            "link": "https://example.org/legacy",
                            "description": "Legacy payload",
                            "type": "Education",
                            "topics": ["education"],
                        }
                    ],
                    "schema_version": 1,
                    "total_pages": 2,
                }
            return {
                "items": [
                    {
                        "source": "Grants Portal",
                        "donor_name": "Current Donor",
                        "title": "Current Education Grant",
                        "portal_url": "https://example.org/current",
                        "summary": "Current payload",
                        "category": "Education",
                        "tags": ["education"],
                    }
                ],
                "result_schema_version": 2,
                "next_page": None,
            }

        connector = MinimalConnector(
            http_client=fake_http_client,
            transport="http",
            page_size=2,
            max_retries=0,
        )
        result = connector._fetch_remote_result(["education"])

        self.assertEqual(2, len(result["opportunities"]))
        self.assertEqual(2, result["metadata"]["detected_schema_version"])
        self.assertEqual(2, result["metadata"]["pages_fetched"])
        self.assertEqual([1, 2], [call["page"] for call in calls])

    def test_invoke_http_get_client_supports_multiple_custom_signatures(self):
        connector = GrantsPortalConnector(
            credential_name="",
            http_client=lambda url, params, headers=None: {"ok": headers},
        )
        payload = connector._invoke_http_get_client(
            "https://example.org/search",
            {"keywords": ["education"]},
            headers={"X-Test": "1"},
        )
        self.assertEqual({"X-Test": "1"}, payload["ok"])

        connector = GrantsPortalConnector(
            credential_name="",
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
            connector = GrantsPortalConnector(credential_name="")
            payload = connector._invoke_http_get_client(
                "https://example.org/search",
                {"keywords": ["education"], "page": 1},
            )

        request = urlopen.call_args.args[0]
        self.assertIn("keywords=education", request.full_url)
        self.assertEqual("GET", request.get_method())
        self.assertEqual({"ok": True}, payload)

    def test_fetch_remote_json_handles_http_429_and_infinite_rate_limits(self):
        clock = FakeClock()
        response_headers = {"Retry-After": "2.5"}
        error = urllib.error.HTTPError(
            "https://example.org/search",
            429,
            "too many requests",
            response_headers,
            None,
        )
        connector = GrantsPortalConnector(max_retries=0, sleep_func=clock.sleep)
        with mock.patch.object(connector, "_invoke_http_get_client", side_effect=error):
            with self.assertRaises(ConnectionError):
                connector._fetch_remote_json("https://example.org/search", {"q": "education"})

        self.assertEqual([2.5], clock.sleeps)
        self.assertEqual(1, connector.get_failure_metrics()["rate_limited_requests"])

        class NeverRecoversRateLimiter:
            def consume(self, tokens=1.0):
                return False, float("inf")

            @property
            def available_tokens(self):
                return 0.0

        limited_connector = GrantsPortalConnector(rate_limiter=NeverRecoversRateLimiter())
        with self.assertRaises(Exception):
            limited_connector._throttle_remote_request()

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

    def test_live_grants_portal_connector_formats_request_and_response(self):
        connector = GrantsPortalConnector(
            credentials={
                "authorization_header": "******",
                "api_key": "demo-key",
                "sort_by": "openDate",
                "agencies": ["DOE", "USAID"],
                "funding_categories": ["Education", "Youth"],
                "start_record_num": 10,
            },
            transport="http",
            request_session="session",
        )
        with mock.patch("funding_bot._perform_json_request") as request:
            request.return_value = {
                "data": {
                    "hitCount": 1,
                    "oppHits": [
                        {
                            "id": "123",
                            "number": "DOE-42",
                            "agency": "Department of Education",
                            "agencyCode": "DOE",
                            "title": "Student Success Grant",
                            "openDate": "2026-01-01",
                            "closeDate": "2026-02-01",
                            "oppStatus": "posted",
                            "docType": "Education",
                            "cfdaList": ["84.123"],
                        }
                    ],
                }
            }

            result = connector._fetch_remote_result(["education"])

        self.assertEqual("POST", request.call_args.args[0])
        self.assertEqual("https://api.grants.gov/v1/api/search2", request.call_args.args[1])
        self.assertEqual("session", request.call_args.kwargs["session"])
        self.assertEqual("******", request.call_args.kwargs["headers"]["Authorization"])
        self.assertEqual("demo-key", request.call_args.kwargs["headers"]["X-API-Key"])
        self.assertEqual("DOE|USAID", request.call_args.kwargs["json_payload"]["agencies"])
        self.assertEqual("Education|Youth", request.call_args.kwargs["json_payload"]["fundingCategories"])
        self.assertEqual("Student Success Grant", result["opportunities"][0]["title"])
        self.assertEqual("grants.gov", result["metadata"]["provider"])
        self.assertTrue(result["metadata"]["auth_applied"])

    def test_live_csr_connector_requires_credentials_and_normalizes_payload(self):
        with self.assertRaises(CredentialNotFoundError):
            CSRNetworkConnector(transport="http")._fetch_remote_result(["csr"])

        connector = CSRNetworkConnector(
            credentials={"subscriptionKey": "sub-key"},
            transport="http",
            request_session="session",
        )
        with mock.patch("funding_bot._perform_json_request") as request:
            request.return_value = {
                "items": [
                    {
                        "funder": {"name": "Corporate Fund", "url": "https://fund.example.org"},
                        "program_areas": ["Corporate Partnerships"],
                        "eligibility": ["Nonprofits"],
                        "tags": "education",
                        "description": "Description fallback",
                    }
                ]
            }

            result = connector._fetch_remote_result(["csr"])

        self.assertEqual("GET", request.mock_calls[0].args[0])
        self.assertEqual("sub-key", request.mock_calls[0].kwargs["headers"]["Subscription-Key"])
        self.assertEqual("Corporate Fund", result["opportunities"][0]["donor_name"])
        self.assertEqual("Untitled CSR opportunity", result["opportunities"][0]["title"])
        self.assertEqual("https://fund.example.org", result["opportunities"][0]["portal_url"])
        self.assertEqual("Description fallback", result["opportunities"][0]["summary"])
        self.assertEqual("Corporate Partnerships", result["opportunities"][0]["category"])

    def test_live_ngo_directory_connector_deduplicates_and_normalizes_results(self):
        connector = NGODirectoryConnector()
        responses = {
            ("literacy", 0): {
                "organizations": [
                    {
                        "name": "Readers United",
                        "ein": "12-3456789",
                        "city": "Boston",
                        "state": "MA",
                        "ntee_code": "B90",
                        "subseccd": "3",
                    }
                ],
                "total_results": 2,
                "per_page": 1,
            },
            ("literacy", 1): {
                "organizations": [
                    {
                        "name": "Readers United",
                        "ein": "123456789",
                        "city": "Boston",
                        "state": "MA",
                        "raw_ntee_code": "B91",
                        "sub_name": "Registered charity",
                    }
                ],
                "total_results": 2,
                "per_page": 1,
            },
            ("community engagement", 0): {
                "organizations": [
                    {
                        "organization_name": "Community Library",
                        "strein": "98-7654321",
                        "state": "CA",
                    }
                ],
                "total_results": 1,
                "per_page": 25,
            },
        }

        connector._fetch_remote_json = lambda _url, params: responses[(params["q"], params["page"])]
        result = connector._fetch_remote_result(["literacy", "community engagement"])

        self.assertEqual(2, len(result["opportunities"]))
        self.assertEqual(3, result["metadata"]["pages_fetched"])
        self.assertEqual("B90", result["opportunities"][0]["category"])
        self.assertIn("501(c)(3) organization", result["opportunities"][0]["summary"])
        self.assertIn("987654321", result["opportunities"][1]["portal_url"])

    def test_foundation_directory_connector_requires_api_key_and_normalizes_rows(self):
        class EmptyVault:
            def get_secret(self, name):
                return "{}"

        with self.assertRaises(ConnectorConfigError):
            FoundationDirectoryConnector(
                credential_name="MANUAL",
                credential_vault=EmptyVault(),
            )._fetch_remote_result(["foundation"])

        connector = FoundationDirectoryConnector(
            credential_name="MANUAL",
            credential_vault=EmptyVault(),
            credentials={"api_key": "fd-key"},
        )
        connector._fetch_remote_json = lambda _url, _params, headers=None: {
            "opportunities": [
                {
                    "grantmaker_name": "Heritage Foundation",
                    "recipient": {"name": "Rural Schools"},
                    "program_name": "Education Access",
                    "summary": "Support for classroom technology",
                    "links": {"self": "https://foundation.example.org/grant/1"},
                    "support_strategy": "Education",
                }
            ],
            "next_page": None,
        }

        result = connector._fetch_remote_result(["foundation"])

        self.assertEqual("Heritage Foundation", result["opportunities"][0]["donor_name"])
        self.assertEqual("Education Access", result["opportunities"][0]["title"])
        self.assertEqual("https://foundation.example.org/grant/1", result["opportunities"][0]["portal_url"])
        self.assertEqual("Education", result["opportunities"][0]["category"])

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

        connector = MinimalConnector(
            http_client=flaky_client,
            transport="http",
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
        connector = MinimalConnector(
            http_client=lambda _url, _payload, _credentials=None: (_ for _ in ()).throw(
                ConnectionError("connector offline")
            ),
            transport="http",
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
        broken_session.post.side_effect = requests.RequestException("boom")
        with mock.patch("funding_bot._build_tls_http_session", return_value=broken_session):
            with self.assertRaises(FundingBotError):
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
                "foundation-directory",
                "globalgiving",
                "kickstarter-for-good",
            }.issubset({connector.connector_slug for connector in builtins})
        )
        self.assertEqual("grants-portal", create_connector("grants-portal").connector_slug)
        self.assertEqual(
            "foundation-directory",
            create_connector("foundation-directory", credentials={"api_key": "demo"}).connector_slug,
        )

        with self.assertRaises(ConnectionSecurityError):
            create_connector("grants-portal", base_url="http://grants.example.org/opportunities")
        with self.assertRaises(Exception):
            create_connector("missing-connector")


if __name__ == "__main__":
    unittest.main()
