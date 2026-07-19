from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from funding_bot import FundingBot


def _seed_database(db_path: Path, artifacts_dir: Path) -> None:
    if db_path.exists():
        db_path.unlink()
    if artifacts_dir.exists():
        for path in sorted(artifacts_dir.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    bot = FundingBot(
        db_path=str(db_path),
        connector_configs={
            "connectors": [
                {"type": "grants-portal", "transport": "demo"},
                {"type": "csr-network", "transport": "demo"},
                {"type": "ngo-directory", "transport": "demo"},
                {"type": "foundation-directory", "transport": "demo", "credentials": {"api_key": "demo-key"}},
                {"type": "globalgiving", "transport": "demo"},
                {"type": "kickstarter-for-good", "transport": "demo"},
            ]
        },
    )
    try:
        bot.store_setting(
            "profile",
            {
                "name": "i4Edu",
                "mission": "Expand access to equitable education.",
                "website": "https://i4edu.example.org",
                "contact_email": "ops@i4edu.example.org",
                "privacy_email": "privacy@i4edu.example.org",
                "privacy_jurisdictions": ["EU", "US"],
            },
        )
        bot.store_search_settings(
            keywords=["education", "community", "innovation"],
            trusted_sources=[],
        )
        bot.register_credential("smtp", "SMTP_PASSWORD")

        discovered = bot.discover_opportunities(
            [
                {
                    "source": "Grants Portal",
                    "donor_name": "Future Fund",
                    "title": "Education Innovation Grant",
                    "portal_url": "https://example.org/opportunities/education-innovation",
                    "summary": "Funding for equitable education and digital learning.",
                    "category": "Education",
                    "tags": ["education", "innovation"],
                },
                {
                    "source": "CSR Network",
                    "donor_name": "Community Builders",
                    "title": "Community Learning Fund",
                    "portal_url": "https://example.org/opportunities/community-learning",
                    "summary": "Community support for learning hubs and educators.",
                    "category": "Education",
                    "tags": ["community", "education"],
                },
            ],
            keywords=["education", "community"],
            discovered_at=now - timedelta(days=1),
        )
        bot.submit_application(
            discovered[0]["signature"],
            submission_reference="seed-submission-1",
            status="submitted",
            next_action="Await donor review",
            submitted_at=now - timedelta(hours=6),
        )

        bot.create_task(
            title="Seed review task",
            assignee="admin",
            description="Review the seeded funding workflow.",
            status="todo",
            due_date=(now.date() + timedelta(days=3)).isoformat(),
            source="e2e_seed",
        )
        bot.create_task(
            title="Seed staff follow-up",
            assignee="staff",
            description="Coordinate donor follow-up.",
            status="in-progress",
            due_date=(now.date() + timedelta(days=5)).isoformat(),
            source="e2e_seed",
        )
    finally:
        bot.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Funding Bot E2E fixture server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5010)
    args = parser.parse_args()

    artifacts_dir = PROJECT_ROOT / ".test-artifacts" / "e2e"
    db_path = artifacts_dir / "funding-bot-e2e.db"
    privacy_dir = artifacts_dir / "privacy-policies"

    os.environ["BOT_DB_PATH"] = str(db_path)
    os.environ["ADMIN_PASSWORD"] = "admin-secret"
    os.environ["STAFF_PASSWORD"] = "staff-secret"
    os.environ["AUDITOR_PASSWORD"] = "auditor-secret"
    os.environ["SESSION_COOKIE_SECURE"] = "0"
    os.environ["DATA_RESIDENCY"] = "EU"
    os.environ["DATA_STORAGE_REGION"] = "EU"
    os.environ["PRIVACY_POLICY_OUTPUT_DIR"] = str(privacy_dir)
    os.environ["SMTP_PASSWORD"] = "demo-secret"
    os.environ["FUNDING_BOT_CONNECTORS"] = json.dumps(
        {
            "connectors": [
                {"type": "grants-portal", "transport": "demo"},
                {"type": "csr-network", "transport": "demo"},
                {"type": "ngo-directory", "transport": "demo"},
                {"type": "foundation-directory", "transport": "demo", "credentials": {"api_key": "demo-key"}},
                {"type": "globalgiving", "transport": "demo"},
                {"type": "kickstarter-for-good", "transport": "demo"},
            ]
        }
    )

    _seed_database(db_path, artifacts_dir)

    from web.app import app

    app.config["TESTING"] = False
    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
