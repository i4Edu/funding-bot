import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import task_queue


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

    def test_load_queue_config_supports_queue_only_mode(self):
        with patch.dict(os.environ, {"ENABLE_TASK_QUEUE": "1", "ENABLE_LEGACY_CRON": "0"}, clear=False):
            config = task_queue.load_queue_config()

        self.assertEqual("queue", config.mode)
        self.assertEqual(["queue"], config.active_modes)


class CeleryTaskDefinitionTests(unittest.TestCase):
    def test_task_definitions_are_registered(self):
        self.assertEqual(
            "funding_bot.discover_opportunities",
            task_queue.TASK_DEFINITIONS["discover"]["task_name"],
        )
        self.assertEqual(
            "funding_bot.send_outreach",
            task_queue.TASK_DEFINITIONS["outreach"]["task_name"],
        )
        self.assertEqual(
            "funding_bot.send_daily_summary",
            task_queue.TASK_DEFINITIONS["daily-summary"]["task_name"],
        )
        self.assertEqual("funding-bot", task_queue.TASK_DEFINITIONS["discover"]["queue"])


class CeleryTaskExecutionTests(unittest.TestCase):
    def test_discover_opportunities_task_uses_funding_bot_and_closes_it(self):
        class FakeBot:
            def __init__(self):
                self.closed = False
                self.calls = []

            def execute_queue_task(
                self,
                task_name,
                payload,
                callback,
                *,
                idempotency_key=None,
                worker_id=None,
                install_signal_handlers=True,
            ):
                self.calls.append((task_name, payload, idempotency_key, worker_id, install_signal_handlers))
                result = callback(SimpleNamespace(bot=self, idempotency_key=idempotency_key, checkpoint=lambda *_: None), payload)
                return {
                    "idempotency_key": idempotency_key,
                    "status": "completed",
                    "duplicate": False,
                    "duplicate_requests": 0,
                    "shutdown_requested": False,
                    "result": result,
                }

            def run_discovery(self, *, keywords=None, trusted_sources=None):
                return [{"title": "Education Innovation Grant"}]

            def close(self):
                self.closed = True

        fake_bot = FakeBot()
        with patch("task_queue.FundingBot", return_value=fake_bot):
            payload = task_queue.discover_opportunities_task.run(
                None,
                keywords=["education"],
                trusted_sources=["Grants Portal"],
                db_path=".test_queue.db",
                idempotency_key="discover-key",
            )

        self.assertEqual(1, payload["count"])
        self.assertEqual("queue", payload["mode"])
        self.assertEqual(
            [("discover_opportunities", {"keywords": ["education"], "trusted_sources": ["Grants Portal"]}, "discover-key", None, True)],
            fake_bot.calls,
        )
        self.assertEqual("discover-key", payload["idempotency_key"])
        self.assertTrue(fake_bot.closed)

    def test_send_outreach_task_respects_dry_run(self):
        class FakeBot:
            def __init__(self):
                self.closed = False
                self.sender = None

            def execute_queue_task(
                self,
                task_name,
                payload,
                callback,
                *,
                idempotency_key=None,
                worker_id=None,
                install_signal_handlers=True,
            ):
                result = callback(SimpleNamespace(bot=self, idempotency_key=idempotency_key, checkpoint=lambda *_: None), payload)
                return {
                    "idempotency_key": idempotency_key,
                    "status": "completed",
                    "duplicate": False,
                    "duplicate_requests": 0,
                    "shutdown_requested": False,
                    "result": result,
                }

            def send_outreach(self, **kwargs):
                self.sender = kwargs["sender"]
                return {"email": kwargs["donor_email"], "subject": "Hello", "body": "Body"}

            def close(self):
                self.closed = True

        fake_bot = FakeBot()
        with patch("task_queue.FundingBot", return_value=fake_bot):
            payload = task_queue.send_outreach_task.run(
                None,
                donor_email="donor@example.org",
                donor_name="Donor",
                subject_template="Hello {donor_name}",
                body_template="Body",
                dry_run=True,
                db_path=".test_queue.db",
                idempotency_key="outreach-key",
            )

        self.assertTrue(payload["dry_run"])
        self.assertEqual("queue", payload["mode"])
        self.assertEqual("outreach-key", payload["idempotency_key"])
        self.assertIsNone(fake_bot.sender)
        self.assertTrue(fake_bot.closed)

    def test_send_daily_summary_task_builds_real_sender_when_not_dry_run(self):
        class FakeBot:
            def __init__(self):
                self.closed = False
                self.sender = None

            def execute_queue_task(
                self,
                task_name,
                payload,
                callback,
                *,
                idempotency_key=None,
                worker_id=None,
                install_signal_handlers=True,
            ):
                result = callback(SimpleNamespace(bot=self, idempotency_key=idempotency_key, checkpoint=lambda *_: None), payload)
                return {
                    "idempotency_key": idempotency_key,
                    "status": "completed",
                    "duplicate": False,
                    "duplicate_requests": 0,
                    "shutdown_requested": False,
                    "result": result,
                }

            def send_daily_summary(self, *, recipient, sender):
                self.sender = sender
                return {"recipient": recipient, "subject": "Daily report", "body": "Summary"}

            def close(self):
                self.closed = True

        fake_bot = FakeBot()
        sender = object()
        with patch("task_queue.FundingBot", return_value=fake_bot), patch(
            "task_queue.SMTPEmailSender.from_env", return_value=sender
        ):
            payload = task_queue.send_daily_summary_task.run(
                None,
                recipient="ops@example.org",
                dry_run=False,
                db_path=".test_queue.db",
                idempotency_key="summary-key",
            )

        self.assertEqual("ops@example.org", payload["recipient"])
        self.assertFalse(payload["dry_run"])
        self.assertEqual("summary-key", payload["idempotency_key"])
        self.assertIs(sender, fake_bot.sender)
        self.assertTrue(fake_bot.closed)


class DispatchDiscoveryTests(unittest.TestCase):
    def test_dispatch_discovery_runs_inline_in_cron_mode(self):
        with patch.dict(os.environ, {"ENABLE_TASK_QUEUE": "0", "ENABLE_LEGACY_CRON": "1"}, clear=False), patch(
            "task_queue.discover_opportunities_task.run",
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
        self.assertIn("idempotency_key", payload)
        self.assertTrue(payload["legacy_cron_enabled"])
        delayed.assert_called_once()


class QueueHealthTests(unittest.TestCase):
    def test_get_queue_status_reports_disabled_state(self):
        config = task_queue.QueueConfig(
            enable_task_queue=False,
            enable_legacy_cron=True,
            broker_url="redis://redis:6379/0",
            result_backend="redis://redis:6379/1",
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
            broker_url="redis://redis:6379/0",
            result_backend="redis://redis:6379/1",
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
