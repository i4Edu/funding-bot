import os
import unittest
from pathlib import Path

from celery_app import (
    DEFAULT_CELERY_BROKER_URL,
    DEFAULT_CELERY_RESULT_BACKEND,
    DEFAULT_RABBITMQ_BROKER_URL,
    celery_app,
    get_celery_config,
)
from funding_bot import FundingBot
from tasks.celery_tasks import (
    enforce_data_retention_task,
    export_data_warehouse_task,
    run_discovery_task,
    send_daily_summary_task,
)


class CeleryConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(".test_celery.db")
        if self.db_path.exists():
            self.db_path.unlink()

    def tearDown(self):
        if self.db_path.exists():
            self.db_path.unlink()

    def test_default_config_uses_local_filesystem_broker(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            config = get_celery_config()

        self.assertEqual(DEFAULT_CELERY_BROKER_URL, config["broker_url"])
        self.assertEqual(DEFAULT_CELERY_RESULT_BACKEND, config["result_backend"])
        self.assertEqual(("tasks.celery_tasks",), config["imports"])

    def test_rabbitmq_broker_override_is_supported(self):
        env = {
            "CELERY_BROKER_URL": DEFAULT_RABBITMQ_BROKER_URL,
            "CELERY_RESULT_BACKEND": "rpc://",
        }
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            config = get_celery_config()

        self.assertEqual(DEFAULT_RABBITMQ_BROKER_URL, config["broker_url"])
        self.assertEqual("rpc://", config["result_backend"])

    def test_module_level_app_imports_task_module_and_beat_schedule(self):
        self.assertEqual(("tasks.celery_tasks",), celery_app.conf.imports)
        self.assertIn("daily-summary", celery_app.conf.beat_schedule)
        self.assertIn("warehouse-export", celery_app.conf.beat_schedule)
        self.assertIn("data-retention-cleanup", celery_app.conf.beat_schedule)
        scheduled = celery_app.conf.beat_schedule["daily-summary"]
        self.assertEqual("funding_bot.send_daily_summary", scheduled["task"])
        self.assertEqual("lupael@i4e.com.bd", scheduled["kwargs"]["recipient"])
        self.assertEqual(
            "funding_bot.export_data_warehouse",
            celery_app.conf.beat_schedule["warehouse-export"]["task"],
        )
        self.assertEqual(
            "funding_bot.enforce_data_retention",
            celery_app.conf.beat_schedule["data-retention-cleanup"]["task"],
        )

    def test_discovery_task_persists_opportunities(self):
        result = run_discovery_task.run(
            db_path=str(self.db_path),
            keywords=["education"],
            trusted_sources=["Grants Portal", "CSR Network", "NGO Directory"],
            idempotency_key="celery-config-discovery",
        )

        self.assertEqual(1, result["count"])
        bot = FundingBot(db_path=self.db_path)
        try:
            self.assertEqual(1, len(bot.list_opportunities()))
            task_run = bot.get_task_run("celery-config-discovery")
            self.assertEqual("completed", task_run["status"])
        finally:
            bot.close()

    def test_daily_summary_task_supports_dry_run(self):
        bot = FundingBot(db_path=self.db_path)
        try:
            bot.store_organization_profile(
                {
                    "name": "i4Edu",
                    "mission": "Expand access to equitable education.",
                    "registration_number": "NP-42",
                }
            )
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
            idempotency_key="celery-config-summary",
        )
        self.assertTrue(result["dry_run"])
        self.assertEqual("ops@example.org", result["recipient"])
        self.assertIn("Daily Nonprofit Funding Report", result["subject"])

    def test_export_and_retention_tasks_are_importable(self):
        self.assertEqual("funding_bot.export_data_warehouse", export_data_warehouse_task.name)
        self.assertEqual("funding_bot.enforce_data_retention", enforce_data_retention_task.name)


if __name__ == "__main__":
    unittest.main()
