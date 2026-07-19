import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import task_queue
from funding_bot import FundingBot


class QueueConfigTests(unittest.TestCase):
    def test_load_queue_config_defaults_to_legacy_cron_mode(self):
        with patch.dict(os.environ, {}, clear=False):
            config = task_queue.load_queue_config()

        self.assertFalse(config.enable_task_queue)
        self.assertTrue(config.enable_legacy_cron)
        self.assertEqual("cron", config.mode)
        self.assertEqual(["cron"], config.active_modes)

    def test_load_queue_config_supports_hybrid_mode(self):
        with patch.dict(
            os.environ,
            {"ENABLE_TASK_QUEUE": "1", "ENABLE_LEGACY_CRON": "1", "CELERY_QUEUE_NAME": "funding-bot"},
            clear=False,
        ):
            config = task_queue.load_queue_config()

        self.assertTrue(config.enable_task_queue)
        self.assertTrue(config.enable_legacy_cron)
        self.assertEqual("hybrid", config.mode)
        self.assertEqual(["cron", "queue"], config.active_modes)


class CeleryTaskDefinitionTests(unittest.TestCase):
    def test_task_definitions_are_registered(self):
        self.assertEqual("funding_bot.discover", task_queue.TASK_DEFINITIONS["discover"]["task_name"])
        self.assertEqual("funding_bot.send_outreach", task_queue.TASK_DEFINITIONS["outreach"]["task_name"])
        self.assertEqual("funding_bot.send_daily_summary", task_queue.TASK_DEFINITIONS["daily-summary"]["task_name"])
        self.assertEqual("funding-bot", task_queue.TASK_DEFINITIONS["discover"]["queue"])


class CeleryTaskExecutionTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(".test_queue.db")
        if self.db_path.exists():
            self.db_path.unlink()

    def tearDown(self):
        if self.db_path.exists():
            self.db_path.unlink()

    def test_discover_task_records_progress_and_success_callback(self):
        result = task_queue.discover_opportunities_task(
            keywords=["education"],
            trusted_sources=["Grants Portal", "CSR Network", "NGO Directory"],
            db_path=str(self.db_path),
            idempotency_key="discover-1",
        )

        self.assertEqual(1, result["count"])
        bot = FundingBot(db_path=self.db_path)
        try:
            task_run = bot.get_task_run("discover-1")
            self.assertIsNotNone(task_run)
            self.assertEqual("completed", task_run["status"])
            self.assertEqual(100, task_run["progress"])
            self.assertEqual("on_success", task_run["callback_name"])
            self.assertEqual("completed", task_run["callback_payload"]["state"])
            self.assertEqual(1, len(bot.list_opportunities()))
        finally:
            bot.close()

    def test_send_outreach_task_dry_run_persists_task_run_and_communication(self):
        bot = FundingBot(db_path=self.db_path)
        try:
            bot.store_organization_profile({"name": "i4Edu", "mission": "Expand access to education."})
        finally:
            bot.close()

        result = task_queue.send_outreach_task(
            donor_email="donor@example.org",
            donor_name="Donor",
            dry_run=True,
            locale="en",
            db_path=str(self.db_path),
            idempotency_key="outreach-1",
        )

        self.assertTrue(result["dry_run"])
        bot = FundingBot(db_path=self.db_path)
        try:
            communications = bot.connection.execute("SELECT * FROM communications").fetchall()
            task_run = bot.get_task_run("outreach-1")
            self.assertEqual(1, len(communications))
            self.assertEqual("donor@example.org", communications[0]["donor_email"])
            self.assertEqual("on_success", task_run["callback_name"])
        finally:
            bot.close()

    def test_send_outreach_task_renders_requested_locale_template(self):
        bot = FundingBot(db_path=self.db_path)
        try:
            bot.store_organization_profile({"name": "i4Edu", "mission": "Expand access to education."})
        finally:
            bot.close()

        result = task_queue.send_outreach_task.run(
            None,
            donor_email="bangla@example.org",
            donor_name="দাতা",
            template_name="intro",
            locale="bn",
            dry_run=True,
            db_path=str(self.db_path),
            idempotency_key="outreach-bn",
        )

        self.assertEqual("bn", result["locale"])
        self.assertEqual("intro", result["template_name"])
        self.assertIn("প্রিয় দাতা", result["body"])

    def test_send_daily_summary_task_builds_real_sender_when_not_dry_run(self):
        bot = FundingBot(db_path=self.db_path)
        try:
            bot.store_organization_profile({"name": "i4Edu"})
        finally:
            bot.close()

        sender = object()
        with patch("task_queue.SMTPEmailSender.from_env", return_value=sender):
            result = task_queue.send_daily_summary_task(
                recipient="ops@example.org",
                dry_run=False,
                db_path=str(self.db_path),
                idempotency_key="summary-1",
            )

        self.assertEqual("ops@example.org", result["recipient"])
        bot = FundingBot(db_path=self.db_path)
        try:
            task_run = bot.get_task_run("summary-1")
            self.assertEqual("on_success", task_run["callback_name"])
            self.assertEqual(100, task_run["progress"])
        finally:
            bot.close()


