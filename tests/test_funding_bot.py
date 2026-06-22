import io
import json
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


from funding_bot import (
    CSRNetworkConnector,
    FileVault,
    GrantsPortalConnector,
    NGODirectoryConnector,
    main,
)


class PortalConnectorTests(unittest.TestCase):
    """Tests for stub portal connector implementations."""

    def test_stub_connectors_return_demo_records_and_filter_by_keyword(self):
        grants = GrantsPortalConnector()
        csr = CSRNetworkConnector()
        ngo = NGODirectoryConnector()

        grants_rows = grants.fetch_opportunities(["education"])
        csr_rows = csr.fetch_opportunities(["digital learning"])
        ngo_rows = ngo.fetch_opportunities(["literacy"])

        self.assertEqual(1, len(grants_rows))
        self.assertEqual("Grants Portal", grants_rows[0]["source"])
        self.assertEqual("Education Innovation Grant", grants_rows[0]["title"])

        self.assertEqual(1, len(csr_rows))
        self.assertEqual("CSR Network", csr_rows[0]["source"])
        self.assertEqual("CSR Digital Learning Fund", csr_rows[0]["title"])

        self.assertEqual(1, len(ngo_rows))
        self.assertEqual("NGO Directory", ngo_rows[0]["source"])
        self.assertEqual("Community Literacy Matching Grant", ngo_rows[0]["title"])

        self.assertEqual([], grants.fetch_opportunities(["health"]))


