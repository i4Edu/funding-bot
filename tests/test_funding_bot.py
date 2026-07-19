import io
import json
import os
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from shutil import rmtree
from zipfile import ZipFile

from funding_bot import (
    DuplicateSubmissionError,
    FundingBot,
    FundingBotError,
    OptOutError,
    OutreachThrottledError,
    SMTPEmailSender,
    TaskTransitionError,
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


class FakeClock:
    def __init__(self):
        self.current = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.current

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.current += seconds


class FundingBotTests(unittest.TestCase):
    def setUp(self):
        self.bot = FundingBot(trusted_sources={"Grants Portal", "CSR Network"})
        self.output_dir = Path(".test_document_outputs")
        if self.output_dir.exists():
            rmtree(self.output_dir)
        self.bot.store_organization_profile(
            {
                "name": "i4Edu",
                "mission": "Expand access to equitable education.",
                "registration_number": "NP-42",
            }
        )

    def tearDown(self):
        self.bot.close()
        if self.output_dir.exists():
            rmtree(self.output_dir)
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

    def test_list_tasks_filters_by_assignee(self):
        self.bot.create_task(title="Prepare budget", assigned_to="staff")
        self.bot.create_task(title="Review risk log", assigned_to="auditor")

        staff_tasks = self.bot.list_tasks(assigned_to="staff")

        self.assertEqual(1, len(staff_tasks))
        self.assertEqual("Prepare budget", staff_tasks[0]["title"])
        self.assertEqual("staff", staff_tasks[0]["assigned_to"])

    def test_task_status_transitions_follow_state_machine(self):
        task = self.bot.create_task(title="Prepare proposal draft", assigned_to="staff")

        in_progress = self.bot.transition_task_status(
            task["id"],
            new_status="in-progress",
            changed_by="staff",
        )
        done = self.bot.transition_task_status(
            task["id"],
            new_status="done",
            changed_by="staff",
        )

        self.assertEqual("in-progress", in_progress["status"])
        self.assertEqual("done", done["status"])
        counts = self.bot.get_task_status_counts(assigned_to="staff")
        self.assertEqual(1, counts["done"])

    def test_task_status_transition_validation_rejects_invalid_change(self):
        task = self.bot.create_task(title="Collect attachments", assigned_to="staff")

        with self.assertRaises(TaskTransitionError):
            self.bot.transition_task_status(
                task["id"],
                new_status="done",
                changed_by="staff",
            )

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

        documents = self.bot.generate_document(
            kind="cover_letter",
            template=(
                "{name}\nMission: {mission}\nRegistration: {registration_number}\n"
                "Opportunity: UNICEF CSR Grant"
            ),
            output_dir=self.output_dir,
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

    def test_generate_document_falls_back_to_english_translations(self):
        documents = self.bot.generate_document(
            kind="cover_letter",
            template="{t[greeting]}\nBudget: {budget}\nDate: {report_date}",
            output_dir=self.output_dir,
            formats=("docx",),
            locale="bn",
            context={
                "budget": 1250000,
                "report_date": datetime(2026, 7, 19, 9, 30, tzinfo=timezone.utc),
                "translations": {
                    "en": {"greeting": "Dear Review Committee"},
                    "bn": {},
                },
            },
        )

        with ZipFile(documents["docx"]) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8")

        self.assertIn("Dear Review Committee", document_xml)
        self.assertIn("12,50,000", document_xml)
        self.assertIn("19/07/2026 09:30", document_xml)

    def test_generate_document_formats_english_locale_values(self):
        documents = self.bot.generate_document(
            kind="cover_letter",
            template="{t[greeting]}\nBudget: {budget}\nDate: {report_date}",
            output_dir=self.output_dir,
            formats=("docx",),
            locale="en",
            context={
                "budget": 1250000.5,
                "report_date": datetime(2026, 7, 19, 9, 30, tzinfo=timezone.utc),
                "translations": {
                    "en": {"greeting": "Dear Review Committee"},
                    "bn": {"greeting": "প্রিয় পর্যালোচনা কমিটি"},
                },
            },
        )

        with ZipFile(documents["docx"]) as archive:
            document_xml = archive.read("word/document.xml").decode("utf-8")

        self.assertIn("Dear Review Committee", document_xml)
        self.assertIn("1,250,000.5", document_xml)
        self.assertIn("07/19/2026 09:30", document_xml)


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
    create_connector,
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

    def test_connector_keyword_mappings_expand_synonyms_and_categories(self):
        self.assertEqual(
            "Education Innovation Grant",
            GrantsPortalConnector().fetch_opportunities(["learning"])[0]["title"],
        )
        self.assertEqual(
            "CSR Digital Learning Fund",
            CSRNetworkConnector().fetch_opportunities(["corporate social responsibility"])[0]["title"],
        )
        self.assertEqual(
            "Community Literacy Matching Grant",
            NGODirectoryConnector().fetch_opportunities(["community engagement"])[0]["title"],
        )

    def test_connector_validation_reports_available_keyword_mappings(self):
        validation = create_connector("csr-network").validate_connectivity(["edtech"])

        self.assertEqual("ok", validation["status"])
        self.assertTrue(validation["connectivity_validated"])
        self.assertIn("corporate partnerships", validation["expanded_keywords"])
        self.assertEqual(
            ["csr", "corporate social responsibility", "corporate giving"],
            validation["keyword_mappings"]["csr"]["keywords"],
        )

    def test_connector_retries_transient_errors_with_exponential_backoff(self):
        clock = FakeClock()
        calls = []

        def flaky_http_client(url, payload):
            calls.append((url, payload))
            if len(calls) < 3:
                raise TimeoutError("temporary timeout")
            return {
                "opportunities": [
                    {
                        "source": "Grants Portal",
                        "donor_name": "Recovered Donor",
                        "title": "Recovered Opportunity",
                        "portal_url": "https://grants.example.org/recovered",
                        "summary": "Recovered after retries.",
                        "category": "Education",
                        "tags": ["education"],
                    }
                ]
            }

        connector = GrantsPortalConnector(
            http_client=flaky_http_client,
            max_retries=2,
            retry_backoff_base=1.0,
            retry_backoff_factor=2.0,
            sleep_func=clock.sleep,
            time_func=clock.monotonic,
        )

        rows = connector.fetch_opportunities(["education"])
        metrics = connector.get_failure_metrics()

        self.assertEqual(1, len(rows))
        self.assertEqual("Recovered Opportunity", rows[0]["title"])
        self.assertEqual([1.0, 2.0], clock.sleeps)
        self.assertEqual(2, metrics["retry_attempts"])
        self.assertEqual(1, metrics["successful_requests"])
        self.assertEqual("closed", metrics["state"])

    def test_connector_opens_circuit_and_gracefully_degrades_after_repeated_failures(self):
        clock = FakeClock()

        def failing_http_client(url, payload):
            raise ConnectionError("connector offline")

        connector = GrantsPortalConnector(
            http_client=failing_http_client,
            max_retries=0,
            circuit_failure_threshold=2,
            circuit_recovery_timeout=10.0,
            sleep_func=clock.sleep,
            time_func=clock.monotonic,
        )

        self.assertEqual([], connector.fetch_opportunities(["education"]))
        first_metrics = connector.get_failure_metrics()
        self.assertEqual("closed", first_metrics["state"])

        self.assertEqual([], connector.fetch_opportunities(["education"]))
        opened_metrics = connector.get_failure_metrics()
        self.assertEqual("open", opened_metrics["state"])
        self.assertEqual(2, opened_metrics["failed_requests"])

        self.assertEqual([], connector.fetch_opportunities(["education"]))
        degraded_metrics = connector.get_failure_metrics()
        self.assertEqual(1, degraded_metrics["short_circuits"])
        self.assertEqual("connector offline", degraded_metrics["last_error"])

    def test_connector_transitions_open_to_half_open_to_closed(self):
        clock = FakeClock()
        should_fail = {"value": True}

        def recovering_http_client(url, payload):
            if should_fail["value"]:
                raise OSError("temporary network issue")
            return {
                "opportunities": [
                    {
                        "source": "Grants Portal",
                        "donor_name": "Healthy Donor",
                        "title": "Healthy Opportunity",
                        "portal_url": "https://grants.example.org/healthy",
                        "summary": "Healthy again.",
                        "category": "Education",
                        "tags": ["education"],
                    }
                ]
            }

        connector = GrantsPortalConnector(
            http_client=recovering_http_client,
            max_retries=0,
            circuit_failure_threshold=1,
            circuit_recovery_timeout=5.0,
            sleep_func=clock.sleep,
            time_func=clock.monotonic,
        )

        self.assertEqual([], connector.fetch_opportunities(["education"]))
        self.assertEqual("open", connector.get_failure_metrics()["state"])

        clock.current = 5.0
        health = connector.check_health()
        self.assertTrue(health["healthy"])
        self.assertEqual("half-open", health["state"])

        should_fail["value"] = False
        rows = connector.fetch_opportunities(["education"])
        self.assertEqual(1, len(rows))
        self.assertEqual("closed", connector.get_failure_metrics()["state"])

    def test_run_discovery_skips_unhealthy_connector_and_uses_healthy_ones(self):
        clock = FakeClock()

        def failing_http_client(url, payload):
            raise ConnectionError("grants unavailable")

        unhealthy = GrantsPortalConnector(
            http_client=failing_http_client,
            max_retries=0,
            circuit_failure_threshold=1,
            circuit_recovery_timeout=30.0,
            sleep_func=clock.sleep,
            time_func=clock.monotonic,
        )
        healthy = CSRNetworkConnector()

        self.assertEqual([], unhealthy.fetch_opportunities(["csr"]))

        bot = FundingBot(trusted_sources={"Grants Portal", "CSR Network"})
        try:
            found = bot.run_discovery([unhealthy, healthy], keywords=["csr"])
        finally:
            bot.close()

        self.assertEqual(1, len(found))
        self.assertEqual("CSR Network", found[0]["source"])


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
            locale="bn",
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
        self.assertEqual("bn", corporate[0]["locale"])
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

    def test_send_outreach_from_template_selects_supported_locale_catalogs(self):
        self.bot.upsert_donor(
            email="bangla@example.org",
            name="বাংলা দাতা",
            segment="corporate",
            locale="bn",
        )
        self.bot.upsert_donor(
            email="english@example.org",
            name="English Donor",
            locale="en",
        )

        bangla_result = self.bot.send_outreach_from_template(
            "default",
            "bangla@example.org",
            "বাংলা দাতা",
            sent_at=datetime(2026, 6, 24, tzinfo=timezone.utc),
        )
        english_result = self.bot.send_outreach_from_template(
            "intro",
            "english@example.org",
            "English Donor",
            sent_at=datetime(2026, 6, 25, tzinfo=timezone.utc),
        )

        self.assertIn("ধন্যবাদ", bangla_result["subject"])
        self.assertIn("ভবিষ্যতের যোগাযোগ বন্ধ করতে", bangla_result["body"])
        self.assertIn("Support i4Edu", english_result["subject"])
        self.assertIn("Expand access to equitable education.", english_result["body"])


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

    def test_monthly_audit_report_command_prints_json_to_stdout(self):
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            main(["--db", str(self.db_path), "monthly-audit-report", "--year", "2026", "--month", "6"])

        report = json.loads(stdout.getvalue())
        self.assertEqual("monthly_compliance_audit", report["report_type"])
        self.assertEqual("2026-06", report["period"])

    def test_monthly_audit_report_command_writes_output_file_with_missing_parent_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "reports" / "2026-06-audit.json"
            with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
                main(
                    [
                        "--db",
                        str(self.db_path),
                        "monthly-audit-report",
                        "--year",
                        "2026",
                        "--month",
                        "6",
                        "--output",
                        str(output_path),
                    ]
                )

            self.assertTrue(output_path.exists())
            report = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual("2026-06", report["period"])
            self.assertIn(str(output_path), stdout.getvalue())


class MonthlyAuditReportTests(unittest.TestCase):
    def setUp(self):
        self.bot = FundingBot(trusted_sources={"Grants Portal"})
        self.bot.store_organization_profile({"name": "i4Edu"})

    def tearDown(self):
        self.bot.close()

    def test_build_monthly_audit_report_summarizes_expected_sections(self):
        self.bot.connection.execute(
            """
            INSERT INTO audit_logs (happened_at, action, details_json)
            VALUES (?, 'gdpr_exported', ?)
            """,
            ("2026-06-10T12:00:00+00:00", json.dumps({"subject_email": "privacy@example.org"})),
        )
        self.bot.connection.execute(
            """
            INSERT INTO audit_logs (happened_at, action, details_json)
            VALUES (?, 'donor_upserted', ?)
            """,
            ("2026-06-12T10:00:00+00:00", json.dumps({"email": "new@example.org"})),
        )
        self.bot.connection.execute(
            """
            INSERT INTO donors (email, name, segment, opted_out)
            VALUES (?, ?, ?, ?)
            """,
            ("optout@example.org", "Opt Out", "unknown", 1),
        )
        signature = self.bot.discover_opportunities(
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
        self.bot.submit_application(
            signature,
            submission_reference="ref-1",
            status="submitted",
            next_action="Await donor review",
            submitted_at=datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc),
        )
        self.bot.connection.commit()

        report = self.bot.build_monthly_audit_report(year=2026, month=6)

        self.assertEqual("monthly_compliance_audit", report["report_type"])
        self.assertEqual("2026-06", report["period"])
        self.assertEqual(1, report["audit_log_entries"]["gdpr_exported"])
        self.assertEqual(1, report["gdpr_operations"]["gdpr_exported"])
        self.assertEqual(1, report["application_outcomes"]["submitted"])
        self.assertEqual(1, report["new_donors_registered"])
        self.assertEqual(1, report["opted_out_donors_total"])
        latest_audit = self.bot.connection.execute(
            "SELECT action, details_json FROM audit_logs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual("monthly_audit_report_generated", latest_audit["action"])
        self.assertEqual("2026-06", json.loads(latest_audit["details_json"])["period"])


class SearchSettingsAndDiscoveryTests(unittest.TestCase):
    """Tests for persisted settings and end-to-end discovery orchestration."""

    def setUp(self):
        self.bot = FundingBot(db_path=":memory:")

    def tearDown(self):
        self.bot.close()

    def test_store_and_load_search_settings(self):
        saved = self.bot.store_search_settings(
            keywords=[" Education ", "csr", "csr"],
            trusted_sources=["Grants Portal", ""],
        )
        self.assertEqual(["Education", "csr"], saved["keywords"])
        self.assertEqual(["Grants Portal"], saved["trusted_sources"])

        loaded = self.bot.load_search_settings()
        self.assertEqual(saved, loaded)

    def test_load_search_settings_defaults_when_unset(self):
        self.assertEqual({"keywords": [], "trusted_sources": []}, self.bot.load_search_settings())

    def test_register_credential_and_list_credentials(self):
        self.bot.register_credential("smtp", "SMTP_PASSWORD")
        self.assertEqual(
            [{"alias": "smtp", "env_var_name": "SMTP_PASSWORD"}],
            self.bot.list_credentials(),
        )

    def test_store_organization_profile_round_trips_through_generic_settings(self):
        self.bot.store_organization_profile({"name": "i4Edu", "mission": "Educate"})
        self.assertEqual({"name": "i4Edu", "mission": "Educate"}, self.bot.load_organization_profile())
        # It is addressable via the generic setting API too.
        self.assertEqual({"name": "i4Edu", "mission": "Educate"}, self.bot.load_setting("profile"))

    def test_run_discovery_uses_connectors_and_persists_new_opportunities(self):
        found = self.bot.run_discovery(
            [GrantsPortalConnector(), CSRNetworkConnector(), NGODirectoryConnector()],
            keywords=["education"],
        )
        self.assertEqual(1, len(found))
        self.assertEqual("Education Innovation Grant", found[0]["title"])

        stored = self.bot.list_opportunities()
        self.assertEqual(1, len(stored))

        # Re-running should not duplicate the already-discovered opportunity.
        found_again = self.bot.run_discovery(
            [GrantsPortalConnector(), CSRNetworkConnector(), NGODirectoryConnector()],
            keywords=["education"],
        )
        self.assertEqual([], found_again)

    def test_run_discovery_falls_back_to_stored_search_settings(self):
        self.bot.store_search_settings(keywords=["csr"])
        found = self.bot.run_discovery([GrantsPortalConnector(), CSRNetworkConnector()])
        self.assertEqual(1, len(found))
        self.assertEqual("CSR Digital Learning Fund", found[0]["title"])

    def test_run_discovery_defaults_to_builtin_connectors(self):
        found = self.bot.run_discovery(keywords=["literacy"])
        self.assertEqual(1, len(found))
        self.assertEqual("NGO Directory", found[0]["source"])


class CliSearchAndSettingsCommandsTests(unittest.TestCase):
    """Tests for the settings/discovery/outreach CLI commands."""

    def setUp(self):
        self.db_path = Path(".test_cli_settings.db")
        if self.db_path.exists():
            self.db_path.unlink()

    def tearDown(self):
        if self.db_path.exists():
            self.db_path.unlink()

    def test_discover_command_prints_new_opportunities(self):
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            main(["--db", str(self.db_path), "discover", "--keywords", "education"])

        output = stdout.getvalue()
        self.assertIn("Education Innovation Grant", output)

    def test_discover_command_reports_no_results(self):
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            main(["--db", str(self.db_path), "discover", "--keywords", "no-such-keyword"])

        self.assertIn("No new opportunities found.", stdout.getvalue())

    def test_test_connector_command_prints_validation_and_sample_results(self):
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            main(
                [
                    "--db",
                    str(self.db_path),
                    "test-connector",
                    "--connector",
                    "csr-network",
                    "--keywords",
                    "edtech",
                    "--limit",
                    "1",
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual("csr-network", payload["connector"])
        self.assertEqual("ok", payload["status"])
        self.assertEqual(1, len(payload["sample_results"]))
        self.assertEqual("CSR Digital Learning Fund", payload["sample_results"][0]["title"])
        self.assertIn("corporate partnerships", payload["expanded_keywords"])

    def test_send_outreach_command_dry_run_does_not_send_but_logs(self):
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            main(
                [
                    "--db",
                    str(self.db_path),
                    "send-outreach",
                    "--email",
                    "donor@example.org",
                    "--name",
                    "Donor",
                    "--dry-run",
                ]
            )

        output = stdout.getvalue()
        self.assertIn("dry run", output)

        bot = FundingBot(db_path=self.db_path)
        try:
            communications = bot.connection.execute("SELECT * FROM communications").fetchall()
            self.assertEqual(1, len(communications))
            self.assertEqual("donor@example.org", communications[0]["donor_email"])
        finally:
            bot.close()

    def test_set_organization_profile_and_show_settings_commands(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_path = Path(tmpdir) / "profile.json"
            profile_path.write_text(json.dumps({"name": "i4Edu"}), encoding="utf-8")
            main(["--db", str(self.db_path), "set-organization-profile", "--file", str(profile_path)])

        main(["--db", str(self.db_path), "register-credential", "--alias", "smtp", "--env-var", "SMTP_PASSWORD"])

        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            main(["--db", str(self.db_path), "show-settings"])

        output = stdout.getvalue()
        json_blob, _, table_output = output.partition("Credential aliases")
        settings = json.loads(json_blob)
        self.assertEqual({"name": "i4Edu"}, settings["organization_profile"])
        self.assertIn("smtp", table_output)
        self.assertIn("SMTP_PASSWORD", table_output)


if __name__ == "__main__":
    unittest.main()
