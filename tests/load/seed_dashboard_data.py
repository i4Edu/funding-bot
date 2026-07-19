from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from funding_bot import FundingBot


def seed_dashboard_data(db_path: str, *, opportunities: int, tasks: int) -> None:
    database_path = Path(db_path)
    if database_path.exists():
        database_path.unlink()

    bot = FundingBot(
        db_path=str(database_path),
        trusted_sources={"Grants Portal", "CSR Network"},
    )
    try:
        bot.store_organization_profile(
            {
                "name": "i4Edu",
                "mission": "Expand access to equitable education.",
                "contact_email": "ops@i4edu.example.org",
                "website": "https://i4edu.example.org",
            }
        )
        bot.store_search_settings(
            keywords=["education", "digital learning", "csr"],
            trusted_sources=["Grants Portal", "CSR Network"],
        )

        now = datetime.now(timezone.utc)
        discovered = bot.discover_opportunities(
            [
                {
                    "source": "Grants Portal" if index % 2 == 0 else "CSR Network",
                    "donor_name": f"Donor {index % 12}",
                    "title": f"Education Opportunity {index}",
                    "portal_url": f"https://example.org/opportunities/{index}",
                    "summary": "Funding for equitable education and digital learning.",
                    "tags": ["education", "digital learning", "csr"],
                    "category": "Education",
                }
                for index in range(opportunities)
            ],
            keywords=["education", "digital learning", "csr"],
            discovered_at=now,
        )

        for index, opportunity in enumerate(discovered[: max(1, opportunities // 4)]):
            bot.submit_application(
                opportunity["signature"],
                submission_reference=f"submission-{index}",
                status="submitted" if index % 2 == 0 else "in_review",
                next_action="Await donor review",
                submitted_at=now - timedelta(hours=index),
            )

        statuses = ["pending", "in_progress", "done", "blocked"]
        assignees = ["admin", "staff", "auditor"]
        for index in range(tasks):
            due_date = (now.date() + timedelta(days=(index % 9) - 3)).isoformat()
            bot.create_task(
                title=f"Load test task {index}",
                assignee=assignees[index % len(assignees)],
                description="Dashboard load-test seed task.",
                status=statuses[index % len(statuses)],
                due_date=due_date,
                source="load_test_seed",
            )

        for index in range(18):
            review = bot.submit_translation_review(
                locale="bn" if index % 2 == 0 else "en",
                translation_key=f"dashboard.metric.{index}",
                source_text=f"Dashboard metric label {index}",
                translated_text=f"Translated metric label {index}",
                submitted_by_role="admin",
                created_at=now - timedelta(minutes=index),
            )
            if index % 3 == 1:
                bot.review_translation(
                    review["id"],
                    status="approved",
                    reviewed_by_role="auditor",
                    reviewer_notes="Looks good.",
                    reviewed_at=now - timedelta(minutes=index - 1),
                )
            elif index % 3 == 2:
                bot.review_translation(
                    review["id"],
                    status="rejected",
                    reviewed_by_role="auditor",
                    reviewer_notes="Needs terminology update.",
                    reviewed_at=now - timedelta(minutes=index - 1),
                )
    finally:
        bot.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed dashboard data for load testing.")
    parser.add_argument("--db-path", default=".load-test-dashboard.db")
    parser.add_argument("--opportunities", type=int, default=60)
    parser.add_argument("--tasks", type=int, default=90)
    args = parser.parse_args()
    seed_dashboard_data(args.db_path, opportunities=args.opportunities, tasks=args.tasks)


if __name__ == "__main__":
    main()
