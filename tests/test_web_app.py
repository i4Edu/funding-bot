import base64
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("ADMIN_PASSWORD", "admin-secret")
os.environ.setdefault("STAFF_PASSWORD", "staff-secret")
os.environ.setdefault("AUDITOR_PASSWORD", "auditor-secret")

from funding_bot import FundingBot  # noqa: E402
import web.app as web_app_module  # noqa: E402
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
        self.staff_headers = _auth_header("staff", "staff-secret")
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

    def test_upsert_donor_accepts_locale(self):
        response = self.client.post(
            "/donors",
            json={"email": "donor@example.org", "name": "Donor", "locale": "bn"},
            headers=self.admin_headers,
        )
        self.assertEqual(201, response.status_code)
        self.assertEqual("bn", response.get_json()["locale"])

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
            json={"email": "donor@example.org", "name": "Donor", "locale": "bn"},
            headers=self.admin_headers,
        )
        self.assertEqual(201, response.status_code)
        payload = response.get_json()
        self.assertTrue(payload["dry_run"])
        self.assertEqual("donor@example.org", payload["email"])
        self.assertIn("ধন্যবাদ", payload["subject"])

        audit_response = self.client.get("/audit-log", headers=self.admin_headers)
        actions = [entry["action"] for entry in audit_response.get_json()]
        self.assertIn("outreach_sent", actions)

    def test_dashboard_tasks_lists_only_current_users_tasks(self):
        bot = FundingBot(db_path=str(self.db_path))
        bot.create_task(title="Prepare proposal", assigned_to="staff", description="Draft narrative")
        bot.create_task(title="Check audit trail", assigned_to="auditor")
        bot.close()

        response = self.client.get("/dashboard/tasks", headers=self.staff_headers)

        self.assertEqual(200, response.status_code)
        self.assertIn(b"Prepare proposal", response.data)
        self.assertNotIn(b"Check audit trail", response.data)
        self.assertIn(b"Assigned", response.data)

    def test_task_status_transition_route_validates_workflow(self):
        bot = FundingBot(db_path=str(self.db_path))
        task = bot.create_task(title="Submit attachments", assigned_to="staff")
        bot.close()

        invalid = self.client.post(
            f"/tasks/{task['id']}/status",
            json={"status": "done"},
            headers=self.staff_headers,
        )
        self.assertEqual(400, invalid.status_code)
        self.assertIn("cannot transition", invalid.get_json()["error"])

        valid = self.client.post(
            f"/tasks/{task['id']}/status",
            json={"status": "in-progress"},
            headers=self.staff_headers,
        )
        self.assertEqual(200, valid.status_code)
        payload = valid.get_json()
        self.assertEqual("in-progress", payload["task"]["status"])
        self.assertIn("moved from todo to in-progress", payload["notification"])

    def test_metrics_include_task_counts(self):
        bot = FundingBot(db_path=str(self.db_path))
        bot.create_task(title="Prepare proposal", assigned_to="staff")
        bot.create_task(title="Review blocker", assigned_to="staff", status="blocked")
        bot.close()

        response = self.client.get("/metrics", headers=self.admin_headers)

        self.assertEqual(200, response.status_code)
        body = response.data.decode("utf-8")
        self.assertIn("funding_bot_tasks_total 2", body)
        self.assertIn('funding_bot_tasks_status_total{status="todo"} 1', body)
        self.assertIn('funding_bot_tasks_status_total{status="blocked"} 1', body)
        self.assertIn('funding_bot_tasks_assigned_total{assigned_to="staff"} 2', body)

    def test_queue_health_endpoint_reports_queue_metrics(self):
        queue_snapshot = {
            "status": "ok",
            "queue_name": "funding-bot",
            "broker_reachable": True,
            "timeout_seconds": 2.0,
            "active_tasks": 2,
            "pending_tasks": 4,
            "queue_depth": 4,
            "worker_count": 2,
            "workers": [
                {
                    "name": "worker-a",
                    "status": "online",
                    "active_tasks": 1,
                    "reserved_tasks": 2,
                    "scheduled_tasks": 0,
                },
                {
                    "name": "worker-b",
                    "status": "online",
                    "active_tasks": 1,
                    "reserved_tasks": 0,
                    "scheduled_tasks": 1,
                },
            ],
        }

        with mock.patch.object(web_app_module, "_get_queue_health_snapshot", return_value=queue_snapshot):
            response = self.client.get("/health/queue")

        self.assertEqual(200, response.status_code)
        self.assertEqual(queue_snapshot, response.get_json())

    def test_queue_health_endpoint_returns_503_for_unreachable_broker(self):
        queue_snapshot = {
            "status": "degraded",
            "queue_name": "celery",
            "broker_reachable": False,
            "timeout_seconds": 2.0,
            "active_tasks": 0,
            "pending_tasks": 0,
            "queue_depth": 0,
            "worker_count": 0,
            "workers": [],
            "error": "Timed out while contacting the Celery broker: broker timed out",
        }

        with mock.patch.object(web_app_module, "_get_queue_health_snapshot", return_value=queue_snapshot):
            response = self.client.get("/health/queue")

        self.assertEqual(503, response.status_code)
        self.assertEqual("degraded", response.get_json()["status"])
        self.assertIn("Timed out", response.get_json()["error"])

    def test_queue_health_snapshot_handles_timeout_errors(self):
        os.environ["CELERY_BROKER_URL"] = "redis://broker.example:6379/0"
        try:
            with mock.patch.object(
                web_app_module,
                "_fetch_celery_queue_snapshot",
                side_effect=TimeoutError("broker timed out"),
            ):
                snapshot = web_app_module._get_queue_health_snapshot()
        finally:
            os.environ.pop("CELERY_BROKER_URL", None)

        self.assertEqual("degraded", snapshot["status"])
        self.assertFalse(snapshot["broker_reachable"])
        self.assertEqual(0, snapshot["queue_depth"])
        self.assertIn("Timed out while contacting the Celery broker", snapshot["error"])

    def test_metrics_include_queue_depth_metrics(self):
        queue_snapshot = {
            "status": "ok",
            "queue_name": "funding-bot",
            "broker_reachable": True,
            "timeout_seconds": 2.0,
            "active_tasks": 3,
            "pending_tasks": 5,
            "queue_depth": 5,
            "worker_count": 2,
            "workers": [],
        }

        with mock.patch.object(web_app_module, "_get_queue_health_snapshot", return_value=queue_snapshot):
            response = self.client.get("/metrics", headers=self.admin_headers)

        self.assertEqual(200, response.status_code)
        body = response.data.decode("utf-8")
        self.assertIn("funding_bot_queue_health_status 1", body)
        self.assertIn("funding_bot_queue_broker_up 1", body)
        self.assertIn("funding_bot_queue_active_tasks 3", body)
        self.assertIn("funding_bot_queue_pending_tasks 5", body)
        self.assertIn("funding_bot_queue_depth 5", body)
        self.assertIn("funding_bot_queue_workers 2", body)


if __name__ == "__main__":
    unittest.main()