class DonorSegmentationAndTemplateTests(unittest.TestCase):
    """Tests donor segmentation and outreach templates."""

    def setUp(self):
        self.bot = FundingBot(trusted_sources={"Grants Portal"})
        self.bot.store_organization_profile(
            {"name": "i4Edu", "mission": "Expand access to equitable education."}
        )

    def tearDown(self):
        self.bot.close()

    def test_upsert_donor_persists_segment_and_list_donors_filters(self):
        self.bot.upsert_donor(
            email="corp@example.org",
            name="Corporate Donor",
            segment="corporate",
        )
        self.bot.upsert_donor(
            email="inst@example.org",
            name="Institutional Donor",
            segment="institutional",
        )
        self.bot.upsert_donor(
            email="corp@example.org",
            name="Corporate Donor Updated",
        )

        corporate = self.bot.list_donors(segment="corporate")
        all_donors = self.bot.list_donors()

        self.assertEqual(1, len(corporate))
        self.assertEqual("Corporate Donor Updated", corporate[0]["name"])
        self.assertEqual("corporate", corporate[0]["segment"])
        self.assertEqual({"corporate", "institutional"}, {row["segment"] for row in all_donors})
        latest_upsert = self.bot.connection.execute(
            "SELECT details_json FROM audit_logs WHERE action = 'donor_upserted' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual("corporate", json.loads(latest_upsert["details_json"])["segment"])

    def test_send_outreach_from_template_prefers_segment_template_and_falls_back(self):
        calls = []

        def fake_sender(to_addr, subject, body):
            calls.append({"to": to_addr, "subject": subject, "body": body})

        self.bot.upsert_donor(
            email="corp@example.org",
            name="Corporate Donor",
            segment="corporate",
        )
        self.bot.upsert_donor(
            email="unknown@example.org",
            name="Unknown Donor",
        )
        self.bot.register_outreach_template(
            "intro",
            "Support {organization_name}",
            "Hello {donor_name},\n\n{mission}",
        )
        self.bot.register_outreach_template(
            "intro",
            "Corporate partnership with {organization_name}",
            "Dear {donor_name},\n\nLet us discuss CSR support for {organization_name}.",
            segment="corporate",
        )

        corporate_result = self.bot.send_outreach_from_template(
            "intro",
            "corp@example.org",
            "Corporate Donor",
            sender=fake_sender,
            sent_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
        )
        fallback_result = self.bot.send_outreach_from_template(
            "intro",
            "unknown@example.org",
            "Unknown Donor",
            sender=fake_sender,
            sent_at=datetime(2026, 6, 23, tzinfo=timezone.utc),
        )

        self.assertIn("Corporate partnership", corporate_result["subject"])
        self.assertIn("CSR support", corporate_result["body"])
        self.assertEqual("Support i4Edu", fallback_result["subject"])
        self.assertIn("Expand access to equitable education.", fallback_result["body"])
        self.assertEqual(["corp@example.org", "unknown@example.org"], [call["to"] for call in calls])


class OutreachAnalyticsAndGdprTests(unittest.TestCase):
    """Tests outreach analytics reporting and GDPR workflows."""

    def setUp(self):
        self.bot = FundingBot(trusted_sources={"Grants Portal"})
        self.bot.store_organization_profile(
            {"name": "i4Edu", "mission": "Expand access to equitable education."}
        )

    def tearDown(self):
        self.bot.close()

    def test_outreach_events_analytics_and_report(self):
        self.bot.send_outreach(
            donor_email="engaged@example.org",
            donor_name="Engaged Donor",
            subject_template="Support {organization_name}",
            body_template="Hello {donor_name}",
            sent_at=datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc),
        )
        self.bot.send_outreach(
            donor_email="bounce@example.org",
            donor_name="Bounce Donor",
            subject_template="Support {organization_name}",
            body_template="Hello {donor_name}",
            sent_at=datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc),
        )

        rows = self.bot.connection.execute(
            "SELECT id, donor_email FROM communications ORDER BY id"
        ).fetchall()
        communication_ids = {row["donor_email"]: row["id"] for row in rows}

        self.bot.record_outreach_event(communication_ids["engaged@example.org"], "opened")
        self.bot.record_outreach_event(communication_ids["engaged@example.org"], "clicked")
        self.bot.record_outreach_event(communication_ids["bounce@example.org"], "bounced")

        all_analytics = self.bot.get_outreach_analytics()
        donor_analytics = self.bot.get_outreach_analytics("engaged@example.org")
        report = self.bot.build_outreach_analytics_report("2026-06-22", "2026-06-22")

        self.assertEqual(2, all_analytics["sent"])
        self.assertEqual(1, all_analytics["opened"])
        self.assertEqual(1, all_analytics["clicked"])
        self.assertEqual(1, all_analytics["bounced"])

        self.assertEqual(1, donor_analytics["sent"])
        self.assertEqual(1, donor_analytics["opened"])
        self.assertEqual(1, donor_analytics["clicked"])
        self.assertEqual(0, donor_analytics["bounced"])

        self.assertEqual(2, report["total_sent"])
        self.assertEqual(1, report["opened"])
        self.assertEqual(1, report["clicked"])
        self.assertEqual(0.5, report["bounce_rate"])
        self.assertEqual("engaged@example.org", report["top_engaged_donors"][0]["donor_email"])

    def test_outreach_analytics_report_orders_ties_by_latest_sent(self):
        self.bot.send_outreach(
            donor_email="earlier@example.org",
            donor_name="Earlier Donor",
            subject_template="Support {organization_name}",
            body_template="Hello {donor_name}",
            sent_at=datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc),
        )
        self.bot.send_outreach(
            donor_email="later@example.org",
            donor_name="Later Donor",
            subject_template="Support {organization_name}",
            body_template="Hello {donor_name}",
            sent_at=datetime(2026, 6, 22, 11, 0, tzinfo=timezone.utc),
        )

        rows = self.bot.connection.execute(
            "SELECT id, donor_email FROM communications ORDER BY id"
        ).fetchall()
        communication_ids = {row["donor_email"]: row["id"] for row in rows}
        self.bot.record_outreach_event(communication_ids["earlier@example.org"], "opened")
        self.bot.record_outreach_event(communication_ids["later@example.org"], "opened")

        report = self.bot.build_outreach_analytics_report("2026-06-22", "2026-06-22")

        self.assertEqual("later@example.org", report["top_engaged_donors"][0]["donor_email"])

    def test_gdpr_export_and_delete_cover_related_records(self):
        self.bot.upsert_donor(
            email="privacy@example.org",
            name="Privacy Donor",
            preferences={"frequency": "monthly"},
            segment="individual",
        )
        self.bot.send_outreach(
            donor_email="privacy@example.org",
            donor_name="Privacy Donor",
            subject_template="Support {organization_name}",
            body_template="Hello {donor_name}",
            sent_at=datetime(2026, 6, 22, 11, 0, tzinfo=timezone.utc),
        )
        communication_id = self.bot.connection.execute(
            "SELECT id FROM communications WHERE donor_email = ?",
            ("privacy@example.org",),
        ).fetchone()["id"]
        self.bot.record_outreach_event(communication_id, "opened")

        export = self.bot.gdpr_export("privacy@example.org")

        self.assertEqual("Privacy Donor", export["donor"]["name"])
        self.assertEqual("individual", export["donor"]["segment"])
        self.assertEqual(1, len(export["communications"]))
        self.assertEqual(
            {"sent", "opened"},
            {row["event_type"] for row in export["outreach_events"]},
        )
        self.assertTrue(export["audit_logs"])

        self.bot.gdpr_delete("privacy@example.org")

        donor_row = self.bot.connection.execute("SELECT * FROM donors").fetchone()
        communication_row = self.bot.connection.execute(
            "SELECT donor_email, donor_name, subject, body FROM communications"
        ).fetchone()
        audit_log_text = "\n".join(
            row["details_json"]
            for row in self.bot.connection.execute(
                "SELECT details_json FROM audit_logs ORDER BY id"
            ).fetchall()
        )

        self.assertEqual("[deleted]", donor_row["name"])
        self.assertEqual("unknown", donor_row["segment"])
        self.assertEqual(1, donor_row["opted_out"])
        self.assertTrue(donor_row["email"].endswith("@deleted.invalid"))
        self.assertEqual("[deleted]", communication_row["donor_name"])
        self.assertEqual("[deleted]", communication_row["subject"])
        self.assertEqual("[deleted]", communication_row["body"])
        self.assertNotIn("privacy@example.org", audit_log_text)
        self.assertNotIn("Privacy Donor", audit_log_text)


