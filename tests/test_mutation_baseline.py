from __future__ import annotations

import base64
import os
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

os.environ.setdefault("ADMIN_PASSWORD", "admin-secret")
os.environ.setdefault("STAFF_PASSWORD", "staff-secret")
os.environ.setdefault("AUDITOR_PASSWORD", "auditor-secret")
os.environ.setdefault("SESSION_COOKIE_SECURE", "0")

import task_queue  # noqa: E402
from funding_bot import FundingBot  # noqa: E402
from web.app import app  # noqa: E402


def _auth_header(role: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{role}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


class MutationBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = Path(
            f".test_mutation_baseline_{self._testMethodName}_{os.getpid()}_{uuid4().hex}.db"
        )
        if self.db_path.exists():
            self.db_path.unlink()
        os.environ["BOT_DB_PATH"] = str(self.db_path)
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.admin_headers = _auth_header("admin", "admin-secret")
        self.staff_headers = _auth_header("staff", "staff-secret")

    def tearDown(self) -> None:
        os.environ.pop("BOT_DB_PATH", None)
        os.environ.pop("ENABLE_TASK_QUEUE", None)
        if self.db_path.exists():
            self.db_path.unlink()

    def test_dashboard_requires_authentication(self) -> None:
        response = self.client.get("/dashboard")
        self.assertEqual(401, response.status_code)
        self.assertIn("WWW-Authenticate", response.headers)

    def test_settings_search_and_credential_routes_accept_admin_updates(self) -> None:
        settings_response = self.client.post(
            "/settings/search",
            json={"keywords": ["education"], "trusted_sources": ["CSR Network"]},
            headers=self.admin_headers,
        )
        self.assertEqual(200, settings_response.status_code)
        self.assertEqual(["education"], settings_response.get_json()["search_settings"]["keywords"])

        credential_response = self.client.post(
            "/settings/credentials",
            json={"alias": "smtp", "env_var_name": "SMTP_PASSWORD"},
            headers=self.admin_headers,
        )
        self.assertEqual(201, credential_response.status_code)
        self.assertEqual("smtp", credential_response.get_json()["credentials"][0]["alias"])

    def test_task_routes_cover_create_update_transition_and_export(self) -> None:
        created = self.client.post(
            "/tasks",
            json={
                "title": "Mutation baseline task",
                "assignee": "staff",
                "due_date": "2026-07-24",
                "description": "Seeded by mutation baseline.",
            },
            headers=self.admin_headers,
        )
        self.assertEqual(201, created.status_code)
        task_id = created.get_json()["task"]["id"]

        updated = self.client.put(
            f"/tasks/{task_id}",
            json={
                "title": "Mutation baseline task updated",
                "description": "Updated description",
                "assignee": "staff",
                "status": "todo",
                "due_date": "2026-07-25",
            },
            headers=self.admin_headers,
        )
        self.assertEqual(200, updated.status_code)

        transitioned = self.client.post(
            f"/tasks/{task_id}/status",
            json={"status": "in-progress"},
            headers=self.staff_headers,
        )
        self.assertEqual(200, transitioned.status_code)
        self.assertEqual("in-progress", transitioned.get_json()["task"]["status"])

        exported = self.client.get("/api/tasks/export", headers=self.admin_headers)
        self.assertEqual(200, exported.status_code)
        self.assertEqual(1, exported.get_json()["count"])

    def test_dispatch_discovery_uses_queue_when_enabled(self) -> None:
        os.environ["ENABLE_TASK_QUEUE"] = "1"

        class _Result:
            id = "job-123"

        with mock.patch.object(task_queue.discover_opportunities_task, "delay", return_value=_Result()):
            status_code, payload = task_queue.dispatch_discovery(
                keywords=["education"],
                trusted_sources=["CSR Network"],
                db_path=str(self.db_path),
            )

        self.assertEqual(202, status_code)
        self.assertEqual("hybrid", payload["mode"])
        self.assertEqual("job-123", payload["task_id"])

    def test_dispatch_discovery_runs_inline_when_queue_disabled(self) -> None:
        with mock.patch.object(
            task_queue,
            "_run_discovery_inline",
            return_value={"count": 2, "new_opportunities": [{"title": "Education Grant"}]},
        ):
            status_code, payload = task_queue.dispatch_discovery(
                keywords=["education"],
                trusted_sources=["CSR Network"],
                db_path=str(self.db_path),
            )

        self.assertEqual(200, status_code)
        self.assertEqual("cron", payload["mode"])
        self.assertEqual(2, payload["count"])

    def test_queue_status_reports_disabled_mode(self) -> None:
        status = task_queue.get_queue_status()
        self.assertFalse(status["queue_enabled"])
        self.assertEqual("cron", status["mode"])


if __name__ == "__main__":
    unittest.main()
