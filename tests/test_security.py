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
    _default_http_json_client,
    create_connector,
)
from web.app import app  # noqa: E402


class ConnectorTLSSecurityTests(unittest.TestCase):
    def test_connector_rejects_insecure_base_url(self):
        with self.assertRaises(ConnectionSecurityError):
            create_connector("grants-portal", base_url="http://grants.example.org/opportunities")

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
        self.db_path = Path(".test_security_web.db")
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