class StatusPollingAndVaultTests(unittest.TestCase):
    """Tests status polling and credential vault integrations."""

    def setUp(self):
        self.vault_dir = Path(".test_file_vault")
        if self.vault_dir.exists():
            for path in self.vault_dir.iterdir():
                path.unlink()
            self.vault_dir.rmdir()
        self.vault_dir.mkdir()
        self.bot = FundingBot(trusted_sources={"Grants Portal"}, vault=FileVault(self.vault_dir))
        self.bot.store_organization_profile({"name": "i4Edu"})

    def tearDown(self):
        self.bot.close()
        if self.vault_dir.exists():
            for path in self.vault_dir.iterdir():
                path.unlink()
            self.vault_dir.rmdir()

    def _discover_sample_opportunity(self):
        return self.bot.discover_opportunities(
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
        )[0]["signature"]

    def test_file_vault_resolves_registered_credentials(self):
        (self.vault_dir / "PORTAL_SECRET").write_text(
            '{"username": "vault-user", "password": "vault-pass"}',
            encoding="utf-8",
        )
        self.bot.register_credential("portal", "PORTAL_SECRET")

        credentials = self.bot.resolve_credential("portal")

        self.assertEqual("vault-user", credentials["username"])
        self.assertEqual("vault-pass", credentials["password"])

    def test_poll_application_status_uses_http_client_and_updates_records(self):
        signature = self._discover_sample_opportunity()
        self.bot.submit_application(
            signature,
            submission_reference="sub-123",
            status="submitted",
            next_action="Await donor review",
        )
        calls = []

        def fake_http_client(url, payload):
            calls.append({"url": url, "payload": payload})
            return {
                "status": "approved",
                "next_action": "Prepare grant agreement",
            }

        result = self.bot.poll_application_status(signature, fake_http_client)

        self.assertTrue(result["changed"])
        self.assertEqual("approved", result["status"])
        self.assertEqual("Prepare grant agreement", result["next_action"])
        self.assertEqual(
            "https://example.org/unicef/status",
            calls[0]["url"],
        )
        self.assertEqual(signature, calls[0]["payload"]["opportunity_signature"])
        self.assertEqual("sub-123", calls[0]["payload"]["submission_reference"])
        self.assertEqual("approved", self.bot.list_opportunities()[0]["status"])


