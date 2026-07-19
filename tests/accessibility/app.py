from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, render_template, request

app = Flask(
    __name__,
    template_folder=str(Path(__file__).resolve().parents[2] / "web" / "templates"),
    static_folder=str(Path(__file__).resolve().parents[2] / "web" / "static"),
)


@app.get("/dashboard", endpoint="dashboard")
def dashboard_page() -> str:
    return render_template(
        "dashboard.html",
        current_role="admin",
        new_opportunities_count=3,
        applications_submitted_count=4,
        pending_applications_count=2,
        donor_communications_count=5,
        my_tasks_count=6,
        my_open_tasks_count=2,
        overdue_tasks_count=1,
        overdue_tasks=[
            {
                "title": "Submit impact budget",
                "assigned_to": "admin",
                "description": "Finalize the overdue budget appendix.",
                "due_date": "2026-07-15",
            }
        ],
        recent_opportunities=[
            {
                "title": "Education Innovation Grant",
                "donor_name": "Future Fund",
                "status": "new",
                "discovered_at": "2026-07-18T10:00:00Z",
            }
        ],
        recent_applications=[
            {
                "title": "Community Learning Fund",
                "donor_name": "Civic Foundation",
                "status": "pending_review",
                "submitted_at": "2026-07-17T09:00:00Z",
                "next_action": "Awaiting confirmation",
            }
        ],
    )


@app.get("/dashboard/tasks", endpoint="dashboard_tasks")
def tasks_page() -> str:
    return render_template(
        "tasks.html",
        current_role="admin",
        total_tasks=3,
        task_counts={"todo": 1, "in-progress": 1, "done": 1},
        can_filter_all_assignees=True,
        task_assignee_options=["admin", "staff"],
        task_filters={
            "assignee": "",
            "status": "",
            "due_date_after": "",
            "due_date_before": "",
            "sort": "updated_at_desc",
        },
        task_sort_options=[
            ("updated_at_desc", "Recently updated"),
            ("due_date_asc", "Due date"),
        ],
        task_columns={
            "todo": [],
            "in-progress": [
                {
                    "id": "task-1",
                    "title": "Review grant checklist",
                    "description": "Validate the next submission batch.",
                    "assigned_to": "admin",
                    "status": "in-progress",
                    "due_date": "2026-07-20",
                    "updated_at": "2026-07-18T11:00:00Z",
                    "is_overdue": False,
                    "can_move": True,
                }
            ],
            "done": [],
            "blocked": [],
        },
        tasks=[
            {
                "title": "Review grant checklist",
                "description": "Validate the next submission batch.",
                "assigned_to": "admin",
                "status": "in-progress",
                "due_date": "2026-07-20",
                "updated_at": "2026-07-18T11:00:00Z",
            }
        ],
    )


@app.get("/settings", endpoint="settings_page")
def settings_page() -> str:
    return render_template(
        "settings.html",
        current_role="admin",
        ui_locale={"code": "en", "direction": "ltr", "is_rtl": False},
        organization_profile={"name": "i4Edu", "mission": "Expand access"},
        residency_status={
            "data_residency": "EU",
            "cross_border_transfers": ["none"],
            "retention_days": 365,
        },
        privacy_policy_versions=[
            {
                "version": "2026.07",
                "effective_at": "2026-07-01",
                "locale": "en",
            }
        ],
        search_settings={"keywords": ["education", "csr"], "trusted_sources": ["fund.example"]},
        credentials=[{"alias": "smtp", "env_var_name": "SMTP_PASSWORD"}],
        smtp_configured=False,
        smtp_host="",
    )


@app.get("/translations", endpoint="translation_review_dashboard")
def translation_review_dashboard() -> str:
    return render_template(
        "translations.html",
        current_role="admin",
        ui_locale={"code": "ar", "direction": "rtl", "is_rtl": True},
        review_counts={"pending": 1, "approved": 1, "rejected": 1},
        selected_status="",
        selected_review_locale="ar",
        supported_locales=[
            {
                "code": "en",
                "direction": "ltr",
                "display_name": "English",
                "native_name": "English",
                "is_rtl": False,
            },
            {
                "code": "ar",
                "direction": "rtl",
                "display_name": "Arabic",
                "native_name": "العربية",
                "is_rtl": True,
            },
        ],
        reviews=[
            {
                "id": 1,
                "locale": "ar",
                "locale_metadata": {"display_name": "Arabic"},
                "translation_key": "outreach.default.subject",
                "created_at": "2026-07-18T10:00:00Z",
                "source_text": "Thank you for supporting our mission.",
                "translated_text": "شكرًا لدعم رسالتنا.",
                "submitter_notes": "Use formal tone.",
                "status": "pending",
                "reviewed_by_role": None,
                "reviewed_at": None,
            }
        ],
    )


@app.get("/tasks", endpoint="list_tasks_route")
def list_tasks_route():
    return jsonify(
        {
            "filters": request.args.to_dict(),
            "tasks": [
                {
                    "title": "Review grant checklist",
                    "assigned_to": "admin",
                    "status": "in-progress",
                }
            ],
        }
    )