class DispatchDiscoveryTests(unittest.TestCase):
    def test_dispatch_discovery_runs_inline_in_cron_mode(self):
        with patch.dict(os.environ, {"ENABLE_TASK_QUEUE": "0", "ENABLE_LEGACY_CRON": "1"}, clear=False), patch(
            "task_queue._run_discovery_inline",
            return_value={"mode": "queue", "count": 2, "new_opportunities": [{"title": "One"}, {"title": "Two"}]},
        ) as run_task:
            status_code, payload = task_queue.dispatch_discovery(keywords=["education"])

        self.assertEqual(200, status_code)
        self.assertEqual(2, payload["count"])
        self.assertTrue(payload["legacy_cron_enabled"])
        run_task.assert_called_once()

    def test_dispatch_discovery_enqueues_when_task_queue_enabled(self):
        with patch.dict(os.environ, {"ENABLE_TASK_QUEUE": "1", "ENABLE_LEGACY_CRON": "1"}, clear=False), patch.object(
            task_queue.discover_opportunities_task,
            "delay",
            return_value=SimpleNamespace(id="job-123"),
        ) as delayed:
            status_code, payload = task_queue.dispatch_discovery(keywords=["education"])

        self.assertEqual(202, status_code)
        self.assertEqual("job-123", payload["task_id"])
        self.assertEqual("hybrid", payload["mode"])
        self.assertTrue(payload["legacy_cron_enabled"])
        delayed.assert_called_once()


class QueueHealthTests(unittest.TestCase):
    def test_get_queue_status_reports_disabled_state(self):
        config = task_queue.QueueConfig(
            enable_task_queue=False,
            enable_legacy_cron=True,
            broker_url="filesystem://",
            result_backend="cache+memory://",
            task_always_eager=False,
        )

        status = task_queue.get_queue_status(config=config)

        self.assertEqual("cron", status["mode"])
        self.assertEqual("disabled", status["worker_status"])
        self.assertEqual(0, status["queue_depth"])

    def test_get_queue_status_counts_workers_and_tasks(self):
        class FakeInspect:
            def ping(self):
                return {"worker-a": {"ok": "pong"}, "worker-b": {"ok": "pong"}}

            def stats(self):
                return {"worker-a": {}, "worker-b": {}}

            def active(self):
                return {"worker-a": [{"id": "1"}], "worker-b": [{"id": "2"}]}

            def reserved(self):
                return {"worker-a": [{"id": "3"}], "worker-b": []}

            def scheduled(self):
                return {"worker-a": [], "worker-b": [{"id": "4"}, {"id": "5"}]}

        config = task_queue.QueueConfig(
            enable_task_queue=True,
            enable_legacy_cron=True,
            broker_url="filesystem://",
            result_backend="cache+memory://",
            task_always_eager=False,
        )

        status = task_queue.get_queue_status(config=config, inspector=FakeInspect())

        self.assertEqual("hybrid", status["mode"])
        self.assertEqual("healthy", status["worker_status"])
        self.assertEqual(2, status["worker_count"])
        self.assertEqual(2, status["active_tasks"])
        self.assertEqual(1, status["reserved_tasks"])
        self.assertEqual(2, status["scheduled_tasks"])
        self.assertEqual(3, status["queue_depth"])


if __name__ == "__main__":
    unittest.main()
