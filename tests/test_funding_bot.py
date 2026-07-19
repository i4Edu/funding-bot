import io
import itertools
import json
import logging
import os
import signal
import types
import urllib.error
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
    GracefulShutdownController,
    GracefulShutdownRequested,
    OptOutError,
    OutreachThrottledError,
    SMTPEmailSender,
    Task,
    TaskTransitionError,
)
import task_queue


TEST_ARTIFACTS_DIR = Path(".test-artifacts")


def _reset_test_dir(name: str) -> Path:
    path = TEST_ARTIFACTS_DIR / name
    if path.exists():
        rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


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


class FakeHTTPResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class StaticSecretVault:
    def __init__(self, secrets):
        self.secrets = dict(secrets)

    def get_secret(self, name):
        return self.secrets[name]


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

    def test_database_pool_metrics_are_exposed(self):
        metrics = self.bot.get_database_pool_metrics()

        self.assertIn(metrics["backend"], {"sqlalchemy", "sqlite3"})
        self.assertIn("checkouts", metrics)
        self.assertIn("checked_out", metrics)
        self.assertIn("pool_class", metrics)

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
            attachments=[".test-artifacts/proposal.pdf"],
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

    def test_consent_records_track_outreach_and_opt_out_history(self):
        sent = self.bot.send_outreach(
            donor_email="consent@example.org",
            donor_name="Consent Donor",
            subject_template="Support {organization_name}",
            body_template="Hello {donor_name}",
            context={
                "consent_source": "donor_webform",
                "consent_proof": "checkbox",
                "consent_notes": "Captured from donor preferences form.",
            },
            sent_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
        )
        self.assertEqual("consent@example.org", sent["email"])

        consent_records = self.bot.list_consent_records("consent@example.org")
        self.assertEqual(1, len(consent_records))
        self.assertEqual("granted", consent_records[0]["status"])
        self.assertEqual("donor_webform", consent_records[0]["source"])
        self.assertEqual("checkbox", consent_records[0]["proof"])

        self.bot.set_donor_opt_out(
            "consent@example.org",
            True,
            source="unsubscribe_link",
            recorded_at=datetime(2026, 6, 29, tzinfo=timezone.utc),
            notes="Donor clicked unsubscribe.",
        )
        latest_record = self.bot.get_latest_consent_record("consent@example.org")
        self.assertIsNotNone(latest_record)
        self.assertEqual("withdrawn", latest_record["status"])
        self.assertEqual("unsubscribe_link", latest_record["source"])
        self.assertEqual("2026-06-29T00:00:00+00:00", latest_record["withdrawn_at"])

        export = self.bot.gdpr_export("consent@example.org")
        self.assertEqual(2, len(export["consent_records"]))

        with self.assertRaises(OptOutError):
            self.bot.send_outreach(
                donor_email="consent@example.org",
                donor_name="Consent Donor",
                subject_template="Support {organization_name}",
                body_template="Hello {donor_name}",
                sent_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
            )

    def test_gdpr_self_check_report_summarizes_retention_deletions_and_exports(self):
        self.bot.send_outreach(
            donor_email="archive@example.org",
            donor_name="Archive Donor",
            subject_template="Support {organization_name}",
            body_template="Hello {donor_name}",
            sent_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        self.bot.send_outreach(
            donor_email="privacy@example.org",
            donor_name="Privacy Donor",
            subject_template="Support {organization_name}",
            body_template="Hello {donor_name}",
            context={"consent_source": "crm_import"},
            sent_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
        )
        self.bot.gdpr_export("privacy@example.org")
        self.bot.set_donor_opt_out(
            "privacy@example.org",
            True,
            source="privacy_portal",
            recorded_at=datetime(2026, 6, 29, tzinfo=timezone.utc),
        )
        self.bot.gdpr_delete("privacy@example.org")

        report = self.bot.build_gdpr_compliance_report(cadence="monthly")
        self.assertEqual("gdpr_compliance_self_check", report["report_type"])
        self.assertEqual("monthly", report["cadence"])
        self.assertGreaterEqual(report["data_subject_requests"]["exports_in_period"], 1)
        self.assertGreaterEqual(report["data_subject_requests"]["deletions_in_period"], 1)
        self.assertGreaterEqual(report["data_retention"]["communications_past_retention_count"], 1)
        self.assertIn("consent_summary", report)
        self.assertIn("checks", report)
        self.assertTrue(
            any(check["name"] == "opt_out_records" for check in report["checks"])
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

    def test_create_task_tracks_due_dates_and_overdue_flags(self):
        self.bot.create_task(
            title="File grant packet",
            assigned_to="staff",
            due_date="2026-06-20",
            status="todo",
        )
        self.bot.create_task(
            title="Archive approval",
            assigned_to="staff",
            due_date="2026-06-19",
            status="done",
        )

        overdue = self.bot.list_tasks(
            assigned_to="staff",
            due_date_before="2026-06-20",
            sort="due_date",
        )

        by_title = {task["title"]: task for task in overdue}
        self.assertEqual("2026-06-20", by_title["File grant packet"]["due_date"])
        self.assertTrue(by_title["File grant packet"]["is_overdue"])
        self.assertFalse(by_title["Archive approval"]["is_overdue"])

    def test_update_task_assignment_updates_assignee(self):
        task = self.bot.create_task(title="Coordinate reviewers", assigned_to="staff")

        updated = self.bot.update_task_assignment(
            task["id"],
            assigned_to="admin",
            changed_by="admin",
        )

        self.assertEqual("admin", updated["assigned_to"])

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

    def test_task_comments_crud_and_unread_tracking(self):
        task = self.bot.create_task(
            title="Prepare proposal",
            assigned_to="staff",
            assignee_email="staff@example.org",
        )

        comment = self.bot.create_task_comment(
            task["id"],
            author="admin@example.org",
            content="Please add the latest budget numbers.",
        )
        comment_feed = self.bot.list_task_comments(task["id"], viewer_email="staff@example.org")
        self.assertEqual(1, comment_feed["unread_count"])
        self.assertEqual(comment["id"], comment_feed["comments"][0]["id"])

        marked = self.bot.mark_task_comments_read(task["id"], reader_email="staff@example.org")
        self.assertEqual(0, marked["unread_count"])

        updated = self.bot.update_task_comment(
            task["id"],
            comment["id"],
            content="Please add the latest budget and staffing numbers.",
        )
        self.assertIn("staffing", updated["content"])
        self.assertEqual(
            1,
            self.bot.get_unread_task_comment_count(task["id"], "staff@example.org"),
        )

        self.bot.delete_task_comment(task["id"], comment["id"])
        final_feed = self.bot.list_task_comments(task["id"], viewer_email="staff@example.org")
        self.assertEqual([], final_feed["comments"])
        self.assertEqual(0, final_feed["unread_count"])

    def test_task_assignment_notifications_are_sent_and_rate_limited(self):
        notifications = []

        def fake_sender(to_addr, subject, body):
            notifications.append({"to": to_addr, "subject": subject, "body": body})

        with unittest.mock.patch.dict(
            os.environ,
            {"TASK_ASSIGNMENT_NOTIFICATION_RATE_LIMIT_SECONDS": "3600"},
            clear=False,
        ):
            created = self.bot.create_task(
                title="Review budget",
                assigned_to="staff",
                assignee_email="staff@example.org",
                assignee_name="Staff User",
                sender=fake_sender,
                created_at=datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc),
            )
            reassigned = self.bot.update_task_assignment(
                created["id"],
                assigned_to="staff",
                assignee_email="staff@example.org",
                assignee_name="Staff User",
                sender=fake_sender,
                changed_by="admin",
                changed_at=datetime(2026, 6, 22, 9, 15, tzinfo=timezone.utc),
            )
            later = self.bot.update_task_assignment(
                created["id"],
                assigned_to="staff",
                assignee_email="staff@example.org",
                assignee_name="Staff User",
                sender=fake_sender,
                changed_by="admin",
                changed_at=datetime(2026, 6, 22, 11, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(2, len(notifications))
        self.assertEqual("staff@example.org", notifications[0]["to"])
        self.assertIn("Task assigned", notifications[0]["subject"])
        self.assertEqual("sent", created["assignment_notification"]["status"])
        self.assertEqual("rate_limited", reassigned["assignment_notification"]["status"])
        self.assertEqual("sent", later["assignment_notification"]["status"])

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

    def _legacy_task_filter_fixtures(self):
        return [
            self.bot.create_task(
                title="Staff todo soon",
                assigned_to="staff",
                status="todo",
                due_date="2026-07-20",
            ),
            self.bot.create_task(
                title="Staff in progress late",
                assigned_to="staff",
                status="in-progress",
                due_date="2026-07-25",
            ),
            self.bot.create_task(
                title="Admin todo mid",
                assigned_to="admin",
                status="todo",
                due_date="2026-07-22",
            ),
            self.bot.create_task(
                title="Auditor done early",
                assigned_to="auditor",
                status="done",
                due_date="2026-07-18",
            ),
            self.bot.create_task(
                title="Admin blocked no date",
                assigned_to="admin",
                status="blocked",
            ),
        ]

    def _legacy_test_list_tasks_supports_all_filter_combinations(self):
        tasks = self._legacy_task_filter_fixtures()
        filter_values = {
            "assigned_to": "staff",
            "status": "todo",
            "due_date_after": "2026-07-20",
            "due_date_before": "2026-07-22",
        }

        def matches(task, active_filters):
            due_date = task["due_date"][:10] if task["due_date"] else None
            return all(
                (
                    task["assigned_to"] == filter_values["assigned_to"]
                    if name == "assigned_to"
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
                expected_titles = [
                    task["title"]
                    for task in tasks
                    if matches(task, active_filters)
                ]
                rows = self.bot.list_tasks(
                    assigned_to=filter_values["assigned_to"] if "assigned_to" in active_filters else None,
                    status=filter_values["status"] if "status" in active_filters else None,
                    due_date_after=(
                        filter_values["due_date_after"] if "due_date_after" in active_filters else None
                    ),
                    due_date_before=(
                        filter_values["due_date_before"] if "due_date_before" in active_filters else None
                    ),
                    sort="due_date",
                )
                self.assertEqual(
                    expected_titles,
                    [task["title"] for task in rows],
                    msg=f"Unexpected results for filters {active_filters!r}",
                )

    def _legacy_test_list_tasks_supports_sorting_by_assignee_status_and_due_date(self):
        self._legacy_task_filter_fixtures()
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
            rows = self.bot.list_tasks(sort=sort_name)
            self.assertEqual(expected_titles, [task["title"] for task in rows])

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


class DataResidencyAndPrivacyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.output_dir = Path(".test_privacy_policies")
        if self.output_dir.exists():
            rmtree(self.output_dir)

    def tearDown(self):
        if self.output_dir.exists():
            rmtree(self.output_dir)

    def test_data_residency_validation_rejects_mismatched_storage_region(self):
        with unittest.mock.patch.dict(
            os.environ,
            {"DATA_RESIDENCY": "EU", "DATA_STORAGE_REGION": "US"},
            clear=False,
        ):
            with self.assertRaises(FundingBotError):
                FundingBot(db_path=":memory:")

    def test_generate_privacy_policies_creates_html_and_pdf_with_versions(self):
        with unittest.mock.patch.dict(
            os.environ,
            {"DATA_RESIDENCY": "EU", "DATA_STORAGE_REGION": "EU"},
            clear=False,
        ):
            bot = FundingBot(db_path=":memory:")
        try:
            bot.store_organization_profile(
                {
                    "name": "i4Edu",
                    "mission": "expanding access to equitable education",
                    "registration_number": "NP-42",
                    "website": "https://i4edu.example.org",
                    "contact_email": "hello@i4edu.example.org",
                    "privacy_email": "privacy@i4edu.example.org",
                    "address": "Dhaka, Bangladesh",
                    "privacy_jurisdictions": ["EU", "US"],
                }
            )

            generated = bot.generate_privacy_policies(
                output_dir=self.output_dir,
                jurisdictions=["EU", "US"],
                effective_date="2026-07-19",
            )

            self.assertEqual(2, len(generated))
            eu_policy = next(item for item in generated if item["jurisdiction"] == "EU")
            us_policy = next(item for item in generated if item["jurisdiction"] == "US")
            self.assertEqual("eu-v1", eu_policy["version"])
            self.assertEqual("us-v1", us_policy["version"])
            self.assertTrue(Path(eu_policy["html_path"]).exists())
            self.assertTrue(Path(eu_policy["pdf_path"]).exists())
            html_body = Path(eu_policy["html_path"]).read_text(encoding="utf-8")
            self.assertIn("i4Edu Privacy Policy", html_body)
            self.assertIn("Configured data residency:</strong> EU", html_body)

            generated_again = bot.generate_privacy_policies(
                output_dir=self.output_dir,
                jurisdictions=["EU"],
                formats=["html"],
            )
            self.assertEqual("eu-v2", generated_again[0]["version"])
            versions = bot.list_privacy_policy_versions(limit=10)
            self.assertEqual(3, len(versions))
        finally:
            bot.close()


class TaskFilteringFundingBotTests(unittest.TestCase):
    def setUp(self):
        self.bot = FundingBot()

    def tearDown(self):
        self.bot.close()

    def _task_filter_fixtures(self):
        return [
            self.bot.create_task(
                title="Staff todo soon",
                assigned_to="staff",
                status="todo",
                due_date="2026-07-20",
            ),
            self.bot.create_task(
                title="Staff in progress late",
                assigned_to="staff",
                status="in-progress",
                due_date="2026-07-25",
            ),
            self.bot.create_task(
                title="Admin todo mid",
                assigned_to="admin",
                status="todo",
                due_date="2026-07-22",
            ),
            self.bot.create_task(
                title="Auditor done early",
                assigned_to="auditor",
                status="done",
                due_date="2026-07-18",
            ),
            self.bot.create_task(
                title="Admin blocked latest",
                assigned_to="admin",
                status="blocked",
                due_date="2026-08-01",
            ),
        ]

    def test_list_tasks_supports_all_filter_combinations(self):
        tasks = self._task_filter_fixtures()
        filter_values = {
            "assigned_to": "staff",
            "status": "todo",
            "due_date_after": "2026-07-20",
            "due_date_before": "2026-07-22",
        }

        def matches(task, active_filters):
            due_date = task["due_date"][:10] if task["due_date"] else None
            return all(
                (
                    task["assigned_to"] == filter_values["assigned_to"]
                    if name == "assigned_to"
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
                expected_titles = [
                    task["title"]
                    for task in sorted(
                        (task for task in tasks if matches(task, active_filters)),
                        key=lambda task: (task["due_date"] is None, task["due_date"], task["id"]),
                    )
                ]
                rows = self.bot.list_tasks(
                    assigned_to=filter_values["assigned_to"] if "assigned_to" in active_filters else None,
                    status=filter_values["status"] if "status" in active_filters else None,
                    due_date_after=(
                        filter_values["due_date_after"] if "due_date_after" in active_filters else None
                    ),
                    due_date_before=(
                        filter_values["due_date_before"] if "due_date_before" in active_filters else None
                    ),
                    sort="due_date",
                )
                self.assertEqual(
                    expected_titles,
                    [task["title"] for task in rows],
                    msg=f"Unexpected results for filters {active_filters!r}",
                )

    def test_list_tasks_supports_sorting_by_assignee_status_and_due_date(self):
        self._task_filter_fixtures()
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
            rows = self.bot.list_tasks(sort=sort_name)
            self.assertEqual(expected_titles, [task["title"] for task in rows])


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


class QueueExecutionTests(unittest.TestCase):
    def setUp(self):
        self.bot = FundingBot(trusted_sources={"Grants Portal"})

    def tearDown(self):
        self.bot.close()

    def test_generate_idempotency_key_is_stable_and_prevents_duplicate_execution(self):
        calls = []
        payload = {"value": 7, "nested": {"enabled": True}}
        key = self.bot.generate_idempotency_key("demo-task", payload)

        def task(context, task_payload):
            calls.append(task_payload["value"])
            self.assertEqual(key, context.idempotency_key)
            return {"echo": task_payload["value"]}

        first = self.bot.execute_queue_task(
            "demo-task",
            payload,
            task,
            idempotency_key=key,
            install_signal_handlers=False,
        )
        second = self.bot.execute_queue_task(
            "demo-task",
            payload,
            task,
            idempotency_key=key,
            install_signal_handlers=False,
        )

        self.assertEqual([7], calls)
        self.assertEqual("completed", first["status"])
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual({"echo": 7}, second["result"])

        stored = self.bot.get_task_run(key)
        self.assertEqual(key, stored["idempotency_key"])
        self.assertEqual(1, stored["duplicate_requests"])
        self.assertEqual("completed", stored["status"])
        self.assertEqual(1, self.bot.get_queue_metrics()["duplicate_preventions"])

    def test_execute_queue_task_marks_shutdown_requested_and_cancels_cleanly(self):
        def task(context, task_payload):
            context.bot.request_task_run_shutdown(context.idempotency_key, signal_name="SIGTERM")
            context.checkpoint("Stopping queued work before the next unit.")
            return {"unexpected": task_payload}

        result = self.bot.execute_queue_task(
            "shutdown-task",
            {"step": 1},
            task,
            install_signal_handlers=False,
        )

        self.assertEqual("cancelled", result["status"])
        self.assertTrue(result["shutdown_requested"])
        self.assertIn("Stopping queued work", result["message"])

    def test_execute_queue_task_retries_with_exponential_backoff_and_records_history(self):
        attempts = {"count": 0}
        delays = []

        def flaky_task(_context, payload):
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise RuntimeError("temporary queue error")
            return {"echo": payload["value"]}

        result = self.bot.execute_queue_task(
            "retry-task",
            {"value": 7},
            flaky_task,
            retry_limit=3,
            backoff_seconds=2,
            backoff_max_seconds=10,
            sleep_func=delays.append,
            install_signal_handlers=False,
        )

        self.assertEqual("completed", result["status"])
        self.assertEqual(3, result["attempts"])
        self.assertEqual([2.0, 4.0], delays)
        self.assertFalse(result["dead_lettered"])
        self.assertEqual({"echo": 7}, result["result"])

        history = self.bot.list_task_history(result["task_id"])
        self.assertEqual(
            ["retry_scheduled", "retry_scheduled", "completed"],
            [entry["status"] for entry in history],
        )
        self.assertEqual([2.0, 4.0], [history[0]["backoff_seconds"], history[1]["backoff_seconds"]])
        self.assertEqual([], self.bot.list_dead_letter_queue(task_name="retry-task"))
        self.assertEqual(2, self.bot.get_queue_metrics()["retries_scheduled"])

    def test_execute_queue_task_moves_exhausted_failures_to_dead_letter_queue(self):
        delays = []

        def failing_task(_context, _payload):
            raise RuntimeError("permanent queue error")

        result = self.bot.execute_queue_task(
            "dead-letter-task",
            {"value": 1},
            failing_task,
            retry_limit=2,
            backoff_seconds=1,
            backoff_max_seconds=2,
            sleep_func=delays.append,
            install_signal_handlers=False,
        )

        self.assertEqual("failed", result["status"])
        self.assertEqual(3, result["attempts"])
        self.assertTrue(result["dead_lettered"])
        self.assertEqual([1.0, 2.0], delays)

        history = self.bot.list_task_history(result["task_id"])
        self.assertEqual(
            ["retry_scheduled", "retry_scheduled", "failed"],
            [entry["status"] for entry in history],
        )

        dlq_rows = self.bot.list_dead_letter_queue(task_name="dead-letter-task")
        self.assertEqual(1, len(dlq_rows))
        self.assertEqual(result["task_id"], dlq_rows[0]["task_id"])
        self.assertEqual("permanent queue error", dlq_rows[0]["error_message"])

        latest_audit = self.bot.list_audit_logs(limit=1)[0]
        self.assertEqual("queue_task_failed", latest_audit["action"])
        self.assertTrue(json.loads(latest_audit["details_json"])["dead_lettered"])

    def test_execute_queue_task_uses_environment_retry_defaults(self):
        delays = []
        attempts = {"count": 0}

        def flaky_task(_context, _payload):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("temporary queue error")
            return {"ok": True}

        with unittest.mock.patch.dict(
            os.environ,
            {
                "FUNDING_BOT_TASK_RETRY_LIMIT": "1",
                "FUNDING_BOT_TASK_RETRY_BACKOFF_SECONDS": "3",
                "FUNDING_BOT_TASK_RETRY_BACKOFF_MAX_SECONDS": "9",
            },
            clear=False,
        ):
            result = self.bot.execute_queue_task(
                "env-retry-task",
                {"value": 1},
                flaky_task,
                sleep_func=delays.append,
                install_signal_handlers=False,
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual(2, result["attempts"])
        self.assertEqual(1, result["retry_limit"])
        self.assertEqual(3.0, result["backoff_seconds"])
        self.assertEqual(9.0, result["backoff_max_seconds"])
        self.assertEqual([3.0], delays)

    def test_graceful_shutdown_controller_records_signals(self):
        seen = []
        controller = GracefulShutdownController(on_shutdown=seen.append)

        controller._handle_signal(signal.SIGTERM, None)

        self.assertTrue(controller.shutdown_requested())
        self.assertEqual([signal.SIGTERM], seen)
        with self.assertRaises(GracefulShutdownRequested):
            controller.raise_if_shutdown_requested()


from funding_bot import (
    CSRNetworkConnector,
    CrowdfundingConnector,
    FileVault,
    FoundationDirectoryConnector,
    GlobalGivingConnector,
    GrantsPortalConnector,
    KickstarterForGoodConnector,
    NGODirectoryConnector,
    OAuth2ClientCredentialsVault,
    TokenBucketRateLimiter,
    _resolve_cli_log_level,
    create_connector,
    main,
)


class PortalConnectorTests(unittest.TestCase):
    """Tests for stub portal connector implementations."""

    def test_stub_connectors_return_demo_records_and_filter_by_keyword(self):
        grants = GrantsPortalConnector()
        csr = CSRNetworkConnector()
        ngo = NGODirectoryConnector()
        foundation = FoundationDirectoryConnector()

        grants_rows = grants.fetch_opportunities(["education"])
        csr_rows = csr.fetch_opportunities(["digital learning"])
        ngo_rows = ngo.fetch_opportunities(["literacy"])
        foundation_rows = foundation.fetch_opportunities(["arts"])

        self.assertEqual(1, len(grants_rows))
        self.assertEqual("Grants Portal", grants_rows[0]["source"])
        self.assertEqual("Education Innovation Grant", grants_rows[0]["title"])

        self.assertEqual(1, len(csr_rows))
        self.assertEqual("CSR Network", csr_rows[0]["source"])
        self.assertEqual("CSR Digital Learning Fund", csr_rows[0]["title"])

        self.assertEqual(1, len(ngo_rows))
        self.assertEqual("NGO Directory", ngo_rows[0]["source"])
        self.assertEqual("Community Literacy Matching Grant", ngo_rows[0]["title"])

        self.assertEqual(1, len(foundation_rows))
        self.assertEqual("Foundation Directory", foundation_rows[0]["source"])
        self.assertEqual("Regional Arts Access Grant", foundation_rows[0]["title"])

        self.assertEqual([], grants.fetch_opportunities(["health"]))

    def test_ngo_directory_connector_fetches_live_directory_pages(self):
        calls = []

        def fake_http_client(url, payload, credentials=None, headers=None):
            calls.append((url, dict(payload), dict(headers or {})))
            self.assertEqual("https://projects.propublica.org/nonprofits/api/v2/search.json", url)
            self.assertEqual({}, headers or {})
            if payload["page"] == 0:
                return {
                    "organizations": [
                        {
                            "ein": "123456789",
                            "name": "Housing Alliance",
                            "city": "Boston",
                            "state": "MA",
                            "sub_name": "Public Charity",
                            "ntee_code": "L20",
                        }
                    ],
                    "total_results": 2,
                    "per_page": 1,
                }
            return {
                "organizations": [
                    {
                        "ein": "987654321",
                        "name": "Housing Literacy Lab",
                        "city": "Chicago",
                        "state": "IL",
                        "sub_name": "Educational Organization",
                        "ntee_code": "B90",
                    }
                ],
                "total_results": 2,
                "per_page": 1,
            }

        connector = NGODirectoryConnector(http_client=fake_http_client, transport="http")

        rows = connector.fetch_opportunities(["housing"])

        self.assertEqual(2, len(rows))
        self.assertEqual([0, 1], [call[1]["page"] for call in calls])
        self.assertEqual("Housing Alliance nonprofit directory profile", rows[0]["title"])
        self.assertIn("Live NGO directory match for 'housing'", rows[0]["summary"])
        self.assertEqual(
            "https://projects.propublica.org/nonprofits/organizations/123456789",
            rows[0]["portal_url"],
        )

    def test_foundation_directory_connector_uses_registered_credentials_for_live_results(self):
        calls = []
        bot = FundingBot()
        os.environ["FOUNDATION_DIRECTORY_TEST_KEY"] = "test-api-key"
        bot.register_credential("foundation-api", "FOUNDATION_DIRECTORY_TEST_KEY")

        def fake_http_client(url, payload, credentials=None, headers=None):
            calls.append(
                {
                    "url": url,
                    "payload": dict(payload),
                    "credentials": dict(credentials or {}),
                    "headers": dict(headers or {}),
                }
            )
            self.assertEqual("test-api-key", (headers or {}).get("X-API-Key"))
            if payload["page"] == 1:
                return {
                    "results": [
                        {
                            "funder_name": "Bright Futures Foundation",
                            "recipient_name": "City Learning Trust",
                            "grant_title": "STEM Access Grant",
                            "purpose": "Support after-school STEM labs.",
                            "detail_url": "https://api.candid.org/grants/1",
                            "subject": "Education",
                        }
                    ],
                    "total_pages": 2,
                }
            return {
                "results": [
                    {
                        "funder": {"name": "Bright Futures Foundation"},
                        "recipient": {"name": "Rural Learning Collective"},
                        "title": "Rural Innovation Grant",
                        "description": "Expand rural learning hubs.",
                        "url": "https://api.candid.org/grants/2",
                        "category": "Education",
                    }
                ],
                "total_pages": 2,
            }

        try:
            connectors = bot.connector_registry.build_connectors(
                [
                    {
                        "type": "foundation-directory",
                        "transport": "http",
                        "credential_alias": "foundation-api",
                        "settings": {"http_client": fake_http_client},
                    }
                ],
                credential_resolver=bot.resolve_credential,
            )
            found = bot.run_discovery(connectors, keywords=["education"])
        finally:
            bot.close()
            os.environ.pop("FOUNDATION_DIRECTORY_TEST_KEY", None)

        self.assertEqual(2, len(found))
        self.assertEqual([1, 2], [call["payload"]["page"] for call in calls])
        self.assertEqual("Foundation Directory", found[0]["source"])
        self.assertEqual("STEM Access Grant", found[0]["title"])

    def test_foundation_directory_connector_waits_and_retries_after_rate_limit(self):
        clock = FakeClock()
        calls = []

        def rate_limited_http_client(url, payload, credentials=None, headers=None):
            calls.append(payload["page"])
            if len(calls) == 1:
                raise urllib.error.HTTPError(
                    url,
                    429,
                    "Too Many Requests",
                    {"Retry-After": "1.5"},
                    None,
                )
            return {
                "results": [
                    {
                        "funder_name": "Rate Limit Foundation",
                        "recipient_name": "Learning Lab",
                        "grant_title": "Recovered Opportunity",
                        "detail_url": "https://api.candid.org/grants/3",
                        "purpose": "Recovered after rate limiting.",
                        "subject": "Education",
                    }
                ],
                "total_pages": 1,
            }

        connector = FoundationDirectoryConnector(
            http_client=rate_limited_http_client,
            credentials={"api_key": "test-api-key"},
            transport="http",
            max_retries=1,
            sleep_func=clock.sleep,
            time_func=clock.monotonic,
        )

        rows = connector.fetch_opportunities(["education"])

        self.assertEqual(1, len(rows))
        self.assertEqual("Recovered Opportunity", rows[0]["title"])
        self.assertEqual([1.5, 0.25], clock.sleeps)
        self.assertEqual([1, 1], calls)

    def test_connector_metrics_track_requests_errors_and_latency(self):
        FundingBot.reset_connector_metrics()
        GrantsPortalConnector().fetch_opportunities(["education"])

        def failing_http_client(_url, _params, _credentials=None):
            raise RuntimeError("connector unavailable")

        degraded = GrantsPortalConnector(http_client=failing_http_client, transport="http")
        result = degraded.fetch_result(["education"])

        self.assertEqual("degraded", result["metadata"]["source_status"])
        metrics = {
            row["connector_name"]: row
            for row in FundingBot.connector_metrics_snapshot()
        }
        grants_metrics = metrics["Grants Portal"]
        self.assertEqual(2, grants_metrics["requests_total"])
        self.assertEqual(1, grants_metrics["errors_total"])
        self.assertEqual(2, grants_metrics["latency_seconds_count"])
        self.assertGreaterEqual(grants_metrics["latency_seconds_sum"], 0.0)

    def test_oauth2_connector_credentials_are_resolved_for_remote_calls(self):
        FundingBot.reset_connector_metrics()
        calls = []

        def fake_token_http_client(url, form_data, headers):
            calls.append({"url": url, "form_data": dict(form_data), "headers": dict(headers)})
            return {"access_token": "token-123", "token_type": "Bearer", "expires_in": 3600}

        vault = OAuth2ClientCredentialsVault(
            FileVault("."),
            token_http_client=fake_token_http_client,
            refresh_skew_seconds=60,
        )
        with unittest.mock.patch.object(
            vault,
            "get_secret",
            return_value=json.dumps(
                {
                    "auth_type": "oauth2_client_credentials",
                    "oauth2": {
                        "token_url": "https://auth.example.org/oauth/token",
                        "client_id": "connector-client",
                        "client_secret": "connector-secret",
                        "scope": "grants.read",
                    },
                    "credentials": {"tenant": "ngo-team"},
                }
            ),
        ):
            credentials = vault.resolve_credentials("CONNECTOR_SECRET")

        seen_credentials = []

        def fake_http_client(_url, _params, connector_credentials=None):
            seen_credentials.append(dict(connector_credentials or {}))
            return {"opportunities": []}

        GrantsPortalConnector(
            http_client=fake_http_client,
            transport="http",
            credentials=credentials,
        ).fetch_opportunities(["education"])

        self.assertEqual(1, len(calls))
        self.assertEqual("client_credentials", calls[0]["form_data"]["grant_type"])
        self.assertEqual("Basic Y29ubmVjdG9yLWNsaWVudDpjb25uZWN0b3Itc2VjcmV0", calls[0]["headers"]["Authorization"])
        self.assertEqual("token-123", seen_credentials[0]["access_token"])
        self.assertEqual("Bearer " + "token-123", seen_credentials[0]["authorization_header"])
        self.assertEqual("ngo-team", seen_credentials[0]["tenant"])

    def test_remote_connectors_paginate_until_all_results_are_collected(self):
        calls = []

        def fake_http_client(_url, params, _credentials=None):
            calls.append(dict(params))
            page = params["page"]
            rows = {
                1: [
                    {
                        "source": "Grants Portal",
                        "donor_name": "Fund A",
                        "title": "Education Page 1A",
                        "portal_url": "https://example.org/1",
                        "summary": "Education funding",
                        "category": "Education",
                        "tags": ["education"],
                    },
                    {
                        "source": "Grants Portal",
                        "donor_name": "Fund B",
                        "title": "Education Page 1B",
                        "portal_url": "https://example.org/2",
                        "summary": "Education funding",
                        "category": "Education",
                        "tags": ["education"],
                    },
                ],
                2: [
                    {
                        "source": "Grants Portal",
                        "donor_name": "Fund C",
                        "title": "Education Page 2A",
                        "portal_url": "https://example.org/3",
                        "summary": "Education funding",
                        "category": "Education",
                        "tags": ["education"],
                    }
                ],
            }
            return {
                "opportunities": rows[page],
                "next_page": page + 1 if page < 2 else None,
                "schema_version": 2,
            }

        connector = GrantsPortalConnector(http_client=fake_http_client, page_size=2)
        opportunities = connector.fetch_opportunities(["education"])

        self.assertEqual(3, len(opportunities))
        self.assertEqual([1, 2], [call["page"] for call in calls])
        self.assertTrue(all(call["page_size"] == 2 for call in calls))

    def test_connector_cache_tracks_hits_misses_and_supports_invalidation(self):
        calls = []

        def fake_http_client(_url, params, _credentials=None):
            calls.append(dict(params))
            keyword = params["keywords"][0]
            return {
                "opportunities": [
                    {
                        "source": "Grants Portal",
                        "donor_name": "Cache Fund",
                        "title": f"{keyword.title()} Opportunity",
                        "portal_url": f"https://example.org/{keyword}",
                        "summary": f"{keyword} funding",
                        "category": "Education",
                        "tags": [keyword],
                    }
                ],
                "schema_version": 2,
            }

        connector = GrantsPortalConnector(http_client=fake_http_client, page_size=5)
        connector.fetch_opportunities(["education"])
        connector.fetch_opportunities(["education"])
        connector.fetch_opportunities(["youth"])

        metrics = connector.cache_metrics()
        self.assertEqual(1, metrics["hits"])
        self.assertEqual(2, metrics["misses"])
        self.assertEqual("grants-portal", metrics["connector_id"])
        self.assertEqual(
            "grants-portal",
            json.loads(connector.build_cache_key(["education"]))["connector_id"],
        )
        self.assertEqual(2, len(calls))

        connector.invalidate_cache(["education"])
        connector.fetch_opportunities(["education"])
        self.assertEqual(3, len(calls))

    def test_connector_cache_expires_after_ttl(self):
        calls = []

        def fake_http_client(_url, params, _credentials=None):
            calls.append(dict(params))
            return {
                "opportunities": [
                    {
                        "source": "Grants Portal",
                        "donor_name": "TTL Fund",
                        "title": "Education TTL Opportunity",
                        "portal_url": "https://example.org/ttl",
                        "summary": "Education funding",
                        "category": "Education",
                        "tags": ["education"],
                    }
                ],
                "schema_version": 2,
            }

        connector = GrantsPortalConnector(http_client=fake_http_client, cache_ttl=1, page_size=2)
        with unittest.mock.patch("funding_bot.time.monotonic", side_effect=[0.0, 0.5, 1.5, 1.6]):
            connector.fetch_opportunities(["education"])
            connector.fetch_opportunities(["education"])
            connector.fetch_opportunities(["education"])

        self.assertEqual(2, len(calls))
        self.assertEqual({"hits": 1, "misses": 2}, {
            "hits": connector.cache_metrics()["hits"],
            "misses": connector.cache_metrics()["misses"],
        })

    def test_connector_uses_per_connector_page_size_environment_override(self):
        calls = []

        def fake_http_client(_url, params, _credentials=None):
            calls.append(dict(params))
            return {"opportunities": [], "schema_version": 2}

        with unittest.mock.patch.dict(os.environ, {"GRANTS_PORTAL_PAGE_SIZE": "7"}, clear=False):
            connector = GrantsPortalConnector(http_client=fake_http_client)
            connector.fetch_opportunities(["education"])

        self.assertEqual(7, connector.page_size)
        self.assertEqual(7, calls[0]["page_size"])

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

    def test_grants_connector_http_transport_uses_oauth_credentials_and_maps_results(self):
        token_calls = []

        def fake_token_http_client(url, form_data, headers):
            token_calls.append({"url": url, "form_data": dict(form_data), "headers": dict(headers)})
            return {"access_token": "grants-token", "token_type": "Bearer", "expires_in": 3600}

        class FakeSession:
            def __init__(self):
                self.post_calls = []

            def post(self, url, **kwargs):
                self.post_calls.append({"url": url, **kwargs})
                return FakeHTTPResponse(
                    {
                        "errorcode": 0,
                        "msg": "Webservice Succeeds",
                        "data": {
                            "hitCount": 1,
                            "oppHits": [
                                {
                                    "id": "334326",
                                    "number": "21-595",
                                    "title": "Tribal Colleges and Universities Program",
                                    "agencyCode": "NSF",
                                    "agency": "U.S. National Science Foundation",
                                    "openDate": "06/24/2021",
                                    "closeDate": "09/01/2026",
                                    "oppStatus": "posted",
                                    "docType": "synopsis",
                                    "cfdaList": ["47.076"],
                                }
                            ],
                        },
                    }
                )

        secret_payload = json.dumps(
            {
                "auth_type": "oauth2_client_credentials",
                "token_url": "https://auth.example.org/oauth/token",
                "client_id": "client-id",
                "client_secret": "client-secret",
                "scope": "grants.search",
                "credentials": {"api_key": "grants-api-key"},
            }
        )
        vault = OAuth2ClientCredentialsVault(
            StaticSecretVault({"GRANTS_GOV_API_CREDENTIALS": secret_payload}),
            token_http_client=fake_token_http_client,
        )
        session = FakeSession()
        connector = GrantsPortalConnector(
            transport="http",
            credential_vault=vault,
            request_session=session,
        )

        result = connector.fetch_result(["education"])

        self.assertEqual(1, len(result["opportunities"]))
        self.assertEqual("U.S. National Science Foundation", result["opportunities"][0]["donor_name"])
        self.assertIn("search-results-detail/334326", result["opportunities"][0]["portal_url"])
        self.assertTrue(result["metadata"]["auth_applied"])
        self.assertEqual(1, len(token_calls))
        self.assertEqual("client_credentials", token_calls[0]["form_data"]["grant_type"])
        self.assertEqual("grants-api-key", session.post_calls[0]["headers"]["X-API-Key"])
        self.assertTrue(
            session.post_calls[0]["headers"]["Authorization"].startswith("Bearer "),
        )

    def test_csr_connector_http_transport_uses_subscription_key_and_maps_results(self):
        class FakeSession:
            def __init__(self):
                self.get_calls = []

            def get(self, url, **kwargs):
                self.get_calls.append({"url": url, **kwargs})
                return FakeHTTPResponse(
                    {
                        "count": 1,
                        "results": [
                            {
                                "title": "Corporate Digital Inclusion RFP",
                                "summary": "Funding for digital literacy pilots.",
                                "url": "https://candid.example.org/rfp/123",
                                "program_areas": ["Corporate Partnerships", "Digital Inclusion"],
                                "eligibility": ["Nonprofit", "501c3"],
                                "funder": {"name": "Corporate Impact Fund"},
                            }
                        ],
                    }
                )

        session = FakeSession()
        connector = CSRNetworkConnector(
            transport="http",
            credentials={"subscription_key": "candid-subscription-key"},
            request_session=session,
        )

        result = connector.fetch_result(["csr"])

        self.assertEqual(1, len(result["opportunities"]))
        self.assertEqual("Corporate Impact Fund", result["opportunities"][0]["donor_name"])
        self.assertEqual("Corporate Partnerships", result["opportunities"][0]["category"])
        self.assertEqual(
            "candid-subscription-key",
            session.get_calls[0]["headers"]["Subscription-Key"],
        )
        self.assertIn("corporate giving", session.get_calls[0]["params"]["q"])

    def test_live_connectors_log_and_degrade_on_remote_errors(self):
        connector = CSRNetworkConnector(
            transport="http",
            credentials={"subscription_key": ""},
            request_session=object(),
        )

        with self.assertLogs("funding_bot.CSRNetworkConnector", level="WARNING") as logs:
            result = connector.fetch_result(["csr"])

        self.assertEqual("degraded", result["metadata"]["source_status"])
        self.assertIn("requires a Candid subscription_key", result["metadata"]["last_error"])
        self.assertTrue(any("remote fetch failed" in message for message in logs.output))

    def test_create_connector_supports_crowdfunding_slugs(self):
        self.assertIsInstance(create_connector("globalgiving"), GlobalGivingConnector)
        self.assertIsInstance(create_connector("kickstarter-for-good"), KickstarterForGoodConnector)

    def test_crowdfunding_connectors_support_demo_results_for_globalgiving_and_kickstarter(self):
        globalgiving = GlobalGivingConnector()
        kickstarter = KickstarterForGoodConnector()

        globalgiving_rows = globalgiving.fetch_opportunities(["stem"])
        kickstarter_rows = kickstarter.fetch_opportunities(["assistive tech"])

        self.assertEqual(1, len(globalgiving_rows))
        self.assertEqual("GlobalGiving", globalgiving_rows[0]["source"])
        self.assertEqual("Community STEM Lab Campaign", globalgiving_rows[0]["title"])

        self.assertEqual(1, len(kickstarter_rows))
        self.assertEqual("Kickstarter for Good", kickstarter_rows[0]["source"])
        self.assertEqual("Assistive Tech Makerspace Project", kickstarter_rows[0]["title"])

    def test_crowdfunding_connector_parses_globalgiving_payload(self):
        connector = CrowdfundingConnector(
            platform="globalgiving",
            transport="http",
            http_client=lambda url, payload: {
                "projects": {
                    "project": [
                        {
                            "title": "Girls in STEM",
                            "projectLink": "https://www.globalgiving.org/projects/girls-in-stem/",
                            "summary": "Fund computer science classes.",
                            "themeName": "Education",
                            "country": "Bangladesh",
                            "organization": {"name": "i4Edu Partners"},
                        }
                    ]
                }
            },
        )

        rows = connector.fetch_opportunities(["stem"])

        self.assertEqual(1, len(rows))
        self.assertEqual("GlobalGiving", rows[0]["source"])
        self.assertEqual("i4Edu Partners", rows[0]["donor_name"])
        self.assertEqual("Girls in STEM", rows[0]["title"])

    def test_token_bucket_rate_limiter_refills_over_time(self):
        clock = FakeClock()
        limiter = TokenBucketRateLimiter(2, 0.5, time_func=clock.monotonic)

        self.assertEqual((True, 0.0), limiter.consume())
        self.assertEqual((True, 0.0), limiter.consume())
        allowed, retry_after = limiter.consume()
        self.assertFalse(allowed)
        self.assertAlmostEqual(2.0, retry_after)

        clock.current = 2.0
        self.assertEqual((True, 0.0), limiter.consume())

    def test_connector_rate_limit_gracefully_degrades_and_recovers(self):
        clock = FakeClock()
        calls = []

        def http_client(url, payload):
            calls.append(payload)
            return {
                "projects": {
                    "project": [
                        {
                            "title": f"Campaign {len(calls)}",
                            "projectLink": f"https://example.org/{len(calls)}",
                            "summary": "Crowdfunding equity summary",
                            "themeName": "Education",
                            "organization": {"name": "Community Fund"},
                        }
                    ]
                }
            }

        connector = CrowdfundingConnector(
            platform="globalgiving",
            transport="http",
            http_client=http_client,
            cache_ttl=1,
            time_func=clock.monotonic,
            rate_limit_config={"capacity": 1, "refill_rate": 0.5},
        )

        first = connector.fetch_result(["education"])
        second = connector.fetch_result(["equity"])

        self.assertEqual(1, len(first["opportunities"]))
        self.assertEqual([], second["opportunities"])
        self.assertEqual("rate_limit_exceeded", second["metadata"]["degraded_reason"])
        self.assertGreater(second["metadata"]["retry_after_seconds"], 0)
        self.assertEqual(1, connector.get_failure_metrics()["rate_limited_requests"])

        clock.current = 2.0
        third = connector.fetch_result(["equity"])

        self.assertEqual(1, len(third["opportunities"]))
        self.assertEqual(2, len(calls))

    def test_connector_rate_limit_configuration_can_come_from_env(self):
        with unittest.mock.patch.dict(
            os.environ,
            {
                "GLOBALGIVING_RATE_LIMIT_CAPACITY": "2",
                "GLOBALGIVING_RATE_LIMIT_REFILL_RATE": "0.25",
            },
            clear=False,
        ):
            connector = GlobalGivingConnector()

        self.assertEqual(2.0, connector.rate_limit_config["capacity"])
        self.assertEqual(0.25, connector.rate_limit_config["refill_rate"])

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


class ConnectorFallbackAndVersioningTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(".test_connector_fallback.db")
        if self.db_path.exists():
            self.db_path.unlink()
        os.environ.pop("PORTAL_FALLBACK_MODE", None)

    def tearDown(self):
        if self.db_path.exists():
            self.db_path.unlink()
        os.environ.pop("PORTAL_FALLBACK_MODE", None)

    def test_connector_detects_and_migrates_legacy_schema(self):
        def legacy_http_client(url, payload):
            return {
                "schema_version": 1,
                "opportunities": [
                    {
                        "source": "CSR Network",
                        "funder": "Legacy Donor",
                        "title": "Legacy CSR Grant",
                        "link": "https://csr.example.org/opportunities/legacy",
                        "description": "Legacy schema payload.",
                        "type": "Corporate Partnerships",
                        "topics": ["csr", "education"],
                    }
                ],
            }

        connector = CSRNetworkConnector(http_client=legacy_http_client, max_retries=0)

        result = connector.fetch_result(["csr"])

        self.assertEqual(connector.result_schema_version, result["schema_version"])
        self.assertEqual(1, result["metadata"]["detected_schema_version"])
        self.assertEqual(1, result["metadata"]["upstream_schema_version"])
        self.assertEqual("Legacy Donor", result["opportunities"][0]["donor_name"])
        self.assertEqual(
            "https://csr.example.org/opportunities/legacy",
            result["opportunities"][0]["portal_url"],
        )

    def test_run_discovery_uses_default_fallback_and_logs_activation(self):
        def failing_http_client(url, payload):
            raise ConnectionError("connector offline")

        connector = GrantsPortalConnector(http_client=failing_http_client, max_retries=0)
        bot = FundingBot(db_path=self.db_path, trusted_sources={"Grants Portal"})
        try:
            with self.assertLogs("funding_bot", level="WARNING") as logs:
                found = bot.run_discovery([connector], keywords=["education"])

            self.assertEqual(1, len(found))
            self.assertEqual("Education Innovation Grant", found[0]["title"])
            cache_row = bot.connection.execute(
                """
                SELECT schema_version, source_status, metadata_json
                FROM connector_result_cache
                WHERE connector_name = ?
                """,
                ("Grants Portal",),
            ).fetchone()
            metadata = json.loads(cache_row["metadata_json"])
            self.assertEqual(connector.result_schema_version, cache_row["schema_version"])
            self.assertEqual("default", cache_row["source_status"])
            self.assertEqual("default", metadata["fallback_mode"])
            self.assertTrue(
                any("fallback activated" in message.lower() for message in logs.output)
            )
        finally:
            bot.close()

    def test_run_discovery_migrates_cached_results_before_fallback(self):
        connector = CSRNetworkConnector(http_client=lambda url, payload: (_ for _ in ()).throw(ConnectionError("unreachable")), max_retries=0)
        seeded_bot = FundingBot(db_path=self.db_path, trusted_sources={"CSR Network"})
        try:
            seeded_bot.connection.execute(
                """
                INSERT INTO connector_result_cache (
                    connector_name, cache_key, schema_version, fetched_at,
                    source_status, metadata_json, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "CSR Network",
                    connector.build_cache_key(["csr"]),
                    1,
                    seeded_bot._to_iso(),
                    "remote",
                    json.dumps({"seeded": True}, sort_keys=True),
                    json.dumps(
                        [
                            {
                                "source": "CSR Network",
                                "funder": "Cached Legacy Donor",
                                "title": "Cached Legacy Grant",
                                "link": "https://csr.example.org/opportunities/cached-legacy",
                                "description": "Cached legacy schema payload.",
                                "type": "Corporate Partnerships",
                                "topics": ["csr", "education"],
                            }
                        ],
                        sort_keys=True,
                    ),
                ),
            )
            seeded_bot.connection.commit()
        finally:
            seeded_bot.close()

        bot = FundingBot(db_path=self.db_path, trusted_sources={"CSR Network"})
        try:
            found = bot.run_discovery([connector], keywords=["csr"])
            self.assertEqual(1, len(found))
            self.assertEqual("Cached Legacy Donor", found[0]["donor_name"])
            self.assertEqual(
                "https://csr.example.org/opportunities/cached-legacy",
                found[0]["portal_url"],
            )

            cache_row = bot.connection.execute(
                """
                SELECT schema_version, source_status, metadata_json, result_json
                FROM connector_result_cache
                WHERE connector_name = ?
                """,
                ("CSR Network",),
            ).fetchone()
            metadata = json.loads(cache_row["metadata_json"])
            payload = json.loads(cache_row["result_json"])
            self.assertEqual(connector.result_schema_version, cache_row["schema_version"])
            self.assertEqual("cached", cache_row["source_status"])
            self.assertEqual(1, metadata["migrated_from_schema_version"])
            self.assertEqual("cached", metadata["fallback_mode"])
            self.assertEqual(
                "https://csr.example.org/opportunities/cached-legacy",
                payload[0]["portal_url"],
            )
        finally:
            bot.close()


class DonorSegmentationAndTemplateTests(unittest.TestCase):
    """Tests donor segmentation and outreach templates."""

    def setUp(self):
        self.bot = FundingBot(trusted_sources={"Grants Portal"})
        self.outreach_context = {
            "organization_name": "i4Edu",
            "mission": "Expand access to equitable education.",
        }

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

    def test_donor_cache_tracks_hits_and_invalidates_after_updates(self):
        self.bot.upsert_donor(
            email="cache@example.org",
            name="Cache Donor",
            segment="corporate",
        )

        self.assertEqual("Cache Donor", self.bot.get_donor("cache@example.org")["name"])
        self.assertEqual("Cache Donor", self.bot.get_donor("cache@example.org")["name"])
        self.assertEqual(1, len(self.bot.list_donors(segment="corporate")))
        self.assertEqual(1, len(self.bot.list_donors(segment="corporate")))

        metrics = self.bot.get_cache_metrics()["namespaces"]["donor-records"]
        self.assertGreaterEqual(metrics["hits"], 2)
        self.assertGreaterEqual(metrics["misses"], 2)

        self.bot.upsert_donor(
            email="cache@example.org",
            name="Cache Donor Updated",
            segment="corporate",
        )

        self.assertEqual("Cache Donor Updated", self.bot.get_donor("cache@example.org")["name"])
        self.assertEqual(
            "Cache Donor Updated",
            self.bot.list_donors(segment="corporate")[0]["name"],
        )

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
            context=self.outreach_context,
            sender=fake_sender,
            sent_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
        )
        fallback_result = self.bot.send_outreach_from_template(
            "intro",
            "unknown@example.org",
            "Unknown Donor",
            context=self.outreach_context,
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
            context=self.outreach_context,
            sent_at=datetime(2026, 6, 24, tzinfo=timezone.utc),
        )
        english_result = self.bot.send_outreach_from_template(
            "intro",
            "english@example.org",
            "English Donor",
            context=self.outreach_context,
            sent_at=datetime(2026, 6, 25, tzinfo=timezone.utc),
        )

        self.assertIn("ধন্যবাদ", bangla_result["subject"])
        self.assertIn("ভবিষ্যতের যোগাযোগ বন্ধ করতে", bangla_result["body"])
        self.assertIn("Support i4Edu", english_result["subject"])
        self.assertIn("Expand access to equitable education.", english_result["body"])

    def test_all_catalog_templates_render_for_every_supported_locale(self):
        for locale in self.bot.list_supported_outreach_locales():
            for template_name in self.bot.list_catalog_outreach_templates():
                with self.subTest(locale=locale, template_name=template_name):
                    donor_email = f"{template_name}-{locale}@example.org".replace("_", "-")
                    self.bot.upsert_donor(
                        email=donor_email,
                        name="Matrix Donor",
                        segment="corporate",
                        locale=locale,
                    )

                    result = self.bot.send_outreach_from_template(
                        template_name,
                        donor_email,
                        "Matrix Donor",
                        context={
                            **self.outreach_context,
                            "opt_out_url": "https://i4edu.org/opt-out",
                        },
                        sent_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
                    )

                    self.assertEqual(locale, result["locale"])
                    self.assertEqual(template_name, result["template_name"])
                    self.assertTrue(result["subject"].strip())
                    self.assertTrue(result["body"].strip())
                    self.assertIn("Matrix Donor", result["body"])
                    self.assertIn("https://i4edu.org/opt-out", result["body"])
                    self.assertNotIn("{donor_name}", result["subject"])
                    self.assertNotIn("{donor_name}", result["body"])
                    self.assertNotIn("{organization_name}", result["subject"])
                    self.assertNotIn("{organization_name}", result["body"])

    def test_validate_outreach_template_catalogs_rejects_missing_segment_translations(self):
        broken_catalog = json.loads(json.dumps(FundingBot._load_outreach_template_catalog("en")))
        localized_catalog = {
            template_name: {
                "en": json.loads(json.dumps(template)),
                "bn": json.loads(json.dumps(template)),
            }
            for template_name, template in broken_catalog.items()
        }
        localized_catalog["intro"]["bn"]["segments"] = {}

        with unittest.mock.patch("funding_bot._load_localized_outreach_templates", return_value=localized_catalog):
            with self.assertRaises(FundingBotError):
                FundingBot.validate_outreach_template_catalogs()

    def test_secret_donor_fields_are_encrypted_and_tagged(self):
        self.bot.upsert_donor(
            email="sensitive@example.org",
            name="Sensitive Donor",
            preferences={"notes": "deeply-sensitive-preference"},
        )

        row = self.bot.connection.execute(
            """
            SELECT preferences_json, data_classification, field_classifications_json
            FROM donors
            WHERE email = ?
            """,
            ("sensitive@example.org",),
        ).fetchone()
        donor = self.bot.get_donor("sensitive@example.org")

        self.assertNotIn("deeply-sensitive-preference", row["preferences_json"])
        self.assertEqual("secret", row["data_classification"])
        self.assertEqual("deeply-sensitive-preference", donor["preferences"]["notes"])
        self.assertEqual("secret", donor["field_classifications"]["preferences"])

    def test_organization_profile_is_encrypted_and_tracks_classification_changes(self):
        self.bot.store_organization_profile(
            {
                "name": "i4Edu",
                "mission": "Expand access to equitable education.",
                "tax_id": "SECRET-TAX-ID",
            }
        )
        stored = self.bot.connection.execute(
            """
            SELECT value_json, data_classification, field_classifications_json
            FROM organization_profile
            WHERE key = 'profile'
            """
        ).fetchone()
        self.assertNotIn("SECRET-TAX-ID", stored["value_json"])
        self.assertEqual("secret", stored["data_classification"])
        self.assertEqual(
            "secret",
            json.loads(stored["field_classifications_json"])["tax_id"],
        )
        self.assertEqual("SECRET-TAX-ID", self.bot.load_organization_profile()["tax_id"])

    def test_deduped_profile_cache_tracks_hits_and_invalidates_on_profile_updates(self):
        self.bot.store_organization_profile({"name": "i4Edu", "mission": "First"})

        self.assertEqual("First", self.bot.load_organization_profile()["mission"])
        self.assertEqual("First", self.bot.load_organization_profile()["mission"])

        metrics = self.bot.get_cache_metrics()["namespaces"]["deduped-profiles"]
        self.assertGreaterEqual(metrics["hits"], 1)
        self.assertGreaterEqual(metrics["misses"], 1)

        self.bot.store_organization_profile({"name": "i4Edu", "mission": "Updated"})
        self.assertEqual("Updated", self.bot.load_organization_profile()["mission"])

        self.bot.store_setting(
            "profile",
            {
                "name": "i4Edu",
                "mission": "Expand access to equitable education.",
                "tax_id": "SECRET-TAX-ID",
            },
            field_classifications={"mission": "internal"},
        )
        latest_change = self.bot.connection.execute(
            """
            SELECT details_json
            FROM audit_logs
            WHERE action = 'data_classification_changed'
              AND details_json LIKE '%organization_profile%'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        self.assertEqual(
            "internal",
            json.loads(latest_change["details_json"])["field_classifications"]["mission"],
        )

    def test_classification_enforcement_rejects_invalid_record_level(self):
        with self.assertRaises(ValueError):
            self.bot.upsert_donor(
                email="invalid@example.org",
                name="Invalid Donor",
                data_classification="confidential",
                field_classifications={"preferences": "secret"},
            )


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

    def test_oauth2_client_credentials_are_cached_between_resolutions(self):
        calls = []
        (self.vault_dir / "OAUTH_SECRET").write_text(
            json.dumps(
                {
                    "auth_type": "oauth2_client_credentials",
                    "oauth2": {
                        "token_url": "https://auth.example.org/oauth/token",
                        "client_id": "connector-client",
                        "client_secret": "connector-secret",
                        "scope": "grants.read",
                    },
                    "credentials": {"tenant": "ngo-team"},
                }
            ),
            encoding="utf-8",
        )
        self.bot.close()

        def fake_token_http_client(url, form_data, headers):
            calls.append({"url": url, "form_data": dict(form_data), "headers": dict(headers)})
            return {"access_token": "cached-token", "token_type": "Bearer", "expires_in": 3600}

        self.bot = FundingBot(
            trusted_sources={"Grants Portal"},
            vault=FileVault(self.vault_dir),
            oauth_token_http_client=fake_token_http_client,
            oauth_refresh_skew_seconds=60,
        )
        self.bot.register_credential("oauth", "OAUTH_SECRET")

        first = self.bot.resolve_credential("oauth")
        second = self.bot.resolve_credential("oauth")

        self.assertEqual(1, len(calls))
        self.assertEqual("cached-token", first["access_token"])
        self.assertEqual(first["access_token"], second["access_token"])
        self.assertEqual("Bearer " + "cached-token", second["authorization_header"])
        self.assertEqual("ngo-team", second["tenant"])

    def test_oauth2_client_credentials_refresh_before_expiry(self):
        calls = []
        (self.vault_dir / "OAUTH_SECRET").write_text(
            json.dumps(
                {
                    "auth_type": "oauth2_client_credentials",
                    "oauth2": {
                        "token_url": "https://auth.example.org/oauth/token",
                        "client_id": "connector-client",
                        "client_secret": "connector-secret",
                    },
                }
            ),
            encoding="utf-8",
        )
        self.bot.close()

        def fake_token_http_client(_url, _form_data, _headers):
            token_number = len(calls) + 1
            calls.append(token_number)
            return {
                "access_token": f"refresh-token-{token_number}",
                "token_type": "Bearer",
                "expires_in": 30,
            }

        self.bot = FundingBot(
            trusted_sources={"Grants Portal"},
            vault=FileVault(self.vault_dir),
            oauth_token_http_client=fake_token_http_client,
            oauth_refresh_skew_seconds=60,
        )
        self.bot.register_credential("oauth", "OAUTH_SECRET")

        first = self.bot.resolve_credential("oauth")
        second = self.bot.resolve_credential("oauth")

        self.assertEqual([1, 2], calls)
        self.assertEqual("refresh-token-1", first["access_token"])
        self.assertEqual("refresh-token-2", second["access_token"])

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

    def test_list_opportunities_command_supports_json_output(self):
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            main(
                [
                    "--db",
                    str(self.db_path),
                    "list-opportunities",
                    "--status",
                    "new",
                    "--limit",
                    "1",
                    "--json",
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual("list-opportunities", payload["command"])
        self.assertTrue(payload["ok"])
        self.assertEqual(1, payload["count"])
        self.assertEqual("UNICEF CSR Grant", payload["rows"][0]["title"])

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
        self.assertIn("email\tname\tsegment\tlocale\topted_out\tlast_contact_at", output)
        self.assertIn("corp@example.org\tCorporate Donor\tcorporate", output)

    def test_monthly_audit_report_command_prints_json_to_stdout(self):
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            main(["--db", str(self.db_path), "monthly-audit-report", "--year", "2026", "--month", "6"])

        report = json.loads(stdout.getvalue())
        self.assertEqual("monthly_compliance_audit", report["report_type"])
        self.assertEqual("2026-06", report["period"])

    def test_monthly_audit_report_command_writes_output_file_with_missing_parent_dir(self):
        tmpdir = _reset_test_dir("monthly-audit-report")
        try:
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
        finally:
            rmtree(tmpdir)


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


class DataRetentionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.bot = FundingBot(trusted_sources={"Grants Portal"})
        self.bot.store_organization_profile({"name": "i4Edu"})
        self.expired_document_path = Path(".test_expired_retention_document.txt")
        self.recent_document_path = Path(".test_recent_retention_document.txt")
        for path in (self.expired_document_path, self.recent_document_path):
            if path.exists():
                path.unlink()
        self.expired_document_path.write_text("expired", encoding="utf-8")
        self.recent_document_path.write_text("recent", encoding="utf-8")

    def tearDown(self):
        self.bot.close()
        for path in (self.expired_document_path, self.recent_document_path):
            if path.exists():
                path.unlink()

    def test_store_and_enforce_data_retention_policy(self):
        policy = self.bot.store_data_retention_policy(
            {
                "audit_logs_days": 30,
                "communications_days": 30,
                "documents_days": 30,
                "submission_attempts_days": 30,
                "completed_tasks_days": 30,
            }
        )
        self.assertEqual(30, policy["audit_logs_days"])
        self.assertEqual(policy, self.bot.load_data_retention_policy())

        self.bot.connection.execute(
            "INSERT INTO audit_logs (happened_at, action, details_json) VALUES (?, ?, ?)",
            ("2026-05-01T00:00:00+00:00", "expired_audit", "{}"),
        )
        self.bot.connection.execute(
            "INSERT INTO audit_logs (happened_at, action, details_json) VALUES (?, ?, ?)",
            ("2026-07-10T00:00:00+00:00", "recent_audit", "{}"),
        )
        expired_comm_id = self.bot.connection.execute(
            """
            INSERT INTO communications (donor_email, donor_name, subject, body, channel, sent_at)
            VALUES (?, ?, ?, ?, 'email', ?)
            """,
            (
                "expired@example.org",
                "Expired Donor",
                "Expired outreach",
                "Body",
                "2026-05-01T00:00:00+00:00",
            ),
        ).lastrowid
        recent_comm_id = self.bot.connection.execute(
            """
            INSERT INTO communications (donor_email, donor_name, subject, body, channel, sent_at)
            VALUES (?, ?, ?, ?, 'email', ?)
            """,
            (
                "recent@example.org",
                "Recent Donor",
                "Recent outreach",
                "Body",
                "2026-07-10T00:00:00+00:00",
            ),
        ).lastrowid
        self.bot.connection.execute(
            "INSERT INTO outreach_events (communication_id, event_type, happened_at) VALUES (?, 'sent', ?)",
            (expired_comm_id, "2026-05-01T00:00:00+00:00"),
        )
        self.bot.connection.execute(
            "INSERT INTO outreach_events (communication_id, event_type, happened_at) VALUES (?, 'sent', ?)",
            (recent_comm_id, "2026-07-10T00:00:00+00:00"),
        )
        self.bot.connection.execute(
            "INSERT INTO documents (kind, format, path, created_at) VALUES (?, ?, ?, ?)",
            ("report", "pdf", str(self.expired_document_path), "2026-05-01T00:00:00+00:00"),
        )
        self.bot.connection.execute(
            "INSERT INTO documents (kind, format, path, created_at) VALUES (?, ?, ?, ?)",
            ("report", "pdf", str(self.recent_document_path), "2026-07-10T00:00:00+00:00"),
        )
        self.bot.connection.execute(
            """
            INSERT INTO submission_attempts (opportunity_signature, attempt_number, succeeded, error_message, happened_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("expired-opportunity", 1, 0, "timeout", "2026-05-01T00:00:00+00:00"),
        )
        self.bot.connection.execute(
            """
            INSERT INTO submission_attempts (opportunity_signature, attempt_number, succeeded, error_message, happened_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("recent-opportunity", 1, 1, None, "2026-07-10T00:00:00+00:00"),
        )
        self.bot.connection.execute(
            """
            INSERT INTO tasks (
                external_id, title, description, assignee, status, due_date, source, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 'done', ?, 'manual', ?, ?)
            """,
            (
                None,
                "Expired task",
                "",
                "staff",
                "2026-05-01T00:00:00+00:00",
                "2026-05-01T00:00:00+00:00",
                "2026-05-01T00:00:00+00:00",
            ),
        )
        self.bot.connection.execute(
            """
            INSERT INTO tasks (
                external_id, title, description, assignee, status, due_date, source, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 'done', ?, 'manual', ?, ?)
            """,
            (
                None,
                "Recent task",
                "",
                "staff",
                "2026-07-10T00:00:00+00:00",
                "2026-07-10T00:00:00+00:00",
                "2026-07-10T00:00:00+00:00",
            ),
        )
        self.bot.connection.commit()

        report = self.bot.enforce_data_retention(
            now=datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
        )

        self.assertFalse(report["dry_run"])
        self.assertEqual(1, report["deleted"]["audit_logs"])
        self.assertEqual(1, report["deleted"]["communications"])
        self.assertEqual(1, report["deleted"]["outreach_events"])
        self.assertEqual(1, report["deleted"]["documents"])
        self.assertEqual(1, report["deleted"]["submission_attempts"])
        self.assertEqual(1, report["deleted"]["completed_tasks"])
        self.assertEqual(1, report["deleted"]["document_files_deleted"])

        self.assertFalse(self.expired_document_path.exists())
        self.assertTrue(self.recent_document_path.exists())
        self.assertIsNone(
            self.bot.connection.execute(
                "SELECT 1 FROM audit_logs WHERE action = 'expired_audit'"
            ).fetchone()
        )
        self.assertEqual(
            1,
            self.bot.connection.execute("SELECT COUNT(*) FROM communications").fetchone()[0],
        )
        self.assertEqual(
            1,
            self.bot.connection.execute("SELECT COUNT(*) FROM outreach_events").fetchone()[0],
        )
        self.assertEqual(
            1,
            self.bot.connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
        )
        self.assertEqual(
            1,
            self.bot.connection.execute("SELECT COUNT(*) FROM submission_attempts").fetchone()[0],
        )
        self.assertEqual(
            1,
            self.bot.connection.execute("SELECT COUNT(*) FROM tasks WHERE status = 'done'").fetchone()[0],
        )


class CliDataRetentionCommandsTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(".test_retention_cli.db")
        if self.db_path.exists():
            self.db_path.unlink()
        FundingBot(db_path=self.db_path).close()

    def tearDown(self):
        if self.db_path.exists():
            self.db_path.unlink()

    def test_retention_cli_commands_store_policy_and_report_dry_run(self):
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            main([
                "--db",
                str(self.db_path),
                "set-data-retention-policy",
                "--audit-logs-days",
                "45",
                "--communications-days",
                "60",
            ])
        policy = json.loads(stdout.getvalue())
        self.assertEqual(45, policy["audit_logs_days"])
        self.assertEqual(60, policy["communications_days"])

        bot = FundingBot(db_path=self.db_path)
        bot.connection.execute(
            "INSERT INTO audit_logs (happened_at, action, details_json) VALUES (?, ?, ?)",
            ("2026-05-01T00:00:00+00:00", "expired_audit", "{}"),
        )
        bot.connection.commit()
        bot.close()

        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            main([
                "--db",
                str(self.db_path),
                "enforce-data-retention",
                "--dry-run",
                "--as-of",
                "2026-07-19T12:00:00+00:00",
            ])
        report = json.loads(stdout.getvalue())
        self.assertTrue(report["dry_run"])
        self.assertEqual(1, report["deleted"]["audit_logs"])

        bot = FundingBot(db_path=self.db_path)
        try:
            self.assertEqual(
                1,
                bot.connection.execute(
                    "SELECT COUNT(*) FROM audit_logs WHERE action = 'expired_audit'"
                ).fetchone()[0],
            )
        finally:
            bot.close()


class CliLoggingAndInteractiveTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(".test_cli_interactive.db")
        if self.db_path.exists():
            self.db_path.unlink()
        bot = FundingBot(db_path=self.db_path, trusted_sources={"Grants Portal"})
        bot.store_organization_profile({"name": "i4Edu"})
        bot.close()

    def tearDown(self):
        if self.db_path.exists():
            self.db_path.unlink()

    def test_resolve_cli_log_levels(self):
        self.assertEqual(logging.WARNING, _resolve_cli_log_level())
        self.assertEqual(logging.INFO, _resolve_cli_log_level(verbose=True))
        self.assertEqual(logging.ERROR, _resolve_cli_log_level(quiet=True))

    def test_main_passes_verbosity_flags_to_logging_config(self):
        cases = (
            (["--verbose", "--db", str(self.db_path), "list-opportunities"], {"verbose": True, "quiet": False}),
            (["--quiet", "--db", str(self.db_path), "list-opportunities"], {"verbose": False, "quiet": True}),
            (["--db", str(self.db_path), "list-opportunities"], {"verbose": False, "quiet": False}),
        )
        for argv, expected in cases:
            with self.subTest(argv=argv):
                with (
                    unittest.mock.patch("funding_bot._configure_cli_logging") as configure_logging,
                    unittest.mock.patch("sys.stdout", new_callable=io.StringIO),
                ):
                    main(argv)
                configure_logging.assert_called_once_with(**expected)

    def test_send_outreach_prompts_for_missing_required_arguments(self):
        with (
            unittest.mock.patch.dict(
                "sys.modules",
                {"celery_tasks": types.SimpleNamespace(send_outreach_task=object())},
            ),
            unittest.mock.patch("builtins.input", side_effect=["donor@example.org", "Donor"]) as prompt,
            unittest.mock.patch("funding_bot._queue_async_task") as queue_task,
        ):
            main(["--db", str(self.db_path), "send-outreach", "--dry-run"])

        self.assertEqual(2, prompt.call_count)
        self.assertEqual("send-outreach", queue_task.call_args.args[0])
        self.assertEqual(
            {
                "db_path": str(self.db_path),
                "donor_email": "donor@example.org",
                "donor_name": "Donor",
                "template_name": "default",
                "subject_template": None,
                "body_template": None,
                "locale": None,
                "dry_run": True,
            },
            queue_task.call_args.kwargs["task_kwargs"],
        )

    def test_test_connector_reprompts_until_a_valid_choice_is_given(self):
        with (
            unittest.mock.patch("builtins.input", side_effect=["invalid-connector", "csr-network"]),
            unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
            unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
        ):
            main(["--db", str(self.db_path), "test-connector", "--limit", "1"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual("csr-network", payload["connector"])
        self.assertIn("Invalid value for --connector", stderr.getvalue())

    def test_non_interactive_mode_errors_when_required_arguments_are_missing(self):
        with (
            unittest.mock.patch("sys.stderr", new_callable=io.StringIO) as stderr,
            self.assertRaises(SystemExit) as exc_info,
        ):
            main(["--non-interactive", "--db", str(self.db_path), "send-outreach", "--dry-run"])

        self.assertEqual(2, exc_info.exception.code)
        self.assertIn("--email, --name", stderr.getvalue())


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
        with unittest.mock.patch(
            "funding_bot.default_connectors",
            return_value=[GrantsPortalConnector(), CSRNetworkConnector(), NGODirectoryConnector()],
        ):
            found = self.bot.run_discovery(keywords=["literacy"])
        self.assertEqual(1, len(found))
        self.assertEqual("NGO Directory", found[0]["source"])


class CliSearchAndSettingsCommandsTests(unittest.TestCase):
    """Tests for the settings/discovery/outreach CLI commands."""

    def setUp(self):
        self.db_path = Path(".test_cli_settings.db")
        if self.db_path.exists():
            self.db_path.unlink()
        self._previous_always_eager = task_queue.celery_app.conf.task_always_eager
        self._previous_store_eager = getattr(task_queue.celery_app.conf, "task_store_eager_result", None)
        task_queue.celery_app.conf.task_always_eager = True
        task_queue.celery_app.conf.task_store_eager_result = True

    def tearDown(self):
        task_queue.celery_app.conf.task_always_eager = self._previous_always_eager
        task_queue.celery_app.conf.task_store_eager_result = self._previous_store_eager
        if self.db_path.exists():
            self.db_path.unlink()

    def test_discover_command_prints_new_opportunities(self):
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            main(["--db", str(self.db_path), "discover", "--keywords", "education"])

        output = stdout.getvalue()
        self.assertIn("Queued discover task", output)
        self.assertIn("Education Innovation Grant", output)

    def test_discover_command_reports_no_results(self):
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            main(["--db", str(self.db_path), "discover", "--keywords", "no-such-keyword"])

        self.assertIn("Queued discover task", stdout.getvalue())
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
        self.assertIn("Queued send-outreach task", output)
        self.assertIn("dry run", output)

        bot = FundingBot(db_path=self.db_path)
        try:
            communications = bot.connection.execute("SELECT * FROM communications").fetchall()
            self.assertEqual(1, len(communications))
            self.assertEqual("donor@example.org", communications[0]["donor_email"])
        finally:
            bot.close()

    def test_send_outreach_command_previews_requested_locale_template(self):
        bot = FundingBot(db_path=self.db_path)
        try:
            bot.store_organization_profile({"name": "i4Edu", "mission": "Expand access to education."})
        finally:
            bot.close()

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
                    "--template-name",
                    "intro",
                    "--locale",
                    "bn",
                    "--dry-run",
                ]
            )

        output = stdout.getvalue()
        self.assertIn("Template: intro", output)
        self.assertIn("Locale: bn", output)
        self.assertIn("প্রিয় Donor", output)

        bot = FundingBot(db_path=self.db_path)
        try:
            donor = bot.connection.execute(
                "SELECT locale FROM donors WHERE email = ?",
                ("donor@example.org",),
            ).fetchone()
            self.assertEqual("bn", donor["locale"])
        finally:
            bot.close()

    def test_set_organization_profile_and_show_settings_commands(self):
        tmpdir = _reset_test_dir("organization-profile")
        try:
            profile_path = Path(tmpdir) / "profile.json"
            profile_path.write_text(json.dumps({"name": "i4Edu"}), encoding="utf-8")
            main(["--db", str(self.db_path), "set-organization-profile", "--file", str(profile_path)])
        finally:
            rmtree(tmpdir)

        main(["--db", str(self.db_path), "register-credential", "--alias", "smtp", "--env-var", "SMTP_PASSWORD"])

        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            main(["--db", str(self.db_path), "show-settings"])

        output = stdout.getvalue()
        json_blob, _, table_output = output.partition("Credential aliases")
        settings = json.loads(json_blob)
        self.assertEqual({"name": "i4Edu"}, settings["organization_profile"])
        self.assertIn("smtp", table_output)
        self.assertIn("SMTP_PASSWORD", table_output)

    def test_show_settings_command_supports_json_output(self):
        bot = FundingBot(db_path=self.db_path)
        try:
            bot.store_organization_profile({"name": "i4Edu"})
            bot.register_credential("smtp", "SMTP_PASSWORD")
        finally:
            bot.close()

        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            main(["--db", str(self.db_path), "show-settings", "--json"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual("show-settings", payload["command"])
        self.assertEqual({"name": "i4Edu"}, payload["organization_profile"])
        self.assertEqual([{"alias": "smtp", "env_var_name": "SMTP_PASSWORD"}], payload["credentials"])


class CliEnhancementCommandTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(".test_cli_enhancements.db")
        if self.db_path.exists():
            self.db_path.unlink()
        FundingBot(db_path=self.db_path).close()

    def tearDown(self):
        if self.db_path.exists():
            self.db_path.unlink()

    def test_completion_command_outputs_bash_script(self):
        with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            main(["--db", str(self.db_path), "completion", "--shell", "bash"])

        script = stdout.getvalue()
        self.assertIn("_funding_bot_completion()", script)
        self.assertIn("complete -F _funding_bot_completion funding-bot", script)
        self.assertIn("doctor", script)
        self.assertIn("--json", script)

    def test_doctor_command_supports_json_output(self):
        queue_config = types.SimpleNamespace(
            enable_task_queue=True,
            enable_legacy_cron=False,
            broker_url="redis://127.0.0.1:6379/0",
            result_backend="redis://127.0.0.1:6379/1",
            task_always_eager=False,
            queue_name="funding-bot",
            inspect_timeout_seconds=1.0,
        )
        fake_task_queue = types.SimpleNamespace(
            celery_app=object(),
            load_queue_config=lambda: queue_config,
            get_queue_status=lambda **kwargs: {
                "queue_enabled": True,
                "legacy_cron_enabled": False,
                "mode": "queue",
                "active_modes": ["queue"],
                "broker_transport": "redis",
                "queue_name": "funding-bot",
                "queue_depth": 0,
                "active_tasks": 0,
                "reserved_tasks": 0,
                "scheduled_tasks": 0,
                "worker_count": 1,
                "workers": ["worker@local"],
                "worker_status": "healthy",
            },
        )
        with (
            unittest.mock.patch.dict("sys.modules", {"task_queue": fake_task_queue}),
            unittest.mock.patch(
                "funding_bot._collect_redis_diagnostics",
                return_value={"status": "ok", "checked": True, "targets": [{"role": "broker", "status": "ok"}]},
            ),
            unittest.mock.patch(
                "funding_bot._collect_connector_diagnostics",
                return_value={
                    "status": "ok",
                    "count": 1,
                    "connectors": [{"connector": "csr-network", "status": "ok", "healthy": True}],
                },
            ),
            unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            main(["--db", str(self.db_path), "doctor", "--json"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual("doctor", payload["command"])
        self.assertEqual("ok", payload["overall_status"])
        self.assertEqual("ok", payload["checks"]["database"]["status"])
        self.assertEqual("ok", payload["checks"]["celery"]["status"])
        self.assertEqual("ok", payload["checks"]["redis"]["status"])
        self.assertEqual("ok", payload["checks"]["connectors"]["status"])


class TaskApiRequirementTests(unittest.TestCase):
    def setUp(self):
        self.db_path = Path(".test_task_requirements.db")
        if self.db_path.exists():
            self.db_path.unlink()
        self.bot = FundingBot(db_path=self.db_path)

    def tearDown(self):
        self.bot.close()
        if self.db_path.exists():
            self.db_path.unlink()

    def test_task_model_supports_required_fields(self):
        task = self.bot.create_task(
            title="Draft budget narrative",
            assignee="staff",
            description="Prepare the first donor-ready draft.",
            status="pending",
            due_date="2026-07-31",
        )

        model = Task.from_row(
            self.bot.connection.execute("SELECT * FROM tasks WHERE id = ?", (task["id"],)).fetchone()
        )

        self.assertIsNotNone(model)
        self.assertEqual("Draft budget narrative", model.title)
        self.assertEqual("staff", model.assignee)
        self.assertEqual("2026-07-31", model.due_date)

    def test_task_list_supports_filters_and_sorting(self):
        self.bot.create_task(
            title="Later staff task",
            assignee="staff",
            description="Later due date",
            status="pending",
            due_date="2026-08-02",
        )
        self.bot.create_task(
            title="Earlier staff task",
            assignee="staff",
            description="Earlier due date",
            status="pending",
            due_date="2026-07-25",
        )
        self.bot.create_task(
            title="Blocked admin task",
            assignee="admin",
            description="Blocked",
            status="blocked",
            due_date="2026-07-26",
        )

        rows = self.bot.list_tasks(
            assignee="staff",
            status="pending",
            due_after="2026-07-24",
            sort_by="due_date",
            sort_order="asc",
        )

        self.assertEqual(["Earlier staff task", "Later staff task"], [row["title"] for row in rows])

    def test_task_update_changes_required_fields(self):
        task = self.bot.create_task(
            title="Initial title",
            assignee="staff",
            description="Initial description",
            status="pending",
            due_date="2026-07-21",
        )

        updated = self.bot.update_task(
            task["id"],
            title="Updated title",
            description="Updated description",
            assignee="auditor",
            status="blocked",
            due_date="2026-07-22",
        )

        self.assertEqual("Updated title", updated["title"])
        self.assertEqual("auditor", updated["assignee"])
        self.assertEqual("blocked", updated["status"])
        self.assertEqual("2026-07-22", updated["due_date"])


if __name__ == "__main__":
    unittest.main()
