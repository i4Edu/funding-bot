from __future__ import annotations

import pytest

pytestmark = pytest.mark.smoke


@pytest.mark.quick
def test_health_endpoints_report_ready_state(smoke_client: dict[str, object]) -> None:
    client = smoke_client["client"]

    health = client.get("/health")
    ready = client.get("/ready")
    queue = client.get("/health/queue")

    assert health.status_code == 200
    assert health.get_json()["status"] == "ok"
    assert health.get_json()["healthy"] is True
    assert health.get_json()["queue"]["mode"] == "cron"
    assert ready.status_code == 200
    assert ready.get_json()["ready"] is True
    assert ready.get_json()["checks"]["database"]["status"] == "ok"
    assert ready.get_json()["checks"]["redis"]["status"] in {"ok", "disabled"}
    assert queue.status_code == 200
    assert queue.get_json()["status"] == "disabled"
    assert queue.get_json()["queue_depth"] == 0


@pytest.mark.quick
def test_admin_session_supports_dashboard_settings_and_metrics_navigation(
    smoke_client: dict[str, object],
) -> None:
    client = smoke_client["client"]
    admin_headers = smoke_client["admin_headers"]

    dashboard = client.get("/dashboard", headers=admin_headers)
    settings = client.get("/settings")
    metrics = client.get("/metrics")

    assert dashboard.status_code == 200
    assert b"Dashboard" in dashboard.data
    assert settings.status_code == 200
    assert b"Settings" in settings.data
    assert metrics.status_code == 200
    assert b"funding_bot_opportunities_total" in metrics.data


def test_discovery_submission_and_reporting_flow(smoke_client: dict[str, object]) -> None:
    client = smoke_client["client"]
    admin_headers = smoke_client["admin_headers"]

    profile = client.post(
        "/settings/organization",
        json={"name": "i4Edu", "mission": "Expand access to education."},
        headers=admin_headers,
    )
    search = client.post(
        "/settings/search",
        json={"keywords": ["education"], "trusted_sources": ["Grants Portal", "CSR Network"]},
        headers=admin_headers,
    )
    discovery = client.post(
        "/settings/discover", json={"keywords": ["education"]}, headers=admin_headers
    )

    assert profile.status_code == 200
    assert search.status_code == 200
    assert discovery.status_code == 200
    payload = discovery.get_json()
    assert payload["count"] >= 1

    opportunities = client.get("/opportunities", headers=admin_headers)
    assert opportunities.status_code == 200
    first_opportunity = opportunities.get_json()[0]
    signature = first_opportunity["signature"]

    submit = client.post(
        f"/opportunities/{signature}/submit",
        json={
            "status": "submitted",
            "next_action": "Await donor review",
            "submission_reference": "smoke-ref-001",
        },
        headers=admin_headers,
    )
    detail = client.get(f"/opportunities/{signature}", headers=admin_headers)
    audit_log = client.get("/audit-log", headers=admin_headers)

    assert submit.status_code == 201
    assert detail.status_code == 200
    assert detail.get_json()["application"]["status"] == "submitted"
    assert any(entry["action"] == "application_submitted" for entry in audit_log.get_json())


def test_task_board_and_assignment_flow(smoke_client: dict[str, object]) -> None:
    client = smoke_client["client"]
    admin_headers = smoke_client["admin_headers"]
    staff_headers = smoke_client["staff_headers"]

    created = client.post(
        "/tasks",
        json={
            "title": "Prepare donor update",
            "description": "Draft a concise program update",
            "assignee": "staff",
            "status": "pending",
            "due_date": "2026-07-30",
        },
        headers=admin_headers,
    )
    assert created.status_code == 201
    task_id = created.get_json()["task"]["id"]

    task_list = client.get("/tasks", headers=staff_headers)
    board = client.get("/dashboard/tasks", headers=staff_headers)
    transition = client.post(
        f"/tasks/{task_id}/status",
        json={"status": "in_progress"},
        headers=staff_headers,
    )
    updated = client.get(f"/tasks/{task_id}", headers=staff_headers)

    assert task_list.status_code == 200
    assert any(task["id"] == task_id for task in task_list.get_json())
    assert board.status_code == 200
    assert b"Task Board" in board.data
    assert transition.status_code == 200
    assert updated.status_code == 200
    assert updated.get_json()["status"] == "in_progress"


def test_donor_outreach_and_analytics_flow(smoke_client: dict[str, object]) -> None:
    client = smoke_client["client"]
    admin_headers = smoke_client["admin_headers"]
    auditor_headers = smoke_client["auditor_headers"]

    donor = client.post(
        "/donors",
        json={"email": "donor@example.org", "name": "Donor Example", "locale": "bn"},
        headers=admin_headers,
    )
    outreach = client.post(
        "/settings/test-outreach",
        json={"email": "donor@example.org", "name": "Donor Example", "dry_run": True},
        headers=admin_headers,
    )
    analytics = client.get("/analytics", headers=auditor_headers)

    assert donor.status_code == 201
    assert outreach.status_code == 201
    assert outreach.get_json()["dry_run"] is True
    assert analytics.status_code == 200
    assert analytics.get_json()["stats"]["total_sent"] >= 1
