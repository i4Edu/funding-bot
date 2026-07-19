import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("ADMIN_PASSWORD", "admin-secret")
os.environ.setdefault("STAFF_PASSWORD", "staff-secret")
os.environ.setdefault("AUDITOR_PASSWORD", "auditor-secret")

from funding_bot import (  # noqa: E402
    ConnectionSecurityError,
    FundingBot,
    _default_http_json_client,
    _require_https_url,
    create_connector,
)
from web.app import app  # noqa: E402


class ConnectorTLSSecurityTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("FUNDING_BOT_ALLOW_INSECURE_CONNECTOR_URLS", None)

    def test_connector_rejects_insecure_base_url(self):
        with self.assertRaises(ConnectionSecurityError):
            create_connector("grants-portal", base_url="http://grants.example.org/opportunities")

    def test_connector_allows_local_http_endpoint_only_when_dev_flag_is_enabled(self):
        with self.assertRaises(ConnectionSecurityError):
            _require_https_url("http://localhost:8080/grants-portal", purpose="Connector request")

        os.environ["FUNDING_BOT_ALLOW_INSECURE_CONNECTOR_URLS"] = "1"
        self.assertEqual(
            "http://localhost:8080/grants-portal",
            _require_https_url("http://localhost:8080/grants-portal", purpose="Connector request"),
        )

        with self.assertRaises(ConnectionSecurityError):
            _require_https_url("http://example.org/grants-portal", purpose="Connector request")

    def test_default_http_json_client_enforces_https_and_certificate_validation(self):
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
        fake_session.post.assert_called_once_with(
            "https://grants.example.org/opportunities",
            json={"keywords": ["education"]},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Connector-Api-Key": "secret",
            },
            timeout=10,
            verify=True,
        )

        with self.assertRaises(ConnectionSecurityError):
            _default_http_json_client("http://grants.example.org/opportunities", {"keywords": []})


class DashboardSessionSecurityTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(f".test_security_web_{self._testMethodName}.db")
        if self.db_path.exists():
            self.db_path.unlink()
        os.environ["BOT_DB_PATH"] = str(self.db_path)
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.auth_headers = {
            "Authorization": "Basic YXVkaXRvcjphdWRpdG9yLXNlY3JldA=="
        }

    def tearDown(self):
        if self.db_path.exists():
            self.db_path.unlink()
        os.environ.pop("BOT_DB_PATH", None)

    def test_dashboard_sets_secure_httponly_cookie(self):
        response = self.client.get(
            "/dashboard",
            headers=self.auth_headers,
            base_url="https://localhost",
        )

        self.assertEqual(200, response.status_code)
        session_cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("Secure", session_cookie)
        self.assertIn("HttpOnly", session_cookie)

    def test_dashboard_session_reuses_cookie_until_timeout(self):
        first_response = self.client.get(
            "/dashboard",
            headers=self.auth_headers,
            base_url="https://localhost",
        )
        second_response = self.client.get("/dashboard", base_url="https://localhost")

        self.assertEqual(200, first_response.status_code)
        self.assertEqual(200, second_response.status_code)

    def test_dashboard_session_expires_after_idle_timeout(self):
        original_lifetime = app.config["PERMANENT_SESSION_LIFETIME"]
        app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=30)
        try:
            response = self.client.get(
                "/dashboard",
                headers=self.auth_headers,
                base_url="https://localhost",
            )
            self.assertEqual(200, response.status_code)

            expired_at = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
            with self.client.session_transaction() as flask_session:
                flask_session["authenticated_role"] = "auditor"
                flask_session["authenticated_at"] = expired_at
                flask_session["last_seen_at"] = expired_at

            expired_response = self.client.get("/dashboard", base_url="https://localhost")
            self.assertEqual(401, expired_response.status_code)
        finally:
            app.config["PERMANENT_SESSION_LIFETIME"] = original_lifetime


class DashboardRateLimitAndCsrfTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(f".test_security_{self._testMethodName}.db")
        if self.db_path.exists():
            self.db_path.unlink()
        os.environ["BOT_DB_PATH"] = str(self.db_path)
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.admin_headers = {"Authorization": "Basic YWRtaW46YWRtaW4tc2VjcmV0"}
        self.auditor_headers = {"Authorization": "Basic YXVkaXRvcjphdWRpdG9yLXNlY3JldA=="}

    def tearDown(self):
        if self.db_path.exists():
            self.db_path.unlink()
        os.environ.pop("BOT_DB_PATH", None)

    def test_settings_page_exposes_csrf_token_in_header_and_forms(self):
        response = self.client.get(
            "/settings",
            headers=self.admin_headers,
            base_url="https://localhost",
        )

        self.assertEqual(200, response.status_code)
        self.assertTrue(response.headers.get("X-CSRF-Token"))
        self.assertIn(b'name="csrf_token"', response.data)

        translations = self.client.get(
            "/translations",
            headers=self.admin_headers,
            base_url="https://localhost",
        )
        self.assertEqual(200, translations.status_code)
        self.assertIn(b'name="csrf_token"', translations.data)

    def test_session_backed_form_posts_require_valid_csrf_token(self):
        page = self.client.get(
            "/settings",
            headers=self.admin_headers,
            base_url="https://localhost",
        )
        csrf_token = page.headers.get("X-CSRF-Token")

        missing = self.client.post(
            "/settings/organization",
            json={"name": "i4Edu"},
            base_url="https://localhost",
        )
        self.assertEqual(400, missing.status_code)
        self.assertIn("CSRF", missing.get_json()["error"])
        csrf_token = missing.get_json()["csrf_token"]

        valid = self.client.post(
            "/settings/organization",
            json={"name": "i4Edu"},
            headers={"X-CSRF-Token": csrf_token},
            base_url="https://localhost",
        )
        self.assertEqual(200, valid.status_code)
        self.assertEqual("i4Edu", valid.get_json()["organization_profile"]["name"])

        invalid = self.client.post(
            "/settings/organization",
            json={"name": "i4Edu"},
            headers={"X-CSRF-Token": "invalid-token"},
            base_url="https://localhost",
        )
        self.assertEqual(400, invalid.status_code)
        self.assertIn("csrf_token", invalid.get_json())

    def test_basic_auth_api_clients_can_post_without_csrf_token(self):
        response = self.client.post(
            "/settings/organization",
            json={"name": "Header Auth Only"},
            headers=self.admin_headers,
            base_url="https://localhost",
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("Header Auth Only", response.get_json()["organization_profile"]["name"])

    def test_export_responses_include_rate_limit_headers(self):
        response = self.client.get(
            "/api/tasks/export",
            headers=self.auditor_headers,
            environ_overrides={"REMOTE_ADDR": "198.51.100.20"},
        )

        self.assertEqual(200, response.status_code)
        self.assertIn("X-RateLimit-Limit", response.headers)
        self.assertIn("X-RateLimit-Remaining", response.headers)
        self.assertIn("X-RateLimit-Reset", response.headers)

    def test_export_rate_limit_returns_retry_metadata(self):
        original = app.config["RATE_LIMIT_EXPORT"]
        app.config["RATE_LIMIT_EXPORT"] = "2 per minute"
        try:
            for _ in range(2):
                response = self.client.get(
                    "/api/tasks/export",
                    headers=self.auditor_headers,
                    environ_overrides={"REMOTE_ADDR": "198.51.100.21"},
                )
                self.assertEqual(200, response.status_code)

            limited = self.client.get(
                "/api/tasks/export",
                headers=self.auditor_headers,
                environ_overrides={"REMOTE_ADDR": "198.51.100.21"},
            )
        finally:
            app.config["RATE_LIMIT_EXPORT"] = original

        self.assertEqual(429, limited.status_code)
        self.assertIn("Retry-After", limited.headers)
        payload = limited.get_json()
        self.assertIn("retry_after", payload)
        self.assertIn("reset_at", payload)

    def test_auth_routes_use_separate_rate_limit_policy(self):
        original = app.config["RATE_LIMIT_AUTH"]
        app.config["RATE_LIMIT_AUTH"] = "2 per minute"
        try:
            for _ in range(2):
                response = self.client.get(
                    "/dashboard",
                    headers=self.auditor_headers,
                    base_url="https://localhost",
                    environ_overrides={"REMOTE_ADDR": "198.51.100.22"},
                )
                self.assertEqual(200, response.status_code)

            limited = self.client.get(
                "/dashboard",
                headers=self.auditor_headers,
                base_url="https://localhost",
                environ_overrides={"REMOTE_ADDR": "198.51.100.22"},
            )
        finally:
            app.config["RATE_LIMIT_AUTH"] = original

        self.assertEqual(429, limited.status_code)

    def test_api_routes_use_general_rate_limit_policy(self):
        original = app.config["RATE_LIMIT_API"]
        app.config["RATE_LIMIT_API"] = "2 per minute"
        try:
            for _ in range(2):
                response = self.client.get(
                    "/tasks",
                    headers=self.auditor_headers,
                    environ_overrides={"REMOTE_ADDR": "198.51.100.23"},
                )
                self.assertEqual(200, response.status_code)

            limited = self.client.get(
                "/tasks",
                headers=self.auditor_headers,
                environ_overrides={"REMOTE_ADDR": "198.51.100.23"},
            )
        finally:
            app.config["RATE_LIMIT_API"] = original

        self.assertEqual(429, limited.status_code)

    def test_task_status_route_accepts_session_cookie_with_csrf_token(self):
        bot = FundingBot(db_path=str(self.db_path))
        task = bot.create_task(title="Prepare proposal", assigned_to="admin")
        bot.close()

        page = self.client.get(
            "/settings",
            headers=self.admin_headers,
            base_url="https://localhost",
        )
        csrf_token = page.headers.get("X-CSRF-Token")

        response = self.client.post(
            f"/tasks/{task['id']}/status",
            json={"status": "in-progress"},
            headers={"X-CSRF-Token": csrf_token},
            base_url="https://localhost",
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("in-progress", response.get_json()["task"]["status"])
