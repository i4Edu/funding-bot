import base64
import os
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("ADMIN_PASSWORD", "admin-secret")
os.environ.setdefault("STAFF_PASSWORD", "staff-secret")
os.environ.setdefault("AUDITOR_PASSWORD", "auditor-secret")

from web.app import app  # noqa: E402


def _auth_header(role: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{role}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


class SettingsPanelTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(".test_web_settings.db")
        if self.db_path.exists():
            self.db_path.unlink()
        os.environ["BOT_DB_PATH"] = str(self.db_path)
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.admin_headers = _auth_header("admin", "admin-secret")
        self.auditor_headers = _auth_header("auditor", "auditor-secret")

    def tearDown(self):
        if self.db_path.exists():
            self.db_path.unlink()
        os.environ.pop("BOT_DB_PATH", None)

    def test_settings_page_requires_authentication(self):
        response = self.client.get("/settings")
        self.assertEqual(401, response.status_code)

    def test_settings_page_renders_for_authenticated_role(self):
        response = self.client.get("/settings", headers=self.auditor_headers)
        self.assertEqual(200, response.status_code)
        self.assertIn(b"Settings", response.data)

    def test_update_organization_settings_requires_admin(self):
        response = self.client.post(
            "/settings/organization",
            json={"name": "i4Edu"},
            headers=self.auditor_headers,
        )
        self.assertEqual(403, response.status_code)

    def test_update_organization_settings_as_admin(self):
        response = self.client.post(
            "/settings/organization",
            json={"name": "i4Edu", "mission": "Educate"},
            headers=self.admin_headers,
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            {"name": "i4Edu", "mission": "Educate"},
            response.get_json()["organization_profile"],
        )

    def test_update_search_settings_accepts_comma_separated_strings(self):
        response = self.client.post(
            "/settings/search",
            json={"keywords": "education, csr", "trusted_sources": ""},
            headers=self.admin_headers,
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            ["csr", "education"],
            sorted(response.get_json()["search_settings"]["keywords"]),
        )

    def test_register_credential_and_list(self):
        response = self.client.post(
            "/settings/credentials",
            json={"alias": "smtp", "env_var_name": "SMTP_PASSWORD"},
            headers=self.admin_headers,
        )
        self.assertEqual(201, response.status_code)
        self.assertEqual(
            [{"alias": "smtp", "env_var_name": "SMTP_PASSWORD"}],
            response.get_json()["credentials"],
        )

    def test_run_discovery_now_returns_new_opportunities(self):
        response = self.client.post(
            "/settings/discover",
            json={"keywords": ["education"]},
            headers=self.admin_headers,
        )
        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertEqual(1, payload["count"])
        self.assertEqual("Education Innovation Grant", payload["new_opportunities"][0]["title"])

    def test_run_discovery_requires_admin(self):
        response = self.client.post("/settings/discover", json={}, headers=self.auditor_headers)
        self.assertEqual(403, response.status_code)

    def test_test_outreach_dry_run_composes_email_and_logs_it(self):
        response = self.client.post(
            "/settings/test-outreach",
            json={"email": "donor@example.org", "name": "Donor"},
            headers=self.admin_headers,
        )
        self.assertEqual(201, response.status_code)
        payload = response.get_json()
        self.assertTrue(payload["dry_run"])
        self.assertEqual("donor@example.org", payload["email"])

        audit_response = self.client.get("/audit-log", headers=self.admin_headers)
        actions = [entry["action"] for entry in audit_response.get_json()]
        self.assertIn("outreach_sent", actions)


if __name__ == "__main__":
    unittest.main()
