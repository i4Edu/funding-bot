import base64
import json
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

from funding_bot import FundingBot, TaskTransitionError  # noqa: E402
from web.app import app  # noqa: E402


def _auth_header(role: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{role}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


class CollaborationModelTests(unittest.TestCase):
    def setUp(self):
        self.bot = FundingBot(db_path=":memory:")

    def tearDown(self):
        self.bot.close()

    def test_list_tasks_filters_and_sorts_by_due_date(self):
        self.bot.create_task(
            title="Draft proposal",
            assigned_to="staff",
            due_date="2026-07-12",
        )
        self.bot.create_task(
            title="Collect letters",
            assigned_to="staff",
            due_date="2026-07-08",
        )
        self.bot.create_task(
            title="Audit evidence",
            assigned_to="auditor",
            due_date="2026-07-05",
        )

        tasks = self.bot.list_tasks(
            assigned_to="staff",
            due_date_before="2026-07-31",
            due_date_after="2026-07-01",
            sort="due_date",
        )

        self.assertEqual(["Collect letters", "Draft proposal"], [task["title"] for task in tasks])
        self.assertTrue(all(task["assigned_to"] == "staff" for task in tasks))

    def test_assign_task_updates_assignee_and_audit_log(self):
        task = self.bot.create_task(title="Prepare budget", assigned_to="staff")

        updated = self.bot.update_task_assignment(
            task["id"],
            assigned_to="auditor",
            changed_by="admin",
        )

        self.assertEqual("auditor", updated["assigned_to"])
        audit_entry = self.bot.list_audit_logs(limit=1)[0]
        self.assertEqual("task_assignment_changed", audit_entry["action"])
        details = json.loads(audit_entry["details_json"])
        self.assertEqual("staff", details["previous_assignee"])
        self.assertEqual("auditor", details["assigned_to"])

    def test_task_status_transitions_allow_valid_sequence_and_reject_closed_task_changes(self):
        task = self.bot.create_task(title="Review draft", assigned_to="staff")

        in_progress = self.bot.transition_task_status(
            task["id"], new_status="in-progress", changed_by="staff"
        )
        blocked = self.bot.transition_task_status(
            task["id"], new_status="blocked", changed_by="staff"
        )
        resumed = self.bot.transition_task_status(
            task["id"], new_status="in-progress", changed_by="staff"
        )
        done = self.bot.transition_task_status(
            task["id"], new_status="done", changed_by="staff"
        )

        self.assertEqual("in-progress", in_progress["status"])
        self.assertEqual("blocked", blocked["status"])
        self.assertEqual("in-progress", resumed["status"])
        self.assertEqual("done", done["status"])

        with self.assertRaises(TaskTransitionError):
            self.bot.transition_task_status(task["id"], new_status="todo", changed_by="staff")

    def test_sync_tasks_audits_assignment_and_field_changes(self):
        self.bot.create_task(
            title="Prepare budget",
            assigned_to="staff",
            external_id="ext-1",
            due_date="2026-07-10",
        )

        synced = self.bot.sync_tasks(
            [
                {
                    "external_id": "ext-1",
                    "title": "Prepare revised budget",
                    "assigned_to": "auditor",
                    "status": "in-progress",
                    "due_date": "2026-07-12",
                }
            ]
        )

        self.assertEqual("auditor", synced[0]["assigned_to"])
        self.assertEqual("in-progress", synced[0]["status"])
        actions = [entry["action"] for entry in self.bot.list_audit_logs(limit=10)]
        self.assertIn("task_updated", actions)
        self.assertIn("task_assignment_changed", actions)
        self.assertIn("tasks_synced", actions)

    def test_import_tasks_from_csv_rolls_back_on_error(self):
        csv_text = "\n".join(
            [
                "external_id,title,assigned_to,status,due_date",
                "ext-1,Collect letters,staff,todo,2026-07-10",
                "ext-2,Bad status,auditor,not-a-status,2026-07-11",
            ]
        )

        with self.assertRaises(ValueError):
            self.bot.import_tasks_from_csv(csv_text)

        self.assertEqual([], self.bot.list_tasks())
        self.assertEqual([], self.bot.list_audit_logs(action="tasks_imported"))


class CollaborationApiTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(f".test_collaboration_{self._testMethodName}.db")
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

    def _seed_tasks(self) -> list[dict[str, object]]:
        bot = FundingBot(db_path=str(self.db_path))
        tasks = [
            bot.create_task(
                title="Collect attachments",
                assigned_to="staff",
                due_date="2026-07-06",
                description="Gather signed letters",
            ),
            bot.create_task(
                title="Draft budget",
                assigned_to="staff",
                due_date="2026-07-10",
                status="blocked",
            ),
            bot.create_task(
                title="Compliance review",
                assigned_to="auditor",
                due_date="2026-07-05",
            ),
        ]
        bot.close()
        return tasks

    def test_admin_can_create_get_and_reassign_task(self):
        created = self.client.post(
            "/tasks",
            json={
                "title": "Prepare kickoff notes",
                "assigned_to": "staff",
                "description": "Outline collaboration steps",
                "due_date": "2026-07-20",
            },
            headers=self.admin_headers,
        )
        self.assertEqual(201, created.status_code)
        task = created.get_json()["task"]
        self.assertEqual("staff", task["assigned_to"])
        self.assertEqual("2026-07-20", task["due_date"])

        fetched = self.client.get(f"/tasks/{task['id']}", headers=self.admin_headers)
        self.assertEqual(200, fetched.status_code)
        self.assertEqual(task["title"], fetched.get_json()["title"])

        reassigned = self.client.post(
            f"/tasks/{task['id']}/assign",
            json={
                "assigned_to": "auditor",
                "assignee_email": "auditor@example.org",
                "assignee_name": "Audit Lane",
            },
            headers=self.admin_headers,
        )
        self.assertEqual(200, reassigned.status_code)
        self.assertEqual("auditor", reassigned.get_json()["task"]["assigned_to"])

    def test_tasks_route_filters_and_sorts_for_admin(self):
        self._seed_tasks()

        response = self.client.get(
            "/tasks?status=todo&due_date_before=2026-07-07&sort=due_date",
            headers=self.admin_headers,
        )

        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertEqual(["Compliance review", "Collect attachments"], [task["title"] for task in payload])

    def test_staff_tasks_route_is_scoped_to_current_role(self):
        self._seed_tasks()

        allowed = self.client.get("/tasks?sort=due_date", headers=self.staff_headers)
        self.assertEqual(200, allowed.status_code)
        self.assertEqual(
            ["Collect attachments", "Draft budget"],
            [task["title"] for task in allowed.get_json()],
        )

        forbidden = self.client.get("/tasks?assignee=auditor", headers=self.staff_headers)
        self.assertEqual(403, forbidden.status_code)

    def test_status_transition_route_enforces_assignment_permissions(self):
        task = self._seed_tasks()[0]

        forbidden = self.client.post(
            f"/tasks/{task['id']}/status",
            json={"status": "in-progress"},
            headers=self.auditor_headers,
        )
        self.assertEqual(403, forbidden.status_code)

        valid = self.client.post(
            f"/tasks/{task['id']}/status",
            json={"status": "in-progress"},
            headers=self.staff_headers,
        )
        self.assertEqual(200, valid.status_code)
        self.assertEqual("in-progress", valid.get_json()["task"]["status"])

    def test_task_sync_and_export_routes_round_trip(self):
        sync_response = self.client.post(
            "/api/tasks/sync",
            json={
                "tasks": [
                    {
                        "external_id": "sync-1",
                        "title": "Import prior backlog",
                        "assigned_to": "staff",
                        "status": "todo",
                        "due_date": "2026-07-09",
                    },
                    {
                        "external_id": "sync-2",
                        "title": "Audit imported backlog",
                        "assigned_to": "auditor",
                        "status": "blocked",
                        "due_date": "2026-07-11",
                    },
                ]
            },
            headers=self.admin_headers,
        )

        self.assertEqual(200, sync_response.status_code)
        self.assertEqual(2, sync_response.get_json()["count"])

        export_response = self.client.get(
            "/api/tasks/export?sort=due_date",
            headers=self.auditor_headers,
        )
        self.assertEqual(200, export_response.status_code)
        payload = export_response.get_json()
        self.assertEqual(2, payload["count"])
        self.assertEqual(
            ["Import prior backlog", "Audit imported backlog"],
            [task["title"] for task in payload["tasks"]],
        )

    def test_csv_import_route_validates_and_rolls_back(self):
        invalid_csv = "\n".join(
            [
                "external_id,title,assigned_to,status,due_date",
                "csv-1,Import kickoff checklist,staff,todo,2026-07-10",
                "csv-2,Invalid row,auditor,nope,2026-07-11",
            ]
        )

        response = self.client.post(
            "/api/tasks/import",
            data=invalid_csv,
            headers={**self.admin_headers, "Content-Type": "text/csv"},
        )
        self.assertEqual(400, response.status_code)

        export_response = self.client.get("/api/tasks/export", headers=self.admin_headers)
        self.assertEqual(0, export_response.get_json()["count"])

    def test_csv_import_route_creates_tasks_and_audit_entries(self):
        valid_csv = "\n".join(
            [
                "external_id,title,description,assigned_to,status,due_date,source",
                "csv-1,Import kickoff checklist,Legacy onboarding,staff,todo,2026-07-10,csv_seed",
                "csv-2,Review imported work,Imported from spreadsheet,auditor,blocked,2026-07-12,csv_seed",
            ]
        )

        response = self.client.post(
            "/api/tasks/import",
            data=valid_csv,
            headers={**self.admin_headers, "Content-Type": "text/csv"},
        )
        self.assertEqual(201, response.status_code)
        self.assertEqual(2, response.get_json()["count"])

        audit_response = self.client.get("/audit-log", headers=self.admin_headers)
        actions = [entry["action"] for entry in audit_response.get_json()]
        self.assertIn("tasks_imported", actions)


if __name__ == "__main__":
    unittest.main()
