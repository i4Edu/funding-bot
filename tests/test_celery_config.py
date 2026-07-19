import os
import unittest
from pathlib import Path
from unittest import mock

from celery_app import (
    DEFAULT_CELERY_BROKER_URL,
    DEFAULT_CELERY_RESULT_BACKEND,
    DEFAULT_RABBITMQ_BROKER_URL,
    get_celery_config,
)
from funding_bot import FundingBot
from tasks.celery_tasks import run_discovery_task, send_daily_summary_task


class CeleryConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(".test_celery.db")
        if self.db_path.exists():
            self.db_path.unlink()

    def tearDown(self):
        if self.db_path.exists():
            self.db_path.unlink()

    def test_default_config_prefers_redis(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            config = get_celery_config()

        self.assertEqual(DEFAULT_CELERY_BROKER_URL, config["broker_url"])
        self.assertEqual(DEFAULT_CELERY_RESULT_BACKEND, config["result_backend"])
        self.assertEqual(("tasks.celery_tasks",), config["imports"])

    def test_rabbitmq_broker_override_is_supported(self):
        env = {
            "CELERY_BROKER_URL": DEFAULT_RABBITMQ_BROKER_URL,
            "CELERY_RESULT_BACKEND": "rpc://",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            config = get_celery_config()

        self.assertEqual(DEFAULT_RABBITMQ_BROKER_URL, config["broker_url"])
        self.assertEqual("rpc://", config["result_backend"])

    def test_discovery_task_persists_opportunities(self):
        result = run_discovery_task.run(
            db_path=str(self.db_path),
            keywords=["education"],
            trusted_sources=["Grants Portal", "CSR Network", "NGO Directory"],
        )

        self.assertEqual(1, result["count"])
        bot = FundingBot(db_path=self.db_path)
        try:
            self.assertEqual(1, len(bot.list_opportunities()))
        finally:
            bot.close()

    def test_daily_summary_task_supports_dry_run(self):
        bot = FundingBot(db_path=self.db_path)
        try:
            opportunity = bot.discover_opportunities(
                [
                    {
                        "source": "Grants Portal",
                        "donor_name": "UNICEF",
                        "title": "UNICEF CSR Grant",
                        "portal_url": "https://example.org/unicef",
                        "summary": "CSR funding for nonprofit education programs.",
                        "tags": ["CSR funding", "nonprofit grants"],
                        "category": "Education",
                    }
                ],
                keywords=["csr funding"],
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

        result = send_daily_summary_task.run(
            recipient="ops@example.org",
            db_path=str(self.db_path),
            dry_run=True,
        )
        self.assertTrue(result["dry_run"])
        self.assertEqual("ops@example.org", result["recipient"])
        self.assertIn("Daily Nonprofit Funding Report", result["subject"])


if __name__ == "__main__":
    unittest.main()
