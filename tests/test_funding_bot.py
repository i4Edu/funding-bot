import os
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from funding_bot import (
    DuplicateSubmissionError,
    FundingBot,
    FundingBotError,
    OptOutError,
    OutreachThrottledError,
    SMTPEmailSender,
)


class FakeBrowserClient:
    def __init__(self, failures_before_success=0):
        self.failures_before_success = failures_before_success
        self.calls = 0

    def submit(self, portal_url, credentials, form_data, attachments):
        self.calls += 1
        if self.calls <= self.failures_before_success:
            raise RuntimeError("temporary browser failure")
        return f"ref-{self.calls}"


class FundingBotTests(unittest.TestCase):
    def setUp(self):
        self.bot = FundingBot(trusted_sources={"Grants Portal", "CSR Network"})
        self.bot.store_organization_profile(
            {
                "name": "i4Edu",
                "mission": "Expand access to equitable education.",
                "registration_number": "NP-42",
            }
        )

    def tearDown(self):
        self.bot.close()
        os.environ.pop("PORTAL_CREDENTIALS", None)

    def _discover_sample_opportunity(self):
        found = self.bot.discover_opportunities(
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
            keywords=["csr funding", "nonprofit grants"],
        )
        self.assertEqual(1, len(found))
        return found[0]["signature"]

    def test_discovery_filters_and_deduplicates(self):
        signature = self._discover_sample_opportunity()
        second_run = self.bot.discover_opportunities(
            [
                {
                    "source": "Grants Portal",
                    "donor_name": "UNICEF",
                    "title": "UNICEF CSR Grant",
                    "portal_url": "https://example.org/unicef",
                    "summary": "CSR funding for nonprofit education programs.",
                    "tags": ["CSR funding", "nonprofit grants"],
                },
                {
                    "source": "Untrusted Source",
                    "donor_name": "Spam",
                    "title": "Untrusted listing",
                    "portal_url": "https://bad.example",
                    "summary": "nonprofit grants",
                    "tags": ["nonprofit grants"],
                },
            ],
            keywords=["nonprofit grants"],
        )
        self.assertEqual([], second_run)
        self.assertEqual(signature, self.bot.list_opportunities()[0]["signature"])

    def test_browser_submission_prevents_duplicates_and_tracks_success(self):
        signature = self._discover_sample_opportunity()
        os.environ["PORTAL_CREDENTIALS"] = (
            '{"username": "demo@example.org", "password": "not-a-real-secret"}'
        )
        self.bot.register_credential("unicef-portal", "PORTAL_CREDENTIALS")

        result = self.bot.submit_application_via_browser(
            signature,
            credential_alias="unicef-portal",
            browser_client=FakeBrowserClient(failures_before_success=1),
            form_data={"project_name": "Literacy Lab"},
            attachments=["/tmp/proposal.pdf"],
            max_retries=3,
        )
        self.assertEqual("submitted", result["status"])
        self.assertEqual("Await donor review", result["next_action"])

        with self.assertRaises(DuplicateSubmissionError):
            self.bot.submit_application(
                signature,
                submission_reference="second-ref",
                status="submitted",
                next_action="Should not happen",
            )

    def test_outreach_respects_opt_out_and_weekly_throttle(self):
        sent = self.bot.send_outreach(
            donor_email="donor@example.org",
            donor_name="Donor",
            subject_template="Support {organization_name}",
            body_template="Hello {donor_name},\n\n{mission}",
            context={"opt_out_url": "https://i4edu.org/opt-out"},
            sent_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
        )
        self.assertIn("https://i4edu.org/opt-out", sent["body"])

        with self.assertRaises(OutreachThrottledError):
            self.bot.send_outreach(
                donor_email="donor@example.org",
                donor_name="Donor",
                subject_template="Support {organization_name}",
                body_template="Hello {donor_name}",
                sent_at=datetime(2026, 6, 25, tzinfo=timezone.utc),
            )

        self.bot.upsert_donor(email="optout@example.org", name="Opt Out", opted_out=True)
        with self.assertRaises(OptOutError):
            self.bot.send_outreach(
                donor_email="optout@example.org",
                donor_name="Opt Out",
                subject_template="Support {organization_name}",
                body_template="Hello {donor_name}",
            )

    def test_outreach_normalizes_naive_sent_at_for_throttle(self):
        self.bot.send_outreach(
            donor_email="donor@example.org",
            donor_name="Donor",
            subject_template="Support {organization_name}",
            body_template="Hello {donor_name}",
            sent_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
        )

        with self.assertRaises(OutreachThrottledError):
            self.bot.send_outreach(
                donor_email="donor@example.org",
                donor_name="Donor",
                subject_template="Support {organization_name}",
                body_template="Hello {donor_name}",
                sent_at=datetime(2026, 6, 25),
            )

    def test_update_application_status_requires_existing_application(self):
        signature = self._discover_sample_opportunity()

        with self.assertRaises(FundingBotError):
            self.bot.update_application_status(
                signature,
                status="pending",
                next_action="Awaiting confirmation",
            )

        self.assertEqual("new", self.bot.list_opportunities()[0]["status"])

    def test_sqlite_foreign_keys_are_enabled(self):
        self.assertEqual(
            1,
            self.bot.connection.execute("PRAGMA foreign_keys").fetchone()[0],
        )

    def test_document_generation_and_daily_summary(self):
        signature = self._discover_sample_opportunity()
        self.bot.submit_application(
            signature,
            submission_reference="ref-1",
            status="submitted",
            next_action="Await donor review",
            submitted_at=datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc),
        )
        self.bot.send_outreach(
            donor_email="donor@example.org",
            donor_name="Donor",
            subject_template="Support {organization_name}",
            body_template="Hello {donor_name},\n\n{mission}",
            sent_at=datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc),
        )
        self.bot.update_application_status(
            signature,
            status="pending",
            next_action="Awaiting confirmation",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            documents = self.bot.generate_document(
                kind="cover_letter",
                template=(
                    "{name}\nMission: {mission}\nRegistration: {registration_number}\n"
                    "Opportunity: UNICEF CSR Grant"
                ),
                output_dir=tmpdir,
            )
            self.assertTrue(Path(documents["pdf"]).exists())
            self.assertTrue(Path(documents["docx"]).exists())

        summary = self.bot.build_daily_summary(
            recipient="lupael@i4e.com.bd",
            report_date=datetime(2026, 6, 22, 18, 0, tzinfo=timezone.utc),
        )
        self.assertIn("Daily Nonprofit Funding Report – 2026-06-22", summary["subject"])
        self.assertIn("UNICEF CSR Grant", summary["body"])
        self.assertIn("Pending Applications: 1", summary["body"])


class SMTPEmailSenderTests(unittest.TestCase):
    def test_from_env_reads_environment_variables(self):
        env = {
            "SMTP_HOST": "mail.example.org",
            "SMTP_PORT": "465",
            "SMTP_USERNAME": "bot@example.org",
            "SMTP_PASSWORD": "s3cr3t",
            "SMTP_USE_TLS": "1",
            "SMTP_FROM": "noreply@example.org",
        }
        with unittest.mock.patch.dict(os.environ, env, clear=False):
            sender = SMTPEmailSender.from_env()

        self.assertEqual("mail.example.org", sender.host)
        self.assertEqual(465, sender.port)
        self.assertEqual("bot@example.org", sender.username)
        self.assertEqual("s3cr3t", sender.password)
        self.assertTrue(sender.use_tls)
        self.assertEqual("noreply@example.org", sender.from_address)

    def test_from_env_defaults(self):
        # Remove relevant vars so we test defaults
        clean_env = {
            k: v for k, v in os.environ.items()
            if k not in ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
                         "SMTP_USE_TLS", "SMTP_FROM")
        }
        with unittest.mock.patch.dict(os.environ, clean_env, clear=True):
            sender = SMTPEmailSender.from_env()

        self.assertEqual("localhost", sender.host)
        self.assertEqual(587, sender.port)
        self.assertTrue(sender.use_tls)

    def test_from_env_tls_disabled(self):
        with unittest.mock.patch.dict(os.environ, {"SMTP_USE_TLS": "0"}, clear=False):
            sender = SMTPEmailSender.from_env()
        self.assertFalse(sender.use_tls)


