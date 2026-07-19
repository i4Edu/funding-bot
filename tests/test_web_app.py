import base64
import itertools
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("ADMIN_PASSWORD", "admin-secret")
os.environ.setdefault("STAFF_PASSWORD", "staff-secret")
os.environ.setdefault("AUDITOR_PASSWORD", "auditor-secret")

import web.app as web_app_module  # noqa: E402
from funding_bot import FundingBot  # noqa: E402
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
        FundingBot.reset_connector_metrics()
        web_app_module.reset_health_check_metrics()
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
        FundingBot.reset_connector_metrics()
        web_app_module.reset_health_check_metrics()

    def test_settings_page_requires_authentication(self):
        response = self.client.get("/settings")
        self.assertEqual(401, response.status_code)

    def test_settings_page_renders_for_authenticated_role(self):
        response = self.client.get("/settings", headers=self.auditor_headers)
        self.assertEqual(200, response.status_code)
        self.assertIn(b"Settings", response.data)
        self.assertIn(b"Translations", response.data)
        self.assertIn(b"Queue Monitoring", response.data)
        self.assertIn(b"Open Flower", response.data)

    def test_dashboard_page_exposes_keyboard_shortcuts_and_focus_regions(self):
        html = (PROJECT_ROOT / "web" / "templates" / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn('aria-label="Open settings page"', html)
        self.assertIn('id="main-content" class="container py-4" tabindex="-1"', html)
        self.assertIn('id="recent-opps-region"', html)
        self.assertIn(
            "Alt</kbd> + <kbd>Shift</kbd> + <kbd>O</kbd> — Focus recent opportunities", html
        )
        self.assertIn(
            'KeyO: () => focusAndScroll(document.getElementById("recent-opps-region"))', html
        )

    def test_settings_page_includes_aria_labels_live_regions_and_shortcuts(self):
        html = (PROJECT_ROOT / "web" / "templates" / "settings.html").read_text(encoding="utf-8")
        self.assertIn('aria-label="Save organization profile"', html)
        self.assertIn('aria-label="Run donation discovery now"', html)
        self.assertIn('aria-keyshortcuts="Alt+Shift+R"', html)
        self.assertIn(
            'role="status" aria-live="polite" aria-atomic="true" aria-label="Discovery results"',
            html,
        )
        self.assertIn(
            'role="status" aria-live="polite" aria-atomic="true" aria-label="Outreach results"',
            html,
        )
        self.assertIn("Alt</kbd> + <kbd>Shift</kbd> + <kbd>T</kbd> — Focus donor outreach", html)

    def test_task_dashboard_page_includes_shortcut_help(self):
        html = (PROJECT_ROOT / "web" / "templates" / "tasks.html").read_text(encoding="utf-8")
        self.assertIn('aria-label="Open dashboard page"', html)
        self.assertIn('id="task-board-region"', html)
        self.assertIn("Alt</kbd> + <kbd>Shift</kbd> + <kbd>T</kbd> — Focus the task board", html)
        self.assertIn('aria-keyshortcuts="Enter Space ArrowLeft ArrowRight"', html)
        self.assertIn('id="task-export-link"', html)
        self.assertIn('id="task-create-form"', html)
        self.assertIn('id="task-edit-form"', html)

    def test_settings_page_binds_keyboard_activation_for_action_buttons(self):
        html = (PROJECT_ROOT / "web" / "templates" / "settings.html").read_text(encoding="utf-8")
        self.assertIn(
            'document.querySelectorAll("[data-keyboard-click]").forEach(bindKeyboardActivation);',
            html,
        )
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
            json={
                "name": "i4Edu",
                "mission": "Educate",
                "privacy_jurisdictions": ["EU", "US"],
                "field_classifications": {"mission": "internal"},
            },
            headers=self.admin_headers,
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            {"name": "i4Edu", "mission": "Educate", "privacy_jurisdictions": ["EU", "US"]},
            response.get_json()["organization_profile"],
        )

    def test_upsert_donor_returns_classification_metadata(self):
        response = self.client.post(
            "/donors",
            json={"email": "donor@example.org", "name": "Donor", "locale": "bn"},
            headers=self.admin_headers,
        )
        self.assertEqual(201, response.status_code)
        payload = response.get_json()
        self.assertEqual("bn", payload["locale"])
        self.assertEqual("secret", payload["data_classification"])
        self.assertEqual("secret", payload["field_classifications"]["preferences"])

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
        self.assertTrue(payload["healthy"])
        self.assertEqual("cron", payload["queue"]["mode"])
        self.assertEqual("ok", payload["checks"]["application"]["status"])
        self.assertEqual("ok", payload["checks"]["database"]["status"])

    def test_ready_endpoint_reports_dependency_checks(self):
        with (
            mock.patch.object(
                web_app_module,
                "_check_database_health",
                return_value={"status": "ok", "checked": True},
            ),
            mock.patch.object(
                web_app_module,
                "_check_redis_health",
                return_value={"status": "disabled", "checked": False},
            ),
            mock.patch.object(
                web_app_module,
                "_check_celery_health",
                return_value={"status": "disabled", "checked": False, "queue_enabled": False},
            ),
            mock.patch.object(
                web_app_module,
                "_check_connector_health",
                return_value={
                    "status": "ok",
                    "checked": True,
                    "count": 2,
                    "healthy_count": 2,
                    "connectors": [],
                },
            ),
        ):
            response = self.client.get("/ready")

        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertTrue(payload["ready"])
        self.assertEqual([], payload["failing_checks"])
        self.assertEqual("ok", payload["checks"]["database"]["status"])
        self.assertEqual("disabled", payload["checks"]["redis"]["status"])
        self.assertEqual("disabled", payload["checks"]["celery"]["status"])
        self.assertEqual("ok", payload["checks"]["connectors"]["status"])

    def test_ready_endpoint_returns_503_when_dependency_fails(self):
        with (
            mock.patch.object(
                web_app_module,
                "_check_database_health",
                return_value={"status": "error", "checked": True},
            ),
            mock.patch.object(
                web_app_module,
                "_check_redis_health",
                return_value={"status": "ok", "checked": True},
            ),
            mock.patch.object(
                web_app_module,
                "_check_celery_health",
                return_value={"status": "disabled", "checked": False, "queue_enabled": False},
            ),
            mock.patch.object(
                web_app_module,
                "_check_connector_health",
                return_value={
                    "status": "ok",
                    "checked": True,
                    "count": 1,
                    "healthy_count": 1,
                    "connectors": [],
                },
            ),
        ):
            response = self.client.get("/ready")

        self.assertEqual(503, response.status_code)
        payload = response.get_json()
        self.assertFalse(payload["ready"])
        self.assertEqual(["database"], payload["failing_checks"])
        self.assertIn("database", payload)
        self.assertIn("redis", payload)
        self.assertEqual("ok", payload["redis"]["status"])

    def test_database_and_cache_health_endpoints_return_monitoring_payloads(self):
        database_response = self.client.get("/health/database")
        cache_response = self.client.get("/health/cache")

        self.assertEqual(200, database_response.status_code)
        self.assertEqual(200, cache_response.status_code)
        database_payload = database_response.get_json()
        self.assertIn("pool_class", database_payload)
        self.assertIn("queries", database_payload)
        self.assertIn("summary", database_payload["queries"])
        self.assertIn("backend", cache_response.get_json())

    def test_queue_health_endpoint_reports_disabled_queue_mode(self):
        response = self.client.get("/health/queue")

        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertEqual("disabled", payload["status"])
        self.assertEqual("cron", payload["mode"])
        self.assertEqual(0, payload["queue_depth"])

    def test_queue_monitoring_endpoint_returns_health_and_runtime_metrics(self):
        monitoring_payload = {
            "queue": {
                "status": "ok",
                "queue_name": "funding-bot",
                "broker_reachable": True,
                "timeout_seconds": 2.0,
                "active_tasks": 1,
                "pending_tasks": 3,
                "queue_depth": 3,
                "worker_count": 2,
                "workers": ["worker-a", "worker-b"],
            },
            "task_metrics": {
                "running": 1,
                "completed": 4,
                "failed": 1,
                "cancelled": 0,
                "retries_scheduled": 2,
                "dead_lettered": 1,
                "duplicate_preventions": 0,
                "duration_seconds_sum": 18.0,
                "duration_seconds_count": 4,
                "duration_seconds_average": 4.5,
                "duration_seconds_max": 7.0,
            },
            "flower": {"url": "http://127.0.0.1:5555", "enabled": True},
        }

        with mock.patch.object(
            web_app_module, "_queue_monitoring_payload", return_value=monitoring_payload
        ):
            response = self.client.get("/monitoring/queue", headers=self.auditor_headers)

        self.assertEqual(200, response.status_code)
        self.assertEqual(monitoring_payload, response.get_json())

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

    def test_metrics_include_queue_retry_and_dead_letter_counts(self):
        self.client.get("/settings", headers=self.admin_headers)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO task_runs (
                    task_id, idempotency_key, task_name, status, progress, message, payload_json,
                    result_json, error_message, worker_id, duplicate_requests, shutdown_requested,
                    callback_name, callback_payload_json, created_at, updated_at, completed_at,
                    retry_limit, attempts, backoff_seconds, backoff_max_seconds, dead_lettered,
                    last_attempt_at, next_retry_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "completed-task",
                    "completed-task",
                    "discover_opportunities",
                    "completed",
                    100,
                    "Task completed.",
                    '{"keywords":["education"]}',
                    '{"count":1}',
                    None,
                    None,
                    0,
                    0,
                    "on_success",
                    '{"attempt_number":2,"state":"completed"}',
                    "2026-07-19T00:00:00+00:00",
                    "2026-07-19T00:00:02+00:00",
                    "2026-07-19T00:00:02+00:00",
                    2,
                    2,
                    1.0,
                    2.0,
                    0,
                    "2026-07-19T00:00:02+00:00",
                    None,
                ),
            )
            conn.execute(
                """
                INSERT INTO task_runs (
                    task_id, idempotency_key, task_name, status, progress, message, payload_json,
                    result_json, error_message, worker_id, duplicate_requests, shutdown_requested,
                    callback_name, callback_payload_json, created_at, updated_at, completed_at,
                    retry_limit, attempts, backoff_seconds, backoff_max_seconds, dead_lettered,
                    last_attempt_at, next_retry_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "failed-task",
                    "failed-task",
                    "send_outreach",
                    "failed",
                    0,
                    "Task failed.",
                    '{"email":"donor@example.org"}',
                    None,
                    "permanent queue error",
                    None,
                    0,
                    0,
                    "on_failure",
                    '{"attempt_number":3,"state":"failed"}',
                    "2026-07-19T00:00:00+00:00",
                    "2026-07-19T00:00:03+00:00",
                    "2026-07-19T00:00:03+00:00",
                    2,
                    3,
                    1.0,
                    2.0,
                    1,
                    "2026-07-19T00:00:03+00:00",
                    None,
                ),
            )
            conn.execute(
                """
                INSERT INTO task_history (
                    task_id, task_name, attempt_number, status, happened_at, backoff_seconds,
                    next_retry_at, result_json, error_message, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "failed-task",
                    "send_outreach",
                    1,
                    "retry_scheduled",
                    "2026-07-19T00:00:00+00:00",
                    1.0,
                    "2026-07-19T00:00:01+00:00",
                    None,
                    "temporary queue error",
                    "{}",
                ),
            )
            conn.execute(
                """
                INSERT INTO dead_letter_queue (
                    task_id, task_name, payload_json, error_message, attempts, failed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "failed-task",
                    "send_outreach",
                    '{"email":"donor@example.org"}',
                    "permanent queue error",
                    3,
                    "2026-07-19T00:00:03+00:00",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        response = self.client.get("/metrics", headers=self.admin_headers)
        self.assertEqual(200, response.status_code)
        body = response.data.decode("utf-8")
        self.assertIn("funding_bot_queue_task_runs_failed 1", body)
        self.assertIn("funding_bot_queue_task_retries_total 1", body)
        self.assertIn("funding_bot_dead_letter_queue_total 1", body)
        self.assertIn("funding_bot_queue_task_duration_seconds_count 2", body)
        self.assertIn("funding_bot_queue_task_duration_seconds_average ", body)
        self.assertIn("funding_bot_queue_task_duration_seconds_max ", body)
        self.assertIn("funding_bot_db_pool_size", body)
        self.assertIn('funding_bot_db_queries_total{statement="all",status="success"}', body)
        self.assertIn(
            'funding_bot_db_query_duration_seconds_bucket{statement="all",le="+Inf"}', body
        )
        self.assertIn("funding_bot_db_query_slow_threshold_seconds", body)
        self.assertIn('funding_bot_cache_hits_total{cache="donor-records"', body)

    def test_metrics_include_connector_request_error_and_latency_series(self):
        discover = self.client.post(
            "/settings/discover",
            json={"keywords": ["education"]},
            headers=self.admin_headers,
        )
        self.assertEqual(200, discover.status_code)

        response = self.client.get("/metrics", headers=self.admin_headers)

        self.assertEqual(200, response.status_code)
        body = response.data.decode("utf-8")
        self.assertIn("# HELP funding_bot_connector_requests_total", body)
        self.assertIn(
            'funding_bot_connector_requests_total{connector_name="Grants Portal",connector_type="grants-portal"} 1',
            body,
        )
        self.assertIn(
            'funding_bot_connector_errors_total{connector_name="Grants Portal",connector_type="grants-portal"} 0',
            body,
        )
        self.assertIn(
            'funding_bot_connector_latency_seconds_count{connector_name="Grants Portal",connector_type="grants-portal"} 1',
            body,
        )

    def test_analytics_endpoints_expose_funnel_costs_and_alerts(self):
        bot = FundingBot(db_path=str(self.db_path), trusted_sources={"Grants Portal"})
        try:
            discovered = bot.discover_opportunities(
                [
                    {
                        "source": "Grants Portal",
                        "donor_name": "UNICEF",
                        "title": "UNICEF Literacy Grant",
                        "portal_url": "https://example.org/unicef",
                        "summary": "Funding for literacy programs.",
                        "tags": ["literacy"],
                        "category": "Education",
                    }
                ],
                keywords=["literacy"],
                discovered_at=datetime(2026, 6, 22, 8, 0, tzinfo=timezone.utc),
            )
            signature = discovered[0]["signature"]
            task = bot.create_task(
                title="Review matched grant",
                assigned_to="staff",
                due_date="2026-06-30",
                attributed_connector="Grants Portal",
                opportunity_signature=signature,
            )
            bot.send_outreach(
                donor_email="engaged@example.org",
                donor_name="Engaged Donor",
                subject_template="Support {organization_name}",
                body_template="Hello {donor_name}",
                context={"opportunity_signature": signature, "task_id": task["id"]},
                sent_at=datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc),
            )
            communication_id = bot.connection.execute(
                "SELECT id FROM communications WHERE donor_email = ?",
                ("engaged@example.org",),
            ).fetchone()["id"]
            bot.record_outreach_event(
                communication_id,
                "opened",
                happened_at=datetime(2026, 6, 22, 9, 30, tzinfo=timezone.utc),
            )
            for day in range(1, 8):
                bot.record_connector_call_metric(
                    connector_name="Grants Portal",
                    connector_type="grants-portal",
                    operation="discover",
                    source_status="remote",
                    latency_seconds=0.4,
                    cost_usd=1.0,
                    errored=False,
                    request_count=1,
                    happened_at=datetime(2026, 6, day, 8, 0, tzinfo=timezone.utc),
                )
            for minute in range(4):
                bot.record_connector_call_metric(
                    connector_name="Grants Portal",
                    connector_type="grants-portal",
                    operation="discover",
                    source_status="remote",
                    latency_seconds=2.5,
                    cost_usd=4.0,
                    errored=True,
                    request_count=1,
                    happened_at=datetime(2026, 6, 8, 8, minute, tzinfo=timezone.utc),
                )
        finally:
            bot.close()

        analytics = self.client.get("/analytics", headers=self.auditor_headers)
        funnel = self.client.get("/analytics/funnel", headers=self.auditor_headers)
        costs = self.client.get("/analytics/costs", headers=self.auditor_headers)
        attribution = self.client.get("/analytics/attribution", headers=self.auditor_headers)
        anomalies = self.client.get(
            "/analytics/anomalies?end_at=2026-06-08T09:00:00+00:00",
            headers=self.auditor_headers,
        )
        dashboard = self.client.get("/analytics/dashboard", headers=self.auditor_headers)

        self.assertEqual(200, analytics.status_code)
        self.assertIn("dashboard", analytics.get_json())
        self.assertEqual(200, funnel.status_code)
        self.assertEqual(1, funnel.get_json()["stages"][0]["count"])
        self.assertEqual(200, costs.status_code)
        self.assertEqual(16.0, costs.get_json()["connectors"][0]["total_cost_usd"])
        self.assertEqual(200, attribution.status_code)
        self.assertEqual(1, attribution.get_json()["connectors"][0]["responses"])
        self.assertEqual(200, anomalies.status_code)
        self.assertTrue(anomalies.get_json()["alerts"])
        self.assertEqual(200, dashboard.status_code)
        self.assertIn("funnel", dashboard.get_json())

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

    def test_dashboard_tasks_renders_admin_task_management_controls(self):
        bot = FundingBot(db_path=str(self.db_path))
        bot.create_task(title="Prepare proposal", assigned_to="admin", due_date="2026-07-21")
        bot.close()

        response = self.client.get("/dashboard/tasks", headers=self.admin_headers)

        self.assertEqual(200, response.status_code)
        self.assertIn(b"Task management", response.data)
        self.assertIn(b"Create task", response.data)
        self.assertIn(b"Edit selected task", response.data)
        self.assertIn(b"Export JSON", response.data)

    def _seed_task_filter_data(self):
        payloads = [
            {
                "title": "Staff todo soon",
                "assigned_to": "staff",
                "status": "todo",
                "due_date": "2026-07-20",
            },
            {
                "title": "Staff in progress late",
                "assigned_to": "staff",
                "status": "in-progress",
                "due_date": "2026-07-25",
            },
            {
                "title": "Admin todo mid",
                "assigned_to": "admin",
                "status": "todo",
                "due_date": "2026-07-22",
            },
            {
                "title": "Auditor done early",
                "assigned_to": "auditor",
                "status": "done",
                "due_date": "2026-07-18",
            },
            {
                "title": "Admin blocked latest",
                "assigned_to": "admin",
                "status": "blocked",
                "due_date": "2026-08-01",
            },
        ]
        tasks = []
        for payload in payloads:
            response = self.client.post("/tasks", json=payload, headers=self.admin_headers)
            self.assertEqual(201, response.status_code)
            tasks.append(response.get_json()["task"])
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
                    else (
                        task["status"] == filter_values["status"]
                        if name == "status"
                        else (
                            due_date is not None and due_date >= filter_values["due_date_after"]
                            if name == "due_date_after"
                            else due_date is not None
                            and due_date <= filter_values["due_date_before"]
                        )
                    )
                )
                for name in active_filters
            )

        for size in range(1, len(filter_values) + 1):
            for active_filters in itertools.combinations(filter_values, size):
                query_string = {name: filter_values[name] for name in active_filters}
                query_string["sort"] = "due_date"
                response = self.client.get(
                    "/tasks", query_string=query_string, headers=self.admin_headers
                )
                self.assertEqual(200, response.status_code)
                expected_titles = [
                    task["title"]
                    for task in sorted(
                        (task for task in tasks if matches(task, active_filters)),
                        key=lambda task: (task["due_date"] is None, task["due_date"], task["id"]),
                    )
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
                "Admin blocked latest",
                "Auditor done early",
                "Staff todo soon",
                "Staff in progress late",
            ],
            "status": [
                "Admin blocked latest",
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
                "Admin blocked latest",
            ],
        }
        for sort_name, expected_titles in expected_orders.items():
            response = self.client.get(
                "/tasks", query_string={"sort": sort_name}, headers=self.admin_headers
            )
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

    def test_task_assignment_route_requires_admin(self):
        bot = FundingBot(db_path=str(self.db_path))
        task = bot.create_task(title="Review rubric", assigned_to="staff")
        bot.close()

        forbidden = self.client.post(
            f"/tasks/{task['id']}/assign",
            json={"assigned_to": "auditor"},
            headers=self.staff_headers,
        )
        self.assertEqual(403, forbidden.status_code)

        allowed = self.client.post(
            f"/tasks/{task['id']}/assign",
            json={"assigned_to": "auditor"},
            headers=self.admin_headers,
        )
        self.assertEqual(200, allowed.status_code)
        self.assertEqual("auditor", allowed.get_json()["task"]["assigned_to"])

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

        with (
            patch.object(web_app_module, "_task_assignment_sender", return_value=fake_sender),
            patch.dict(
                os.environ,
                {"TASK_ASSIGNMENT_NOTIFICATION_RATE_LIMIT_SECONDS": "3600"},
                clear=False,
            ),
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

        with mock.patch.object(
            web_app_module, "_get_queue_health_snapshot", return_value=queue_snapshot
        ):
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

        with mock.patch.object(
            web_app_module, "_get_queue_health_snapshot", return_value=queue_snapshot
        ):
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
            def __init__(
                self, *, one: int = 0, rows: list[dict[str, object]] | None = None
            ) -> None:
                self._one = one
                self._rows = rows or []

            def fetchone(self) -> tuple[int]:
                return (self._one,)

            def fetchall(self) -> list[dict[str, object]]:
                return self._rows

        class _FakeConnection:
            def execute(self, query: str) -> _FakeCursor:
                if "GROUP BY assignee" in query:
                    return _FakeCursor(rows=[])
                return _FakeCursor(one=0)

        fake_bot = mock.Mock()
        fake_bot.connection = _FakeConnection()
        fake_bot.get_task_status_counts.return_value = {}

        with (
            mock.patch.object(web_app_module, "_bot", return_value=fake_bot),
            mock.patch.object(
                web_app_module, "_get_queue_health_snapshot", return_value=queue_snapshot
            ),
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

    def test_metrics_include_health_check_counters(self):
        with (
            mock.patch.object(
                web_app_module,
                "_check_database_health",
                return_value={"status": "ok", "checked": True},
            ),
            mock.patch.object(
                web_app_module,
                "_check_redis_health",
                return_value={"status": "error", "checked": True},
            ),
            mock.patch.object(
                web_app_module,
                "_check_celery_health",
                return_value={"status": "disabled", "checked": False, "queue_enabled": False},
            ),
            mock.patch.object(
                web_app_module,
                "_check_connector_health",
                return_value={
                    "status": "ok",
                    "checked": True,
                    "count": 1,
                    "healthy_count": 1,
                    "connectors": [],
                },
            ),
        ):
            self.client.get("/health")
            self.client.get("/ready")

        response = self.client.get("/metrics", headers=self.admin_headers)

        self.assertEqual(200, response.status_code)
        body = response.data.decode("utf-8")
        self.assertIn('funding_bot_health_checks_total{endpoint="health"} 1', body)
        self.assertIn('funding_bot_health_checks_total{endpoint="ready"} 1', body)
        self.assertIn('funding_bot_health_failures_total{endpoint="health"} 0', body)
        self.assertIn('funding_bot_health_failures_total{endpoint="ready"} 1', body)
        self.assertIn('funding_bot_health_component_checks_total{component="database"} 2', body)
        self.assertIn('funding_bot_health_component_failures_total{component="redis"} 1', body)

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

        staff_client = app.test_client()
        response = staff_client.post(
            f"/translations/reviews/{review_id}/decision",
            json={
                "status": "approved",
                "reviewer_notes": "Ready for launch when Arabic templates ship.",
            },
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
        staff_client = app.test_client()
        staff_client.post(
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


class TaskApiRequirementRouteTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(".test_web_task_api.db")
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

    def test_post_tasks_requires_admin(self):
        response = self.client.post(
            "/tasks",
            json={
                "title": "Write summary",
                "description": "Draft a concise summary",
                "assignee": "staff",
                "status": "pending",
                "due_date": "2026-07-30",
            },
            headers=self.staff_headers,
        )
        self.assertEqual(403, response.status_code)

    def test_admin_can_create_list_and_update_tasks(self):
        created = self.client.post(
            "/tasks",
            json={
                "title": "Write summary",
                "description": "Draft a concise summary",
                "assignee": "staff",
                "status": "pending",
                "due_date": "2026-07-30",
            },
            headers=self.admin_headers,
        )
        self.assertEqual(201, created.status_code)
        task_id = created.get_json()["task"]["id"]

        second = self.client.post(
            "/tasks",
            json={
                "title": "Review budget",
                "description": "Review finance notes",
                "assignee": "staff",
                "status": "pending",
                "due_date": "2026-07-25",
            },
            headers=self.admin_headers,
        )
        self.assertEqual(201, second.status_code)

        listed = self.client.get(
            "/tasks",
            query_string={
                "assignee": "staff",
                "status": "pending",
                "sort_by": "due_date",
                "sort_order": "asc",
            },
            headers=self.staff_headers,
        )
        self.assertEqual(200, listed.status_code)
        self.assertEqual(
            ["Review budget", "Write summary"], [row["title"] for row in listed.get_json()]
        )

        updated = self.client.put(
            f"/tasks/{task_id}",
            json={"status": "blocked", "assignee": "auditor", "due_date": "2026-07-31"},
            headers=self.admin_headers,
        )
        self.assertEqual(200, updated.status_code)
        payload = updated.get_json()["task"]
        self.assertEqual("blocked", payload["status"])
        self.assertEqual("auditor", payload["assignee"])
        self.assertEqual("2026-07-31", payload["due_date"])

    def test_put_tasks_requires_admin(self):
        bot = FundingBot(db_path=str(self.db_path))
        try:
            task = bot.create_task(
                title="Prepare checklist",
                assignee="staff",
                description="Initial draft",
                due_date="2026-07-24",
            )
        finally:
            bot.close()

        response = self.client.put(
            f"/tasks/{task['id']}",
            json={"status": "blocked"},
            headers=self.auditor_headers,
        )
        self.assertEqual(403, response.status_code)


class ExportApiRouteTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(f".test_web_exports_{self._testMethodName}.db")
        self.output_dir = Path(f".test_web_exports_output_{self._testMethodName}")
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
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.admin_headers = _auth_header("admin", "admin-secret")
        self.auditor_headers = _auth_header("auditor", "auditor-secret")
        bot = FundingBot(db_path=str(self.db_path))
        try:
            bot.upsert_donor(email="donor@example.org", name="Donor Example")
            bot.create_task(
                title="Prepare export",
                assignee="staff",
                description="Prepare warehouse export",
                due_date="2026-07-30",
            )
            opportunity = bot.discover_opportunities(
                [
                    {
                        "source": "Grants Portal",
                        "donor_name": "Example Foundation",
                        "title": "Example Grant",
                        "portal_url": "https://example.org/grants/1",
                        "summary": "Supports education programs.",
                        "category": "education",
                    }
                ],
                keywords=["education"],
                trusted_sources=["Grants Portal"],
            )[0]
            bot.submit_application(
                opportunity["signature"],
                submission_reference="ref-1",
                status="submitted",
                next_action="Await donor review",
            )
        finally:
            bot.close()

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
        os.environ.pop("ENABLE_TASK_QUEUE", None)

    def test_post_exports_route_creates_export_artifacts(self):
        response = self.client.post(
            "/api/exports",
            json={
                "datasets": ["donors", "tasks", "matches", "results"],
                "format": "json",
                "output_dir": str(self.output_dir),
                "archive": False,
            },
            headers=self.admin_headers,
        )
        self.assertEqual(201, response.status_code)
        payload = response.get_json()
        self.assertEqual(4, payload["count"])
        self.assertTrue(all(Path(artifact["path"]).exists() for artifact in payload["artifacts"]))

    def test_post_exports_route_can_enqueue_async_job(self):
        os.environ["ENABLE_TASK_QUEUE"] = "1"
        with patch(
            "web.app.dispatch_export",
            return_value=(
                202,
                {
                    "mode": "queue",
                    "task_id": "export-123",
                    "task_name": "funding_bot.export_data_warehouse",
                    "legacy_cron_enabled": True,
                },
            ),
        ):
            response = self.client.post(
                "/api/exports",
                json={"async": True, "output_dir": str(self.output_dir)},
                headers=self.admin_headers,
            )
        self.assertEqual(202, response.status_code)
        self.assertEqual("export-123", response.get_json()["task_id"])

    def test_get_exports_route_returns_schedule_and_audits(self):
        self.client.post(
            "/api/exports",
            json={"datasets": ["donors"], "format": "json", "output_dir": str(self.output_dir)},
            headers=self.admin_headers,
        )
        response = self.client.get("/api/exports", headers=self.auditor_headers)
        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertIn("schedule", payload)
        self.assertGreaterEqual(payload["count"], 1)


class DatabaseIndexMetricsRouteTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(".test_web_index_metrics.db")
        if self.db_path.exists():
            self.db_path.unlink()
        os.environ["BOT_DB_PATH"] = str(self.db_path)
        app.config["TESTING"] = True
        self.client = app.test_client()
        self.admin_headers = _auth_header("admin", "admin-secret")

    def tearDown(self):
        if self.db_path.exists():
            self.db_path.unlink()
        os.environ.pop("BOT_DB_PATH", None)

    def test_database_health_endpoint_includes_index_snapshot(self):
        response = self.client.get("/health/database")

        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertIn("indexes", payload)
        self.assertGreater(payload["indexes"]["summary"]["expected"], 0)
        self.assertEqual(
            payload["indexes"]["summary"]["expected"],
            payload["indexes"]["summary"]["present"],
        )

    def test_metrics_include_index_monitoring_series(self):
        response = self.client.get("/metrics", headers=self.admin_headers)

        self.assertEqual(200, response.status_code)
        body = response.data.decode("utf-8")
        self.assertIn("funding_bot_db_indexes_expected_total", body)
        self.assertIn('funding_bot_db_index_present{index_name="idx_tasks_created_at_status"', body)
        self.assertIn(
            'funding_bot_db_query_plan_uses_index{query_name="task-assignee-status"} 1', body
        )


if __name__ == "__main__":
    unittest.main()
