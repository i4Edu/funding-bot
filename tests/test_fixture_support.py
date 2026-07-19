from __future__ import annotations


def test_bot_factory_shares_a_single_in_memory_database(bot_factory) -> None:
    first = bot_factory()
    first.store_organization_profile({"name": "Shared Memory Org", "mission": "Test shared state"})

    second = bot_factory()
    assert second.load_organization_profile()["name"] == "Shared Memory Org"


def test_seeded_database_populates_expected_records(bot_factory, seeded_database: dict[str, object]) -> None:
    bot = bot_factory()

    opportunities = bot.list_opportunities()
    tasks = bot.list_tasks()
    donors = bot.list_donors()
    reviews = bot.list_translation_reviews()

    assert seeded_database["organization_name"] == "i4Edu"
    assert len(opportunities) == seeded_database["counts"]["opportunities"]
    assert len(tasks) == seeded_database["counts"]["tasks"]
    assert len(donors) == seeded_database["counts"]["donors"]
    assert len(reviews) == seeded_database["counts"]["translation_reviews"]


def test_seeded_app_client_reads_seed_data(seeded_app_client: dict[str, object]) -> None:
    client = seeded_app_client["client"]
    admin_headers = seeded_app_client["admin_headers"]
    seed_data = seeded_app_client["seed_data"]

    opportunities = client.get("/opportunities", headers=admin_headers)
    tasks = client.get("/tasks", headers=admin_headers)

    assert opportunities.status_code == 200
    assert len(opportunities.get_json()) == seed_data["counts"]["opportunities"]
    assert tasks.status_code == 200
    assert any(task["id"] == seed_data["task_id"] for task in tasks.get_json())
