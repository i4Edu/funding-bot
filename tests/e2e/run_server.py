from __future__ import annotations

import argparse
import base64
import json
import os
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from flask import Flask, Response, g, jsonify, redirect, render_template, request, session, url_for

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_ROOT = PROJECT_ROOT / "web" / "templates"
STATIC_ROOT = PROJECT_ROOT / "web" / "static"
ARTIFACTS_ROOT = PROJECT_ROOT / ".test-artifacts" / "e2e"
PRIVACY_ROOT = ARTIFACTS_ROOT / "privacy-policies"

app = Flask(__name__, template_folder=str(TEMPLATE_ROOT), static_folder=str(STATIC_ROOT))
app.config["SECRET_KEY"] = "e2e-secret"
app.config["SESSION_COOKIE_SECURE"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

ROLE_PASSWORDS = {
    "admin": "admin-secret",
    "staff": "staff-secret",
    "auditor": "auditor-secret",
}
TASK_STATUSES = ("todo", "in-progress", "done", "blocked")
TASK_STATUS_LABELS = {
    "todo": "Todo",
    "in-progress": "In Progress",
    "done": "Done",
    "blocked": "Blocked",
}
TASK_STATUS_TRANSITIONS = {
    "todo": {"in-progress", "blocked"},
    "in-progress": {"todo", "done", "blocked"},
    "blocked": {"todo", "in-progress"},
    "done": set(),
}
TASK_SORT_OPTIONS = (
    ("updated_at", "Recently updated"),
    ("-updated_at", "Least recently updated"),
    ("assignee", "Assignee (A-Z)"),
    ("-assignee", "Assignee (Z-A)"),
    ("status", "Status (A-Z)"),
    ("-status", "Status (Z-A)"),
    ("due_date", "Due date (earliest first)"),
    ("-due_date", "Due date (latest first)"),
)
SUPPORTED_LOCALES = [
    {
        "code": "en",
        "direction": "ltr",
        "display_name": "English",
        "native_name": "English",
        "is_rtl": False,
    },
    {
        "code": "bn",
        "direction": "ltr",
        "display_name": "Bengali",
        "native_name": "বাংলা",
        "is_rtl": False,
    },
    {
        "code": "ar",
        "direction": "rtl",
        "display_name": "Arabic",
        "native_name": "العربية",
        "is_rtl": True,
    },
]


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _initial_state() -> dict[str, Any]:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    return {
        "organization_profile": {
            "name": "i4Edu",
            "mission": "Expand access to equitable education.",
            "website": "https://i4edu.example.org",
            "contact_email": "ops@i4edu.example.org",
            "privacy_email": "privacy@i4edu.example.org",
            "privacy_jurisdictions": ["EU", "US"],
        },
        "search_settings": {
            "keywords": ["education", "community", "innovation"],
            "trusted_sources": [],
        },
        "credentials": [{"alias": "smtp", "env_var_name": "SMTP_PASSWORD"}],
        "privacy_policy_versions": [],
        "communications": [],
        "opportunities": [
            {
                "signature": "opp-1",
                "source": "Grants Portal",
                "donor_name": "Future Fund",
                "title": "Education Innovation Grant",
                "portal_url": "https://example.org/opportunities/education-innovation",
                "summary": "Funding for equitable education and digital learning.",
                "category": "Education",
                "discovered_at": (now - timedelta(days=1)).isoformat(),
                "status": "new",
            },
            {
                "signature": "opp-2",
                "source": "CSR Network",
                "donor_name": "Community Builders",
                "title": "Community Learning Fund",
                "portal_url": "https://example.org/opportunities/community-learning",
                "summary": "Community support for learning hubs and educators.",
                "category": "Education",
                "discovered_at": (now - timedelta(hours=12)).isoformat(),
                "status": "new",
            },
        ],
        "applications": [
            {
                "opportunity_signature": "opp-1",
                "title": "Education Innovation Grant",
                "donor_name": "Future Fund",
                "status": "submitted",
                "next_action": "Await donor review",
                "submission_reference": "seed-submission-1",
                "submitted_at": (now - timedelta(hours=6)).isoformat(),
            }
        ],
        "tasks": [
            {
                "id": 1,
                "title": "Seed review task",
                "description": "Review the seeded funding workflow.",
                "assignee": "admin",
                "assigned_to": "admin",
                "status": "todo",
                "due_date": "2026-07-24",
                "source": "e2e_seed",
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "is_overdue": False,
                "unread_comment_count": 0,
            },
            {
                "id": 2,
                "title": "Seed staff follow-up",
                "description": "Coordinate donor follow-up.",
                "assignee": "staff",
                "assigned_to": "staff",
                "status": "in-progress",
                "due_date": "2026-07-26",
                "source": "e2e_seed",
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
                "is_overdue": False,
                "unread_comment_count": 0,
            },
        ],
        "next_task_id": 3,
    }


STATE = _initial_state()


def _reset_artifacts() -> None:
    if ARTIFACTS_ROOT.exists():
        for path in sorted(ARTIFACTS_ROOT.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    ARTIFACTS_ROOT.mkdir(parents=True, exist_ok=True)
    PRIVACY_ROOT.mkdir(parents=True, exist_ok=True)


_reset_artifacts()


def _challenge(message: str = "Authentication required") -> Response:
    response = jsonify({"error": message})
    response.status_code = 401
    response.headers["WWW-Authenticate"] = 'Basic realm="Funding Bot E2E"'
    return response


SESSION_ROLE_KEY = "authenticated_role"


def _establish_session(role: str) -> None:
    session[SESSION_ROLE_KEY] = role


def _get_authenticated_role() -> str:
    role = session.get(SESSION_ROLE_KEY)
    if isinstance(role, str) and role in ROLE_PASSWORDS:
        return role
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        raise PermissionError("Authentication required")
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
    except Exception as exc:  # pragma: no cover - defensive
        raise PermissionError("Invalid authentication credentials") from exc
    username, _, password = decoded.partition(":")
    role = username.strip().lower()
    if ROLE_PASSWORDS.get(role) != password:
        raise PermissionError("Invalid authentication credentials")
    _establish_session(role)
    return role


def require_role(*roles: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    allowed = {role.lower() for role in roles}

    def decorator(view_func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view_func)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            try:
                role = _get_authenticated_role()
            except PermissionError as exc:
                return _challenge(str(exc))
            if role not in allowed:
                return jsonify({"error": "Forbidden"}), 403
            g.current_role = role
            return view_func(*args, **kwargs)

        return wrapped

    return decorator


@app.get("/health")
def health() -> Response:
    return jsonify({"status": "ok"})


@app.get("/")
def index() -> Response:
    return redirect(url_for("dashboard"))


@app.get("/dashboard", endpoint="dashboard")
@require_role("admin", "staff", "auditor")
def dashboard() -> str:
    role = getattr(g, "current_role", None)
    visible_tasks = _visible_tasks(role)
    overdue_tasks = [task for task in visible_tasks if _is_overdue(task)]
    return render_template(
        "dashboard.html",
        current_role=role,
        new_opportunities_count=len(STATE["opportunities"]),
        applications_submitted_count=len(STATE["applications"]),
        pending_applications_count=sum(
            1
            for app in STATE["applications"]
            if app["status"] in {"pending", "submitted", "in_review"}
        ),
        donor_communications_count=len(STATE["communications"]),
        my_tasks_count=len(visible_tasks),
        my_open_tasks_count=sum(1 for task in visible_tasks if task["status"] != "done"),
        overdue_tasks_count=len(overdue_tasks),
        overdue_tasks=overdue_tasks[:5],
        recent_opportunities=sorted(
            STATE["opportunities"], key=lambda item: item["discovered_at"], reverse=True
        )[:10],
        recent_applications=sorted(
            STATE["applications"], key=lambda item: item["submitted_at"], reverse=True
        )[:10],
        tracing_summary={"enabled": False, "exporter": "disabled"},
        slo_summary=[],
    )


@app.get("/settings", endpoint="settings_page")
@require_role("admin", "staff", "auditor")
def settings_page() -> str:
    return render_template(
        "settings.html",
        current_role=getattr(g, "current_role", None),
        ui_locale=SUPPORTED_LOCALES[0],
        organization_profile=deepcopy(STATE["organization_profile"]),
        residency_status={"data_residency": "EU", "storage_region": "EU"},
        privacy_policy_versions=deepcopy(STATE["privacy_policy_versions"]),
        search_settings=deepcopy(STATE["search_settings"]),
        credentials=deepcopy(STATE["credentials"]),
        smtp_configured=False,
        smtp_host="",
        supported_locales=SUPPORTED_LOCALES,
        queue_monitoring={
            "flower": {"url": "http://127.0.0.1:5555"},
            "queue": {
                "status": "disabled",
                "queue_name": "funding-bot",
                "worker_count": 0,
                "queue_depth": 0,
                "active_tasks": 0,
            },
            "task_metrics": {
                "retries_scheduled": 0,
                "duration_seconds_average": 0.0,
                "duration_seconds_max": 0.0,
            },
        },
    )


@app.get("/translations", endpoint="translation_review_dashboard")
@require_role("admin", "staff", "auditor")
def translation_review_dashboard() -> str:
    return render_template(
        "translations.html",
        current_role=getattr(g, "current_role", None),
        ui_locale=SUPPORTED_LOCALES[0],
        review_counts={"pending": 0, "approved": 0, "rejected": 0},
        selected_status="",
        selected_review_locale="",
        supported_locales=SUPPORTED_LOCALES,
        reviews=[],
    )


@app.post("/settings/organization")
@require_role("admin")
def update_organization_settings() -> Response:
    payload = request.get_json(force=True)
    STATE["organization_profile"].update(payload)
    return jsonify({"organization_profile": deepcopy(STATE["organization_profile"])})


@app.post("/settings/search")
@require_role("admin")
def update_search_settings() -> Response:
    payload = request.get_json(force=True)
    STATE["search_settings"] = {
        "keywords": _normalize_list(payload.get("keywords", [])),
        "trusted_sources": _normalize_list(payload.get("trusted_sources", [])),
    }
    return jsonify({"search_settings": deepcopy(STATE["search_settings"])})


@app.post("/settings/credentials")
@require_role("admin")
def register_credential_route() -> Response:
    payload = request.get_json(force=True)
    STATE["credentials"].append(
        {
            "alias": str(payload.get("alias", "")).strip(),
            "env_var_name": str(payload.get("env_var_name", "")).strip(),
        }
    )
    return jsonify({"credentials": deepcopy(STATE["credentials"])})


@app.post("/settings/discover")
@require_role("admin")
def run_discovery_now() -> Response:
    payload = request.get_json(silent=True) or {}
    keywords = _normalize_list(payload.get("keywords", STATE["search_settings"]["keywords"]))
    candidates = [
        {
            "signature": f"discovery-{len(STATE['opportunities']) + 1}",
            "source": "GlobalGiving",
            "donor_name": "GlobalGiving",
            "title": "Education Community Accelerator",
            "portal_url": "https://example.org/opportunities/education-community-accelerator",
            "summary": "Supports education, community, and innovation partnerships.",
            "category": "Education",
            "discovered_at": _iso_now(),
            "status": "new",
        },
        {
            "signature": f"discovery-{len(STATE['opportunities']) + 2}",
            "source": "Kickstarter for Good",
            "donor_name": "Kickstarter for Good",
            "title": "Innovation Makerspace Campaign",
            "portal_url": "https://example.org/opportunities/innovation-makerspace",
            "summary": "Innovation campaign for equitable learning spaces.",
            "category": "Innovation",
            "discovered_at": _iso_now(),
            "status": "new",
        },
    ]
    discovered = []
    for item in candidates:
        searchable = f"{item['title']} {item['summary']} {item['category']}".lower()
        if keywords and not any(keyword.lower() in searchable for keyword in keywords):
            continue
        if item["title"] not in {existing["title"] for existing in STATE["opportunities"]}:
            STATE["opportunities"].append(item)
            discovered.append(item)
    return jsonify(
        {
            "mode": "cron",
            "legacy_cron_enabled": True,
            "count": len(discovered),
            "new_opportunities": discovered,
        }
    )


@app.post("/settings/privacy-policy")
@require_role("admin")
def generate_privacy_policy() -> Response:
    payload = request.get_json(force=True)
    effective_date = str(payload.get("effective_date") or date.today().isoformat())
    jurisdiction = _normalize_list(payload.get("jurisdictions", ["EU"]))[0]
    version = f"{jurisdiction}-{effective_date}"
    html_path = PRIVACY_ROOT / f"{version}.html"
    pdf_path = PRIVACY_ROOT / f"{version}.pdf"
    html_path.write_text(f"<html><body><h1>{version}</h1></body></html>", encoding="utf-8")
    pdf_path.write_bytes(f"PDF placeholder for {version}\n".encode("utf-8"))
    record = {
        "version": version,
        "jurisdiction": jurisdiction,
        "generated_at": _iso_now(),
        "html_path": str(html_path),
        "pdf_path": str(pdf_path),
    }
    STATE["privacy_policy_versions"].insert(0, record)
    return (
        jsonify(
            {
                "policies": [record],
                "residency_status": {"data_residency": "EU", "storage_region": "EU"},
                "versions": deepcopy(STATE["privacy_policy_versions"]),
            }
        ),
        201,
    )


@app.post("/settings/test-outreach")
@require_role("admin")
def send_test_outreach() -> Response:
    payload = request.get_json(force=True)
    result = {
        "email": str(payload.get("email", "")).strip(),
        "name": str(payload.get("name", "")).strip(),
        "dry_run": bool(payload.get("dry_run", True)),
        "subject": "Thank you for supporting i4Edu",
    }
    STATE["communications"].append({**result, "sent_at": _iso_now()})
    return jsonify(result), 201


@app.get("/dashboard/tasks", endpoint="dashboard_tasks")
@require_role("admin", "staff", "auditor")
def dashboard_tasks() -> str:
    role = getattr(g, "current_role", None)
    filters = {
        "assignee": (request.args.get("assignee") or "").strip()
        or (None if role in {"admin", "auditor"} else role),
        "status": (request.args.get("status") or "").strip() or None,
        "due_date_after": (request.args.get("due_date_after") or "").strip() or None,
        "due_date_before": (request.args.get("due_date_before") or "").strip() or None,
        "sort": (request.args.get("sort") or "updated_at").strip(),
    }
    tasks = _list_tasks(
        filters["assignee"],
        filters["status"],
        filters["due_date_after"],
        filters["due_date_before"],
        filters["sort"],
        role,
    )
    grouped = {status: [] for status in TASK_STATUSES}
    for task in tasks:
        grouped[task["status"]].append(task)
    counts = {status: len(grouped[status]) for status in TASK_STATUSES}
    return render_template(
        "tasks.html",
        current_role=role,
        tasks=tasks,
        task_columns=grouped,
        task_counts=counts,
        total_tasks=len(tasks),
        task_filters=filters,
        task_board_columns=TASK_STATUSES,
        task_status_labels=TASK_STATUS_LABELS,
        task_sort_options=TASK_SORT_OPTIONS,
        task_assignee_options=sorted({task["assigned_to"] for task in STATE["tasks"]}),
        task_assignment_options=["admin", "auditor", "staff"],
        can_filter_all_assignees=role in {"admin", "auditor"},
        can_reassign_tasks=role == "admin",
        can_manage_tasks=role == "admin",
        can_export_tasks=role in {"admin", "auditor"},
        ui_locale=SUPPORTED_LOCALES[0],
    )


@app.get("/tasks", endpoint="list_tasks_route")
@require_role("admin", "staff", "auditor")
def list_tasks_route() -> Response:
    role = getattr(g, "current_role", None)
    tasks = _list_tasks(
        request.args.get("assignee") or request.args.get("assigned_to"),
        request.args.get("status"),
        request.args.get("due_date_after"),
        request.args.get("due_date_before"),
        request.args.get("sort") or "updated_at",
        role,
    )
    return jsonify(tasks)


@app.get("/api/tasks/export", endpoint="export_tasks_route")
@require_role("admin", "auditor")
def export_tasks_route() -> Response:
    tasks = _list_tasks(
        request.args.get("assignee") or request.args.get("assigned_to"),
        request.args.get("status"),
        request.args.get("due_date_after"),
        request.args.get("due_date_before"),
        request.args.get("sort") or "updated_at",
        getattr(g, "current_role", None),
    )
    return jsonify({"tasks": tasks, "count": len(tasks)})


@app.get("/monitoring/queue")
@require_role("admin", "staff", "auditor")
def queue_monitoring_route() -> Response:
    return jsonify(
        {
            "flower": {"url": "http://127.0.0.1:5555"},
            "queue": {
                "status": "disabled",
                "queue_name": "funding-bot",
                "worker_count": 0,
                "queue_depth": 0,
                "active_tasks": 0,
            },
            "task_metrics": {
                "retries_scheduled": 0,
                "duration_seconds_average": 0.0,
                "duration_seconds_max": 0.0,
            },
        }
    )


@app.post("/tasks")
@require_role("admin")
def create_task_route() -> Response:
    payload = request.get_json(force=True)
    now = _iso_now()
    task = {
        "id": STATE["next_task_id"],
        "title": str(payload.get("title", "")).strip(),
        "description": str(payload.get("description", "")).strip(),
        "assignee": str(payload.get("assignee", "admin")).strip(),
        "assigned_to": str(payload.get("assignee", "admin")).strip(),
        "status": _normalize_status(str(payload.get("status", "todo"))),
        "due_date": str(payload.get("due_date", "")).strip(),
        "source": "manual",
        "created_at": now,
        "updated_at": now,
        "is_overdue": False,
        "unread_comment_count": 0,
    }
    STATE["next_task_id"] += 1
    STATE["tasks"].append(task)
    return jsonify({"task": deepcopy(task)}), 201


@app.put("/tasks/<int:task_id>")
@require_role("admin")
def update_task_route(task_id: int) -> Response:
    payload = request.get_json(force=True)
    task = _get_task(task_id)
    task["title"] = str(payload.get("title", task["title"])).strip()
    task["description"] = str(payload.get("description", task["description"])).strip()
    assignee = str(payload.get("assignee", task["assigned_to"])).strip()
    task["assignee"] = assignee
    task["assigned_to"] = assignee
    task["status"] = _normalize_status(str(payload.get("status", task["status"])))
    task["due_date"] = str(payload.get("due_date", task["due_date"])).strip()
    task["updated_at"] = _iso_now()
    return jsonify({"task": deepcopy(task)})


@app.post("/tasks/<int:task_id>/status")
@require_role("admin", "staff")
def transition_task_status_route(task_id: int) -> Response:
    task = _get_task(task_id)
    role = getattr(g, "current_role", None)
    if role != "admin" and task["assigned_to"] != role:
        return jsonify({"error": "Forbidden"}), 403
    payload = request.get_json(force=True)
    next_status = _normalize_status(str(payload.get("status", "")))
    if next_status not in TASK_STATUS_TRANSITIONS.get(task["status"], set()):
        return (
            jsonify({"error": f"Task cannot transition from {task['status']} to {next_status}."}),
            400,
        )
    previous_status = task["status"]
    task["status"] = next_status
    task["updated_at"] = _iso_now()
    return jsonify(
        {
            "task": deepcopy(task),
            "notification": f"Task moved from {previous_status} to {next_status}.",
        }
    )


@app.errorhandler(404)
def not_found(_: Any) -> Response:
    return jsonify({"error": "Not found"}), 404


def _normalize_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items = value.split(",")
    else:
        items = value or []
    return [str(item).strip() for item in items if str(item).strip()]


def _normalize_status(status: str) -> str:
    normalized = status.strip().lower().replace("_", "-")
    aliases = {"pending": "todo", "completed": "done", "in_progress": "in-progress"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in TASK_STATUSES:
        raise ValueError(f"Invalid task status {status!r}")
    return normalized


def _get_task(task_id: int) -> dict[str, Any]:
    for task in STATE["tasks"]:
        if task["id"] == task_id:
            return task
    raise KeyError(task_id)


def _is_overdue(task: dict[str, Any]) -> bool:
    return bool(
        task.get("due_date") and task["status"] != "done" and task["due_date"] < "2026-07-19"
    )


def _visible_tasks(role: str | None) -> list[dict[str, Any]]:
    tasks = [deepcopy(task) for task in STATE["tasks"]]
    if role not in {"admin", "auditor"}:
        tasks = [task for task in tasks if task["assigned_to"] == role]
    for task in tasks:
        task["can_move"] = role == "admin" or task["assigned_to"] == role
        task["is_overdue"] = _is_overdue(task)
    return tasks


def _list_tasks(
    assignee: str | None,
    status: str | None,
    due_date_after: str | None,
    due_date_before: str | None,
    sort: str,
    role: str | None,
) -> list[dict[str, Any]]:
    tasks = _visible_tasks(role)
    if assignee:
        tasks = [task for task in tasks if task["assigned_to"] == assignee]
    if status:
        tasks = [task for task in tasks if task["status"] == _normalize_status(status)]
    if due_date_after:
        tasks = [task for task in tasks if task["due_date"] >= due_date_after]
    if due_date_before:
        tasks = [task for task in tasks if task["due_date"] <= due_date_before]

    reverse = sort.startswith("-")
    key = sort[1:] if reverse else sort
    if key == "assignee":
        tasks.sort(key=lambda task: task["assigned_to"].lower(), reverse=reverse)
    elif key == "status":
        tasks.sort(key=lambda task: task["status"], reverse=reverse)
    elif key == "due_date":
        tasks.sort(key=lambda task: task["due_date"] or "9999-12-31", reverse=reverse)
    else:
        tasks.sort(key=lambda task: task["updated_at"], reverse=not reverse)
    return tasks


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Funding Bot E2E fixture server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5010)
    args = parser.parse_args()
    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