class SendDailySummaryTests(unittest.TestCase):
    def setUp(self):
        self.bot = FundingBot(trusted_sources={"Grants Portal"})
        self.bot.store_organization_profile(
            {"name": "i4Edu", "mission": "Expand access to equitable education."}
        )

    def tearDown(self):
        self.bot.close()

    def test_send_daily_summary_calls_sender(self):
        calls = []

        def fake_sender(to_addr, subject, body):
            calls.append({"to": to_addr, "subject": subject, "body": body})

        summary = self.bot.send_daily_summary(
            recipient="lupael@i4e.com.bd",
            sender=fake_sender,
            report_date=datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(1, len(calls))
        self.assertEqual("lupael@i4e.com.bd", calls[0]["to"])
        self.assertIn("Daily Nonprofit Funding Report – 2026-06-22", calls[0]["subject"])
        self.assertEqual(summary["subject"], calls[0]["subject"])
        self.assertEqual(summary["body"], calls[0]["body"])

    def test_send_daily_summary_no_sender_returns_summary_without_sending(self):
        summary = self.bot.send_daily_summary(
            recipient="lupael@i4e.com.bd",
            report_date=datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc),
        )
        self.assertIn("Daily Nonprofit Funding Report", summary["subject"])
        self.assertIn("body", summary)

    def test_outreach_sender_callable_is_invoked(self):
        calls = []

        def fake_sender(to_addr, subject, body):
            calls.append(to_addr)

        self.bot.send_outreach(
            donor_email="donor@example.org",
            donor_name="Donor",
            subject_template="Support {organization_name}",
            body_template="Hello {donor_name}",
            sender=fake_sender,
            sent_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
        )

        self.assertEqual(["donor@example.org"], calls)


if __name__ == "__main__":
    unittest.main()

