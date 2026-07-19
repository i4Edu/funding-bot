from __future__ import annotations

import base64
import os

from locust import HttpUser, between, task


def _basic_auth_header(role: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{role}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


class DashboardAdminUser(HttpUser):
    wait_time = between(0.1, 0.5)

    def on_start(self) -> None:
        role = os.environ.get("LOAD_TEST_ROLE", "admin")
        password = os.environ.get(
            "LOAD_TEST_PASSWORD", os.environ.get("ADMIN_PASSWORD", "admin-secret")
        )
        response = self.client.get(
            "/dashboard",
            headers=_basic_auth_header(role, password),
            name="GET /dashboard (authenticate)",
        )
        response.raise_for_status()

    @task(4)
    def dashboard(self) -> None:
        self.client.get("/dashboard", name="GET /dashboard")

    @task(3)
    def task_board(self) -> None:
        self.client.get("/dashboard/tasks?sort=-updated_at", name="GET /dashboard/tasks")

    @task(2)
    def settings(self) -> None:
        self.client.get("/settings", name="GET /settings")

    @task(2)
    def translations(self) -> None:
        self.client.get("/translations?status=pending", name="GET /translations")

    @task(2)
    def tasks_api(self) -> None:
        self.client.get("/tasks?assigned_to=admin&sort=due_date", name="GET /tasks")

    @task(1)
    def tasks_export(self) -> None:
        self.client.get("/api/tasks/export?assigned_to=admin", name="GET /api/tasks/export")

    @task(1)
    def metrics(self) -> None:
        self.client.get("/metrics", name="GET /metrics")
