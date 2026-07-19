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


class CollaborationApiTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(".test_collaboration.db")
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


if __name__ == "__main__":
    unittest.main()
