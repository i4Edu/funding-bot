import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from funding_bot import FileVault, FundingBot

REGRESSION_VAULT_DIR = Path(".test_regression_vault")
DISCOVERED_AT = datetime(2026, 6, 22, 8, 30, tzinfo=timezone.utc)
SUMMARY_SENT_AT = datetime(2026, 6, 22, 9, 0, tzinfo=timezone.utc)
OUTREACH_SENT_AT = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
SUMMARY_REPORT_AT = datetime(2026, 6, 22, 18, 0, tzinfo=timezone.utc)


def _sample_opportunity(*, title: str, portal_url: str, summary: str, category: str = "Education"):
    return {
        "source": "Grants Portal",
        "donor_name": "UNICEF",
        "title": title,
        "portal_url": portal_url,
        "summary": summary,
        "tags": ["CSR funding", "education"],
        "category": category,
    }


def _normalize_report_snapshot(report):
    normalized = dict(report)
    normalized["generated_at"] = "<generated-at>"
    return normalized


@pytest.fixture
def bot():
    instance = FundingBot(trusted_sources={"Grants Portal"})
    instance.connection.execute("""
        CREATE TABLE IF NOT EXISTS funnel_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stage TEXT NOT NULL,
            entity_key TEXT NOT NULL,
            connector_name TEXT,
            opportunity_signature TEXT,
            task_id INTEGER,
            communication_id INTEGER,
            event_type TEXT,
            success INTEGER NOT NULL DEFAULT 1,
            happened_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        )
        """)
    instance.connection.commit()
    instance.store_organization_profile(
        {
            "name": "i4Edu",
            "mission": "Expand access to equitable education.",
        }
    )
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def file_vault_dir():
    if REGRESSION_VAULT_DIR.exists():
        shutil.rmtree(REGRESSION_VAULT_DIR)
    REGRESSION_VAULT_DIR.mkdir()
    try:
        yield REGRESSION_VAULT_DIR
    finally:
        if REGRESSION_VAULT_DIR.exists():
            shutil.rmtree(REGRESSION_VAULT_DIR)


def test_daily_summary_formatting_regression(bot, snapshot):
    signature = bot.discover_opportunities(
        [
            _sample_opportunity(
                title="UNICEF CSR Grant",
                portal_url="https://example.org/unicef",
                summary="CSR funding for nonprofit education programs.",
            )
        ],
        keywords=["csr funding", "education"],
        discovered_at=DISCOVERED_AT,
    )[0]["signature"]
    bot.submit_application(
        signature,
        submission_reference="summary-regression-ref",
        status="submitted",
        next_action="Await donor review",
        submitted_at=SUMMARY_SENT_AT,
    )
    bot.send_outreach(
        donor_email="donor@example.org",
        donor_name="Donor",
        subject_template="Support {organization_name}",
        body_template="Hello {donor_name},\n\n{mission}",
        sent_at=OUTREACH_SENT_AT,
    )
    bot.update_application_status(
        signature,
        status="pending",
        next_action="Awaiting confirmation",
    )

    summary = bot.build_daily_summary(
        recipient="lupael_i4e.team@example.org",
        report_date=SUMMARY_REPORT_AT,
    )

    assert summary == snapshot


def test_env_var_vault_backend_regression(monkeypatch):
    monkeypatch.setenv("PORTAL_SECRET", '{"username": "env-user", "password": "env-pass"}')
    bot = FundingBot(trusted_sources={"Grants Portal"})
    try:
        bot.register_credential("portal", "PORTAL_SECRET")

        assert bot.list_credentials() == [{"alias": "portal", "env_var_name": "PORTAL_SECRET"}]
        assert bot.resolve_credential("portal") == {
            "username": "env-user",
            "password": "env-pass",
        }
    finally:
        bot.close()


def test_file_vault_backend_regression(file_vault_dir):
    (file_vault_dir / "PORTAL_SECRET").write_text(
        '{"username": "vault-user", "password": "vault-pass"}\n',
        encoding="utf-8",
    )
    bot = FundingBot(trusted_sources={"Grants Portal"}, vault=FileVault(file_vault_dir))
    try:
        bot.register_credential("portal", "PORTAL_SECRET")

        assert bot.list_credentials() == [{"alias": "portal", "env_var_name": "PORTAL_SECRET"}]
        assert bot.resolve_credential("portal") == {
            "username": "vault-user",
            "password": "vault-pass",
        }
    finally:
        bot.close()


def test_monthly_audit_report_empty_month_regression(bot, monkeypatch, snapshot):
    monkeypatch.setattr(
        bot,
        "_utcnow",
        lambda: datetime(2026, 1, 31, 23, 59, tzinfo=timezone.utc),
    )

    report = bot.build_monthly_audit_report(year=2026, month=1)

    assert _normalize_report_snapshot(report) == snapshot


def test_monthly_audit_report_leap_year_regression(bot, monkeypatch, snapshot):
    leap_day = datetime(2024, 2, 29, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(bot, "_utcnow", lambda: leap_day)

    signature = bot.discover_opportunities(
        [
            _sample_opportunity(
                title="Leap Day Learning Fund",
                portal_url="https://example.org/leap-day",
                summary="Education funding announced on leap day.",
            )
        ],
        keywords=["education"],
        discovered_at=leap_day,
    )[0]["signature"]
    bot.submit_application(
        signature,
        submission_reference="leap-day-ref",
        status="submitted",
        next_action="Await donor review",
        submitted_at=leap_day,
    )
    bot.send_outreach(
        donor_email="leap@example.org",
        donor_name="Leap Donor",
        subject_template="Leap day support for {organization_name}",
        body_template="Hello {donor_name},\n\n{mission}",
        sent_at=leap_day,
    )
    communication_id = bot.connection.execute(
        "SELECT id FROM communications ORDER BY id DESC LIMIT 1"
    ).fetchone()["id"]
    bot.record_outreach_event(communication_id, "opened")
    bot.record_outreach_event(communication_id, "clicked")

    report = bot.build_monthly_audit_report(year=2024, month=2)

    assert _normalize_report_snapshot(report) == snapshot


def test_deduplication_results_snapshot(bot, snapshot):
    opportunities = [
        _sample_opportunity(
            title="UNICEF CSR Grant",
            portal_url="https://example.org/unicef",
            summary="CSR funding for nonprofit education programs.",
        ),
        _sample_opportunity(
            title="UNICEF CSR Grant",
            portal_url="https://example.org/unicef",
            summary="CSR funding for nonprofit education programs.",
        ),
        _sample_opportunity(
            title="STEM Expansion Challenge",
            portal_url="https://example.org/stem-expansion",
            summary="Education support for STEM classrooms and labs.",
            category="STEM",
        ),
        {
            "source": "Untrusted Source",
            "donor_name": "Ignore Me",
            "title": "Filtered listing",
            "portal_url": "https://bad.example/filtered",
            "summary": "Education funding from an untrusted source.",
            "tags": ["education"],
            "category": "Education",
        },
    ]

    first_run = bot.discover_opportunities(
        opportunities,
        keywords=["education", "stem"],
        discovered_at=DISCOVERED_AT,
    )
    second_run = bot.discover_opportunities(
        opportunities,
        keywords=["education", "stem"],
        discovered_at=DISCOVERED_AT,
    )
    snapshot_payload = {
        "first_run": first_run,
        "second_run": second_run,
        "stored": sorted(bot.list_opportunities(), key=lambda item: item["signature"]),
    }

    assert snapshot_payload == snapshot
