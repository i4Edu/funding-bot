import base64
import itertools
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from unittest.mock import patch

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
        self.db_path = Path(f".test_web_settings_{self._testMethodName}.db")
        self.output_dir = Path(f".test_policy_output_{self._testMethodName}")
        if self.db_path.exists():
            self.db_path.unlink()
        if self.output_dir.exists():
            for path in sorted(self.output_dir.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            self.output_dir.rmdir()
        os.environ["BOT_DB_PATH"] = str(self.db_path)
        os.environ["DATA_RESIDENCY"] = "EU"
        os.environ["DATA_STORAGE_REGION"] = "EU"
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.admin_headers = _auth_header("admin", "admin-secret")
        self.staff_headers = _auth_header("staff", "staff-secret")
        self.auditor_headers = _auth_header("auditor", "auditor-secret")

    def tearDown(self):
        if self.db_path.exists():
            self.db_path.unlink()
        if self.output_dir.exists():
            for path in sorted(self.output_dir.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            self.output_dir.rmdir()
        os.environ.pop("BOT_DB_PATH", None)
        os.environ.pop("DATA_RESIDENCY", None)
        os.environ.pop("DATA_STORAGE_REGION", None)
        os.environ.pop("ENABLE_TASK_QUEUE", None)
        os.environ.pop("ENABLE_LEGACY_CRON", None)
        os.environ.pop("ENABLE_TASK_QUEUE", None)
        os.environ.pop("CELERY_BROKER_URL", None)
        os.environ.pop("CELERY_QUEUE_NAME", None)
        os.environ.pop("CELERY_HEALTH_TIMEOUT_SECONDS", None)

    def test_settings_page_requires_authentication(self):
        response = self.client.get("/settings")
        self.assertEqual(401, response.status_code)

    def test_settings_page_renders_for_authenticated_role(self):
        response = self.client.get("/settings", headers=self.auditor_headers)
        self.assertEqual(200, response.status_code)
        self.assertIn(b"Settings", response.data)
        self.assertIn(b"Translations", response.data)

    def test_dashboard_auth_sets_secure_httponly_session_cookie(self):
        response = self.client.get("/dashboard", headers=self.auditor_headers)

        self.assertEqual(200, response.status_code)
        session_cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("Secure", session_cookie)
        self.assertIn("HttpOnly", session_cookie)

    def test_dashboard_session_allows_follow_up_request_without_auth_header(self):
        first_response = self.client.get("/dashboard", headers=self.auditor_headers)
        second_response = self.client.get("/dashboard")

        self.assertEqual(200, first_response.status_code)
        self.assertEqual(200, second_response.status_code)

    def test_dashboard_session_times_out_after_configured_lifetime(self):
        original_lifetime = app.config["PERMANENT_SESSION_LIFETIME"]
        app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=30)
        try:
            first_response = self.client.get("/dashboard", headers=self.auditor_headers)
            self.assertEqual(200, first_response.status_code)

            expired_at = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
            with self.client.session_transaction() as flask_session:
                flask_session["authenticated_role"] = "auditor"
                flask_session["authenticated_at"] = expired_at
                flask_session["last_seen_at"] = expired_at

            expired_response = self.client.get("/dashboard")
            self.assertEqual(401, expired_response.status_code)
        finally:
            app.config["PERMANENT_SESSION_LIFETIME"] = original_lifetime

    def test_dashboard_page_exposes_keyboard_shortcuts_and_focus_regions(self):
        html = (PROJECT_ROOT / "web" / "templates" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('aria-label="Open settings page"', html)
        self.assertIn('id="main-content" class="container py-4" tabindex="-1"', html)
        self.assertIn('id="recent-opps-region"', html)
        self.assertIn('Alt</kbd> + <kbd>Shift</kbd> + <kbd>O</kbd> — Focus recent opportunities', html)
        self.assertIn('KeyO: () => focusAndScroll(document.getElementById("recent-opps-region"))', html)

    def test_settings_page_includes_aria_labels_live_regions_and_shortcuts(self):
        html = (PROJECT_ROOT / "web" / "templates" / "settings.html").read_text(encoding="utf-8")
        self.assertIn('aria-label="Save organization profile"', html)
        self.assertIn('aria-label="Run donation discovery now"', html)
        self.assertIn('aria-keyshortcuts="Alt+Shift+R"', html)
        self.assertIn('role="status" aria-live="polite" aria-atomic="true" aria-label="Discovery results"', html)
        self.assertIn('role="status" aria-live="polite" aria-atomic="true" aria-label="Outreach results"', html)
        self.assertIn('Alt</kbd> + <kbd>Shift</kbd> + <kbd>T</kbd> — Focus donor outreach', html)

    def test_task_dashboard_page_includes_shortcut_help(self):
        html = (PROJECT_ROOT / "web" / "templates" / "tasks.html").read_text(encoding="utf-8")
        self.assertIn('aria-label="Open dashboard page"', html)
        self.assertIn('id="task-board-region"', html)
        self.assertIn('Alt</kbd> + <kbd>Shift</kbd> + <kbd>T</kbd> — Focus the task board', html)
        self.assertIn('aria-keyshortcuts="Enter Space ArrowLeft ArrowRight"', html)

    def test_settings_page_binds_keyboard_activation_for_action_buttons(self):
        html = (PROJECT_ROOT / "web" / "templates" / "settings.html").read_text(encoding="utf-8")
        self.assertIn('document.querySelectorAll("[data-keyboard-click]").forEach(bindKeyboardActivation);', html)
        self.assertIn('event.key === "Enter" || event.key === " "', html)
        self.assertIn('KeyR: () => document.getElementById("run-discovery").click()', html)

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
            json={"name": "i4Edu", "mission": "Educate", "privacy_jurisdictions": ["EU", "US"]},
            headers=self.admin_headers,
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            {"name": "i4Edu", "mission": "Educate", "privacy_jurisdictions": ["EU", "US"]},
            response.get_json()["organization_profile"],
        )

    def test_generate_privacy_policy_returns_versions_and_artifacts(self):
        self.client.post(
            "/settings/organization",
            json={
                "name": "i4Edu",
                "mission": "Educate",
                "privacy_email": "privacy@i4edu.example.org",
                "contact_email": "hello@i4edu.example.org",
                "privacy_jurisdictions": ["EU", "US"],
            },
            headers=self.admin_headers,
        )

        response = self.client.post(
            "/settings/privacy-policy",
            json={
                "jurisdictions": ["EU", "US"],
                "output_dir": str(self.output_dir),
                "effective_date": "2026-07-19",
            },
            headers=self.admin_headers,
        )

        self.assertEqual(201, response.status_code)
        payload = response.get_json()
        self.assertEqual("EU", payload["residency_status"]["data_residency"])
        self.assertEqual(2, len(payload["policies"]))
        self.assertEqual(2, len(payload["versions"]))
        first_policy = payload["policies"][0]
        self.assertTrue(Path(first_policy["html_path"]).exists())
        self.assertTrue(Path(first_policy["pdf_path"]).exists())

    def test_generate_privacy_policy_requires_admin(self):
        response = self.client.post(
            "/settings/privacy-policy",
            json={"jurisdictions": ["EU"], "output_dir": str(self.output_dir)},
            headers=self.auditor_headers,
        )
        self.assertEqual(403, response.status_code)

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
        with patch(
            "web.app.dispatch_discovery",
            return_value=(
                200,
                {
                    "mode": "cron",
                    "legacy_cron_enabled": True,
                    "count": 1,
                    "new_opportunities": [{"title": "Education Innovation Grant"}],
                },
            ),
        ):
            response = self.client.post(
                "/settings/discover",
                json={"keywords": ["education"]},
                headers=self.admin_headers,
            )
        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertEqual(1, payload["count"])
        self.assertEqual("cron", payload["mode"])
        self.assertTrue(payload["legacy_cron_enabled"])
        self.assertEqual("Education Innovation Grant", payload["new_opportunities"][0]["title"])

    def test_run_discovery_requires_admin(self):
        response = self.client.post("/settings/discover", json={}, headers=self.auditor_headers)
        self.assertEqual(403, response.status_code)

    def test_run_discovery_now_returns_task_metadata_in_queue_mode(self):
        os.environ["ENABLE_TASK_QUEUE"] = "1"
        os.environ["ENABLE_LEGACY_CRON"] = "1"

        with patch(
            "web.app.dispatch_discovery",
            return_value=(
                202,
                {
                    "mode": "hybrid",
                    "legacy_cron_enabled": True,
                    "task_name": "funding_bot.discover_opportunities",
                    "task_id": "job-123",
                },
            ),
        ):
            response = self.client.post(
                "/settings/discover",
                json={"keywords": ["education"]},
                headers=self.admin_headers,
            )

        self.assertEqual(202, response.status_code)
        payload = response.get_json()
        self.assertEqual("hybrid", payload["mode"])
        self.assertTrue(payload["legacy_cron_enabled"])
        self.assertEqual("funding_bot.discover_opportunities", payload["task_name"])
        self.assertTrue(payload["task_id"])

    def test_health_endpoint_includes_queue_mode(self):
        response = self.client.get("/health")

        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertEqual("ok", payload["status"])
        self.assertEqual("cron", payload["queue"]["mode"])
        self.assertFalse(payload["queue"]["queue_enabled"])

    def test_queue_health_endpoint_reports_disabled_queue_mode(self):
        response = self.client.get("/health/queue")

        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertEqual("disabled", payload["status"])
        self.assertEqual("cron", payload["mode"])
        self.assertEqual(0, payload["queue_depth"])

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

    def test_dashboard_tasks_renders_kanban_board_and_overdue_highlight(self):
        bot = FundingBot(db_path=str(self.db_path))
        bot.create_task(
            title="Prepare proposal",
            assigned_to="staff",
            description="Draft narrative",
            due_date="2026-07-01",
        )
        bot.create_task(title="Check audit trail", assigned_to="auditor")
        bot.close()

        response = self.client.get("/dashboard/tasks", headers=self.staff_headers)

        self.assertEqual(200, response.status_code)
        self.assertIn(b"Prepare proposal", response.data)
        self.assertNotIn(b"Check audit trail", response.data)
        self.assertIn(b"Task Board", response.data)
        self.assertIn(b"Todo", response.data)
        self.assertIn(b"In Progress", response.data)
        self.assertIn(b"Overdue", response.data)

    def _seed_task_filter_data(self):
        bot = FundingBot(db_path=str(self.db_path))
        tasks = [
            bot.create_task(title="Staff todo soon", assigned_to="staff", status="todo", due_date="2026-07-20"),
            bot.create_task(
                title="Staff in progress late",
                assigned_to="staff",
                status="in-progress",
                due_date="2026-07-25",
            ),
            bot.create_task(title="Admin todo mid", assigned_to="admin", status="todo", due_date="2026-07-22"),
            bot.create_task(
                title="Auditor done early",
                assigned_to="auditor",
                status="done",
                due_date="2026-07-18",
            ),
            bot.create_task(title="Admin blocked no date", assigned_to="admin", status="blocked"),
        ]
        bot.close()
        return tasks

    def test_tasks_api_supports_all_filter_combinations(self):
        tasks = self._seed_task_filter_data()
        filter_values = {
            "assignee": "staff",
            "status": "todo",
            "due_date_after": "2026-07-20",
            "due_date_before": "2026-07-22",
        }

        def matches(task, active_filters):
            due_date = task["due_date"][:10] if task["due_date"] else None
            return all(
                (
                    task["assigned_to"] == filter_values["assignee"]
                    if name == "assignee"
                    else task["status"] == filter_values["status"]
                    if name == "status"
                    else due_date is not None and due_date >= filter_values["due_date_after"]
                    if name == "due_date_after"
                    else due_date is not None and due_date <= filter_values["due_date_before"]
                )
                for name in active_filters
            )

        for size in range(1, len(filter_values) + 1):
            for active_filters in itertools.combinations(filter_values, size):
                query_string = {
                    name: filter_values[name]
                    for name in active_filters
                }
                query_string["sort"] = "due_date"
                response = self.client.get("/tasks", query_string=query_string, headers=self.admin_headers)
                self.assertEqual(200, response.status_code)
                expected_titles = [
                    task["title"]
                    for task in tasks
                    if matches(task, active_filters)
                ]
                self.assertEqual(
                    expected_titles,
                    [task["title"] for task in response.get_json()],
                    msg=f"Unexpected API results for filters {active_filters!r}",
                )

    def test_tasks_api_supports_assignee_status_and_due_date_sorting(self):
        self._seed_task_filter_data()
        expected_orders = {
            "assignee": [
                "Admin todo mid",
                "Admin blocked no date",
                "Auditor done early",
                "Staff todo soon",
                "Staff in progress late",
            ],
            "status": [
                "Admin blocked no date",
                "Auditor done early",
                "Staff in progress late",
                "Staff todo soon",
                "Admin todo mid",
            ],
            "due_date": [
                "Auditor done early",
                "Staff todo soon",
                "Admin todo mid",
                "Staff in progress late",
                "Admin blocked no date",
            ],
        }
        for sort_name, expected_titles in expected_orders.items():
            response = self.client.get("/tasks", query_string={"sort": sort_name}, headers=self.admin_headers)
            self.assertEqual(200, response.status_code)
            self.assertEqual(expected_titles, [task["title"] for task in response.get_json()])

    def test_dashboard_tasks_applies_filters_and_sorting(self):
        self._seed_task_filter_data()
        response = self.client.get(
            "/dashboard/tasks",
            query_string={
                "assignee": "admin",
                "status": "todo",
                "due_date_after": "2026-07-20",
                "due_date_before": "2026-07-22",
                "sort": "due_date",
            },
            headers=self.admin_headers,
        )

        self.assertEqual(200, response.status_code)
        self.assertIn(b"Admin todo mid", response.data)
        self.assertNotIn(b"Staff todo soon", response.data)
        self.assertIn(b'value="todo" selected', response.data)
        self.assertIn(b'value="2026-07-20"', response.data)
        self.assertIn(b'value="2026-07-22"', response.data)

    def test_staff_cannot_filter_tasks_for_other_assignees(self):
        self._seed_task_filter_data()
        response = self.client.get(
            "/tasks",
            query_string={"assignee": "admin"},
            headers=self.staff_headers,
        )
        self.assertEqual(403, response.status_code)

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

    def test_task_comments_crud_and_unread_tracking_routes(self):
        bot = FundingBot(db_path=str(self.db_path))
        task = bot.create_task(
            title="Prepare proposal",
            assigned_to="staff",
            assignee_email="staff@example.org",
        )
        bot.close()

        created = self.client.post(
            f"/tasks/{task['id']}/comments",
            json={"author": "admin@example.org", "content": "Please add a timeline."},
            headers=self.admin_headers,
        )
        self.assertEqual(201, created.status_code)
        comment_id = created.get_json()["id"]

        listed = self.client.get(
            f"/tasks/{task['id']}/comments?viewer_email=staff@example.org",
            headers=self.staff_headers,
        )
        self.assertEqual(200, listed.status_code)
        self.assertEqual(1, listed.get_json()["unread_count"])

        marked = self.client.post(
            f"/tasks/{task['id']}/comments/read",
            json={"reader_email": "staff@example.org"},
            headers=self.staff_headers,
        )
        self.assertEqual(200, marked.status_code)
        self.assertEqual(0, marked.get_json()["unread_count"])

        updated = self.client.patch(
            f"/tasks/{task['id']}/comments/{comment_id}",
            json={"content": "Please add a timeline and budget."},
            headers=self.admin_headers,
        )
        self.assertEqual(200, updated.status_code)
        self.assertIn("budget", updated.get_json()["content"])

        relisted = self.client.get(
            f"/tasks/{task['id']}/comments?viewer_email=staff@example.org",
            headers=self.staff_headers,
        )
        self.assertEqual(1, relisted.get_json()["unread_count"])

        deleted = self.client.delete(
            f"/tasks/{task['id']}/comments/{comment_id}",
            headers=self.admin_headers,
        )
        self.assertEqual(204, deleted.status_code)

    def test_task_assignment_route_sends_notification_and_rate_limits(self):
        bot = FundingBot(db_path=str(self.db_path))
        task = bot.create_task(title="Submit attachments", assigned_to="staff")
        bot.close()
        notifications = []

        def fake_sender(to_addr, subject, body):
            notifications.append({"to": to_addr, "subject": subject, "body": body})

        with patch.object(web_app_module, "_task_assignment_sender", return_value=fake_sender), patch.dict(
            os.environ,
            {"TASK_ASSIGNMENT_NOTIFICATION_RATE_LIMIT_SECONDS": "3600"},
            clear=False,
        ):
            first = self.client.post(
                f"/tasks/{task['id']}/assignment",
                json={
                    "assigned_to": "staff",
                    "assignee_email": "staff@example.org",
                    "assignee_name": "Staff User",
                },
                headers=self.admin_headers,
            )
            second = self.client.post(
                f"/tasks/{task['id']}/assignment",
                json={
                    "assigned_to": "staff",
                    "assignee_email": "staff@example.org",
                    "assignee_name": "Staff User",
                },
                headers=self.admin_headers,
            )

        self.assertEqual(200, first.status_code)
        self.assertEqual(200, second.status_code)
        self.assertEqual(1, len(notifications))
        self.assertEqual("sent", first.get_json()["notification"]["status"])
        self.assertEqual("rate_limited", second.get_json()["notification"]["status"])

    def test_metrics_include_task_counts(self):
        bot = FundingBot(db_path=str(self.db_path))
        bot.create_task(title="Prepare proposal", assigned_to="staff")
        bot.create_task(title="Review blocker", assigned_to="staff", status="blocked")
        idempotency_key = bot.generate_idempotency_key("metrics-task", {"value": 1})
        bot.execute_queue_task(
            "metrics-task",
            {"value": 1},
            lambda context, payload: {"value": payload["value"]},
            idempotency_key=idempotency_key,
            install_signal_handlers=False,
        )
        bot.execute_queue_task(
            "metrics-task",
            {"value": 1},
            lambda context, payload: {"value": payload["value"]},
            idempotency_key=idempotency_key,
            install_signal_handlers=False,
        )
        bot.close()

        response = self.client.get("/metrics", headers=self.admin_headers)

        self.assertEqual(200, response.status_code)
        body = response.data.decode("utf-8")
        self.assertIn("funding_bot_tasks_total 2", body)
        self.assertIn('funding_bot_tasks_status_total{status="todo"} 1', body)
        self.assertIn('funding_bot_tasks_status_total{status="blocked"} 1', body)
        self.assertIn('funding_bot_tasks_assigned_total{assigned_to="staff"} 2', body)
        self.assertIn("funding_bot_queue_task_runs_completed 1", body)
        self.assertIn("funding_bot_queue_duplicate_preventions_total 1", body)

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
        os.environ["ENABLE_TASK_QUEUE"] = "1"
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

        class _FakeCursor:
            def __init__(self, *, one: int = 0, rows: list[dict[str, object]] | None = None) -> None:
                self._one = one
                self._rows = rows or []

            def fetchone(self) -> tuple[int]:
                return (self._one,)

            def fetchall(self) -> list[dict[str, object]]:
                return self._rows

        class _FakeConnection:
            def execute(self, query: str) -> _FakeCursor:
                if "GROUP BY assigned_to" in query:
                    return _FakeCursor(rows=[])
                return _FakeCursor(one=0)

        fake_bot = mock.Mock()
        fake_bot.connection = _FakeConnection()
        fake_bot.get_task_status_counts.return_value = {}

        with (
            mock.patch.object(web_app_module, "_bot", return_value=fake_bot),
            mock.patch.object(web_app_module, "_get_queue_health_snapshot", return_value=queue_snapshot),
        ):
            response = self.client.get("/metrics", headers=self.admin_headers)

        self.assertEqual(200, response.status_code)
        body = response.data.decode("utf-8")
        self.assertIn("funding_bot_queue_health_status 1", body)
        self.assertIn("funding_bot_queue_broker_up 1", body)
        self.assertIn("funding_bot_queue_active_tasks 3", body)
        self.assertIn("funding_bot_queue_pending_tasks 5", body)
        self.assertIn("funding_bot_queue_depth 5", body)
        self.assertIn("funding_bot_queue_workers 2", body)

    def test_create_translation_review_defaults_to_pending_status(self):
        response = self.client.post(
            "/translations/reviews",
            json={
                "locale": "bn",
                "translation_key": "outreach.default.subject",
                "source_text": "Thank you for supporting {organization_name}",
                "translated_text": "{organization_name}কে সমর্থন করার জন্য ধন্যবাদ",
                "submitter_notes": "Initial Bengali draft",
            },
            headers=self.admin_headers,
        )
        self.assertEqual(201, response.status_code)
        payload = response.get_json()
        self.assertEqual("pending", payload["status"])
        self.assertEqual("bn", payload["locale"])
        self.assertFalse(payload["locale_metadata"]["is_rtl"])

    def test_staff_can_approve_translation_review(self):
        create_response = self.client.post(
            "/translations/reviews",
            json={
                "locale": "ar",
                "translation_key": "outreach.default.body",
                "source_text": "Thank you for your continued interest in {organization_name}.",
                "translated_text": "شكرًا لاهتمامك المستمر بـ {organization_name}.",
            },
            headers=self.admin_headers,
        )
        review_id = create_response.get_json()["id"]

        response = self.client.post(
            f"/translations/reviews/{review_id}/decision",
            json={"status": "approved", "reviewer_notes": "Ready for launch when Arabic templates ship."},
            headers=self.staff_headers,
        )
        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertEqual("approved", payload["status"])
        self.assertEqual("staff", payload["reviewed_by_role"])
        self.assertTrue(payload["locale_metadata"]["is_rtl"])

    def test_translation_reviews_can_be_filtered_by_status(self):
        self.client.post(
            "/translations/reviews",
            json={
                "locale": "bn",
                "translation_key": "dashboard.summary",
                "source_text": "Pending locale approvals",
                "translated_text": "অপেক্ষমাণ লোকেল অনুমোদন",
            },
            headers=self.admin_headers,
        )
        approved_response = self.client.post(
            "/translations/reviews",
            json={
                "locale": "ur",
                "translation_key": "dashboard.review.heading",
                "source_text": "Translation Review",
                "translated_text": "ترجمہ جائزہ",
            },
            headers=self.admin_headers,
        )
        approved_id = approved_response.get_json()["id"]
        self.client.post(
            f"/translations/reviews/{approved_id}/decision",
            json={"status": "approved"},
            headers=self.staff_headers,
        )

        response = self.client.get(
            "/translations/reviews?status=approved",
            headers=self.admin_headers,
        )
        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertEqual(1, payload["count"])
        self.assertEqual(approved_id, payload["reviews"][0]["id"])
        self.assertEqual("approved", payload["reviews"][0]["status"])

    def test_translation_dashboard_renders_rtl_preview(self):
        response = self.client.get("/translations?locale=ar", headers=self.auditor_headers)
        self.assertEqual(200, response.status_code)
        self.assertIn(b'dir="rtl"', response.data)
        self.assertIn(b"RTL preview active", response.data)
        self.assertIn(b"Translation Review", response.data)


if __name__ == "__main__":
    unittest.main()