class ProposalDraftingTests(unittest.TestCase):
    """Tests AI-assisted proposal drafting."""

    def setUp(self):
        self.bot = FundingBot(trusted_sources={"Grants Portal"})
        self.bot.store_organization_profile(
            {"name": "i4Edu", "mission": "Expand access to equitable education."}
        )
        self.signature = self.bot.discover_opportunities(
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
        )[0]["signature"]

    def tearDown(self):
        self.bot.close()

    def test_draft_proposal_without_ai_uses_template_fallback(self):
        proposal = self.bot.draft_proposal(self.signature)

        self.assertIn("# Proposal Draft: UNICEF CSR Grant", proposal)
        self.assertIn("## Executive Summary", proposal)
        self.assertIn("## Organizational Fit", proposal)
        self.assertIn("## Program Plan", proposal)
        self.assertIn("## Expected Outcomes", proposal)
        self.assertIn("## Compliance Notes", proposal)

    def test_draft_proposal_with_ai_client_uses_generated_text(self):
        class FakeAIClient:
            def __init__(self):
                self.prompts = []

            def generate(self, prompt):
                self.prompts.append(prompt)
                return "AI proposal draft"

        ai_client = FakeAIClient()

        proposal = self.bot.draft_proposal(self.signature, ai_client=ai_client)

        self.assertEqual("AI proposal draft", proposal)
        self.assertEqual(1, len(ai_client.prompts))
        self.assertIn("Organization profile:", ai_client.prompts[0])
        self.assertIn("UNICEF CSR Grant", ai_client.prompts[0])


class CliExtensionTests(unittest.TestCase):
    """Tests CLI list commands added in newer versions."""

    def setUp(self):
        self.db_path = Path(".test_cli_commands.db")
        if self.db_path.exists():
            self.db_path.unlink()
        bot = FundingBot(db_path=self.db_path, trusted_sources={"Grants Portal"})
        bot.store_organization_profile({"name": "i4Edu"})
        bot.discover_opportunities(
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
        )
        bot.upsert_donor(
            email="corp@example.org",
            name="Corporate Donor",
            segment="corporate",
        )
        bot.close()

    def tearDown(self):
        if self.db_path.exists():
            self.db_path.unlink()

    def test_list_opportunities_command_prints_rows(self):
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            main(["--db", str(self.db_path), "list-opportunities", "--status", "new", "--limit", "1"])

        output = stdout.getvalue()
        self.assertIn("signature\tsource\tdonor_name\ttitle\tstatus\tdiscovered_at", output)
        self.assertIn("UNICEF CSR Grant", output)

    def test_audit_log_command_prints_filtered_rows(self):
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            main(["--db", str(self.db_path), "audit-log", "--action", "donor_upserted", "--limit", "5"])

        output = stdout.getvalue()
        self.assertIn("happened_at\taction\tdetails_json", output)
        self.assertIn("donor_upserted", output)
        self.assertIn("corp@example.org", output)

    def test_list_donors_command_prints_segment_filter(self):
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            main(["--db", str(self.db_path), "list-donors", "--segment", "corporate"])

        output = stdout.getvalue()
        self.assertIn("email\tname\tsegment\topted_out\tlast_contact_at", output)
        self.assertIn("corp@example.org\tCorporate Donor\tcorporate", output)


if __name__ == "__main__":
    unittest.main()
