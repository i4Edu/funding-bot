from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from flask import Flask, Response, g, jsonify, redirect, render_template, request, url_for

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from funding_bot import (  # noqa: E402
    DuplicateSubmissionError,
    FundingBot,
    FundingBotError,
    OpportunityNotFoundError,
    SMTPEmailSender,
    _validate_email,
)

app = Flask(__name__, template_folder=str(Path(__file__).resolve().parent / "templates"))
app.config["JSON_SORT_KEYS"] = False
MAX_FEEDBACK_MESSAGE_LENGTH = 2000

ROLE_PASSWORD_ENV_VARS = {
    "admin": "ADMIN_PASSWORD",
    "staff": "STAFF_PASSWORD",
    "auditor": "AUDITOR_PASSWORD",
}

# Track server start time for the uptime metric.
_APP_START_TIME = time.time()


def _bot() -> FundingBot:
    bot = g.get("_bot")
    if bot is None:
        bot = FundingBot(db_path=os.environ.get("BOT_DB_PATH", "funding_bot.db"))
        g._bot = bot
    return bot


def _json_error(message: str, status_code: int, *, headers: dict[str, str] | None = None) -> Response:
    response = jsonify({"error": message})
    response.status_code = status_code
    if headers:
        response.headers.update(headers)
    return response


def _auth_challenge(message: str = "Authentication required") -> Response:
    return _json_error(
        message,
        401,
        headers={"WWW-Authenticate": 'Basic realm="Funding Bot Dashboard"'},
    )


def _get_authenticated_role() -> str:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        raise PermissionError("Authentication required")

    token = header.split(" ", 1)[1].strip()
    try:
        decoded = base64.b64decode(token, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise PermissionError("Invalid authentication credentials") from exc

    username, separator, password = decoded.partition(":")
    if not separator:
        raise PermissionError("Invalid authentication credentials")

    role = username.strip().lower()
    if role not in ROLE_PASSWORD_ENV_VARS:
        raise PermissionError("Invalid authentication credentials")

    expected_password = os.environ.get(ROLE_PASSWORD_ENV_VARS[role])
    if not expected_password or not hmac.compare_digest(password, expected_password):
        raise PermissionError("Invalid authentication credentials")

    return role


def require_role(*roles: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    allowed_roles = {role.lower() for role in roles}

    def decorator(view_func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view_func)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            try:
                role = _get_authenticated_role()
            except PermissionError:
                return _auth_challenge()

            if role not in allowed_roles:
                return _json_error("Forbidden", 403)

            g.current_role = role
            return view_func(*args, **kwargs)

        return wrapped

    return decorator


def _parse_json_column(value: str | None) -> Any:
    if not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _coerce_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise ValueError(f"Field '{field_name}' must be a boolean.")


def _serialize_opportunity(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["raw_data"] = _parse_json_column(data.pop("raw_data_json", "{}"))
    return data


def _serialize_donor(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["opted_out"] = bool(data["opted_out"])
    data["preferences"] = _parse_json_column(data.pop("preferences_json", "{}"))
    return data


def _serialize_audit_log(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["details"] = _parse_json_column(data.pop("details_json", "{}"))
    return data


def _serialize_application(row: Any) -> dict[str, Any]:
    return dict(row)


def _serialize_submission_attempt(row: Any) -> dict[str, Any]:
    data = dict(row)
    data["succeeded"] = bool(data["succeeded"])
    return data


def _fetch_opportunity(signature: str) -> dict[str, Any]:
    row = _bot().connection.execute(
        "SELECT * FROM opportunities WHERE signature = ?",
        (signature,),
    ).fetchone()
    if not row:
        raise OpportunityNotFoundError(f"Unknown opportunity {signature!r}.")
    return _serialize_opportunity(row)


def _fetch_donor(email: str) -> dict[str, Any] | None:
    row = _bot().connection.execute("SELECT * FROM donors WHERE email = ?", (email,)).fetchone()
    return _serialize_donor(row) if row else None


def _get_request_json() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object request body.")
    return payload


def _dashboard_context() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    recent_cutoff = (now - timedelta(days=7)).isoformat()

    new_opportunities_count = _bot().connection.execute(
        "SELECT COUNT(*) FROM opportunities WHERE discovered_at >= ?",
        (recent_cutoff,),
    ).fetchone()[0]
    applications_submitted_count = _bot().connection.execute(
        "SELECT COUNT(*) FROM applications",
    ).fetchone()[0]
    pending_applications_count = _bot().connection.execute(
        "SELECT COUNT(*) FROM applications WHERE status IN ('pending', 'submitted', 'in_review')",
    ).fetchone()[0]
    donor_communications_count = _bot().connection.execute(
        "SELECT COUNT(*) FROM communications",
    ).fetchone()[0]

    recent_opportunities = [
        _serialize_opportunity(row)
        for row in _bot().connection.execute(
            "SELECT * FROM opportunities ORDER BY discovered_at DESC LIMIT 10"
        ).fetchall()
    ]
    recent_applications = [
        _serialize_application(row)
        for row in _bot().connection.execute(
            """
            SELECT
                applications.opportunity_signature,
                opportunities.title,
                applications.donor_name,
                applications.status,
                applications.next_action,
                applications.submission_reference,
                applications.submitted_at
            FROM applications
            JOIN opportunities
                ON opportunities.signature = applications.opportunity_signature
            ORDER BY applications.submitted_at DESC
            LIMIT 10
            """
        ).fetchall()
    ]

    return {
        "current_role": getattr(g, "current_role", None),
        "new_opportunities_count": new_opportunities_count,
        "applications_submitted_count": applications_submitted_count,
        "pending_applications_count": pending_applications_count,
        "donor_communications_count": donor_communications_count,
        "recent_opportunities": recent_opportunities,
        "recent_applications": recent_applications,
    }


@app.errorhandler(400)
def handle_bad_request(_: Any) -> Response:
    return _json_error("Bad request", 400)


@app.errorhandler(401)
def handle_unauthorized(_: Any) -> Response:
    return _auth_challenge()


@app.errorhandler(403)
def handle_forbidden(_: Any) -> Response:
    return _json_error("Forbidden", 403)


@app.errorhandler(404)
def handle_not_found(_: Any) -> Response:
    return _json_error("Not found", 404)


@app.errorhandler(DuplicateSubmissionError)
def handle_duplicate_submission(exc: DuplicateSubmissionError) -> Response:
    return _json_error(str(exc), 400)


@app.errorhandler(OpportunityNotFoundError)
def handle_opportunity_not_found(exc: OpportunityNotFoundError) -> Response:
    return _json_error(str(exc), 404)


@app.errorhandler(FundingBotError)
def handle_funding_bot_error(exc: FundingBotError) -> Response:
    return _json_error(str(exc), 400)


@app.errorhandler(ValueError)
def handle_value_error(exc: ValueError) -> Response:
    return _json_error(str(exc), 400)


@app.errorhandler(Exception)
def handle_unexpected_error(exc: Exception) -> Response:
    # Do not expose internal details (stack traces, db paths, etc.) to clients.
    app.logger.exception("Unhandled exception: %s", exc)
    return _json_error("Internal server error", 500)


@app.teardown_appcontext
def close_bot(_: Any) -> None:
    bot = g.pop("_bot", None)
    if bot is not None:
        bot.close()


@app.get("/")
def index() -> Response:
    return redirect(url_for("dashboard"))


@app.get("/dashboard")
@require_role("staff", "admin", "auditor")
def dashboard() -> str:
    return render_template("dashboard.html", **_dashboard_context())


@app.get("/opportunities")
@require_role("staff", "admin", "auditor")
def list_opportunities() -> Response:
    opportunities = [_serialize_opportunity(row) for row in _bot().connection.execute(
        "SELECT * FROM opportunities ORDER BY discovered_at DESC"
    ).fetchall()]
    return jsonify(opportunities)


@app.get("/opportunities/<signature>")
@require_role("staff", "admin", "auditor")
def get_opportunity(signature: str) -> Response:
    opportunity = _fetch_opportunity(signature)
    application_row = _bot().connection.execute(
        "SELECT * FROM applications WHERE opportunity_signature = ?",
        (signature,),
    ).fetchone()
    attempts = [
        _serialize_submission_attempt(row)
        for row in _bot().connection.execute(
            """
            SELECT attempt_number, succeeded, error_message, happened_at
            FROM submission_attempts
            WHERE opportunity_signature = ?
            ORDER BY attempt_number ASC
            """,
            (signature,),
        ).fetchall()
    ]
    response = {
        "opportunity": opportunity,
        "application": _serialize_application(application_row) if application_row else None,
        "submission_attempts": attempts,
    }
    return jsonify(response)


@app.post("/opportunities/<signature>/submit")
@require_role("admin")
def submit_opportunity(signature: str) -> Response:
    payload = _get_request_json()
    status = str(payload.get("status", "")).strip()
    next_action = str(payload.get("next_action", "")).strip()
    submission_reference = payload.get("submission_reference")

    if not status:
        raise ValueError("Field 'status' is required.")
    if not next_action:
        raise ValueError("Field 'next_action' is required.")
    if submission_reference is not None and not isinstance(submission_reference, str):
        raise ValueError("Field 'submission_reference' must be a string or null.")

    # CSRF protection is intentionally not implemented to keep this Flask app
    # limited to Flask + stdlib. Use flask-wtf or equivalent in production.
    result = _bot().submit_application(
        signature,
        submission_reference=submission_reference,
        status=status,
        next_action=next_action,
    )
    return jsonify(result), 201


@app.get("/donors")
@require_role("admin", "auditor")
def list_donors() -> Response:
    donors = [_serialize_donor(row) for row in _bot().connection.execute(
        "SELECT * FROM donors ORDER BY name COLLATE NOCASE ASC, email ASC"
    ).fetchall()]
    return jsonify(donors)


@app.post("/donors")
@require_role("admin")
def upsert_donor() -> Response:
    payload = _get_request_json()
    email = str(payload.get("email", "")).strip()
    name = str(payload.get("name", "")).strip()
    opted_out = _coerce_bool(payload.get("opted_out", False), "opted_out")
    preferences = payload.get("preferences", {})

    if not email:
        raise ValueError("Field 'email' is required.")
    if not name:
        raise ValueError("Field 'name' is required.")
    # Validate email format before passing to the bot layer.
    email = _validate_email(email)
    if preferences is None:
        preferences = {}
    if not isinstance(preferences, dict):
        raise ValueError("Field 'preferences' must be an object.")

    _bot().upsert_donor(
        email=email,
        name=name,
        opted_out=opted_out,
        preferences=preferences,
    )
    donor = _fetch_donor(email)
    return jsonify(donor), 201


@app.post("/donors/<path:email>/opt-out")
@require_role("admin")
def opt_out_donor(email: str) -> Response:
    donor = _fetch_donor(email)
    if donor is None:
        return _json_error("Donor not found", 404)

    _bot().set_donor_opt_out(email, opted_out=True)
    updated_donor = _fetch_donor(email)
    return jsonify(updated_donor)


@app.get("/analytics")
@require_role("admin", "auditor")
def get_analytics() -> Response:
    stats = _bot().get_outreach_analytics()
    return jsonify({"stats": stats})


@app.get("/audit-log")
@require_role("admin", "auditor")
def audit_log() -> Response:
    logs = [_serialize_audit_log(row) for row in _bot().connection.execute(
        """
        SELECT id, happened_at, action, details_json
        FROM audit_logs
        ORDER BY happened_at DESC, id DESC
        LIMIT 100
        """
    ).fetchall()]
    return jsonify(logs)


@app.get("/settings")
@require_role("staff", "admin", "auditor")
def settings_page() -> str:
    bot = _bot()
    smtp_configured = SMTPEmailSender.is_configured()
    context = {
        "current_role": getattr(g, "current_role", None),
        "organization_profile": bot.load_organization_profile(),
        "search_settings": bot.load_search_settings(),
        "credentials": bot.list_credentials(),
        "smtp_configured": smtp_configured,
        "smtp_host": os.environ.get("SMTP_HOST", ""),
    }
    return render_template("settings.html", **context)


@app.post("/settings/organization")
@require_role("admin")
def update_organization_settings() -> Response:
    payload = _get_request_json()
    if not payload:
        raise ValueError(
            "Request body must contain at least one profile field, e.g. "
            "'name', 'mission', or 'registration_number'."
        )
    _bot().store_organization_profile(payload)
    return jsonify({"organization_profile": _bot().load_organization_profile()})


@app.post("/settings/search")
@require_role("admin")
def update_search_settings() -> Response:
    payload = _get_request_json()
    keywords = payload.get("keywords", [])
    trusted_sources = payload.get("trusted_sources", [])
    if isinstance(keywords, str):
        keywords = [item.strip() for item in keywords.split(",") if item.strip()]
    if isinstance(trusted_sources, str):
        trusted_sources = [item.strip() for item in trusted_sources.split(",") if item.strip()]
    if not isinstance(keywords, list) or not isinstance(trusted_sources, list):
        raise ValueError(
            "Fields 'keywords' and 'trusted_sources' must be lists (e.g. "
            "[\"education\", \"csr\"]) or comma-separated strings (e.g. \"education,csr\")."
        )

    settings = _bot().store_search_settings(keywords=keywords, trusted_sources=trusted_sources)
    return jsonify({"search_settings": settings})


@app.post("/settings/credentials")
@require_role("admin")
def register_credential_route() -> Response:
    payload = _get_request_json()
    alias = str(payload.get("alias", "")).strip()
    env_var_name = str(payload.get("env_var_name", "")).strip()
    if not alias:
        raise ValueError("Field 'alias' is required.")
    if not env_var_name:
        raise ValueError("Field 'env_var_name' is required.")

    _bot().register_credential(alias, env_var_name)
    return jsonify({"credentials": _bot().list_credentials()}), 201


@app.post("/settings/discover")
@require_role("admin")
def run_discovery_now() -> Response:
    """Trigger a live search across configured donation sources.

    Demonstrates the bot's donation-search capability directly from the
    admin panel: it queries every configured portal connector, filters by
    the saved keyword/source settings (or an ad-hoc override), and persists
    any newly discovered opportunities.
    """
    payload = request.get_json(silent=True) or {}
    keywords = payload.get("keywords")
    trusted_sources = payload.get("trusted_sources")
    if isinstance(keywords, str):
        keywords = [item.strip() for item in keywords.split(",") if item.strip()]
    if isinstance(trusted_sources, str):
        trusted_sources = [item.strip() for item in trusted_sources.split(",") if item.strip()]

    found = _bot().run_discovery(keywords=keywords, trusted_sources=trusted_sources)
    return jsonify({"new_opportunities": found, "count": len(found)})


@app.post("/settings/test-outreach")
@require_role("admin")
def send_test_outreach() -> Response:
    """Compose (and optionally send) a donor outreach email from the panel.

    This demonstrates the bot's ability to communicate with a donor without
    requiring CLI access: by default the email is only composed and logged
    (``dry_run``); set ``"dry_run": false`` to actually deliver it via the
    configured SMTP credentials.
    """
    payload = _get_request_json()
    email = str(payload.get("email", "")).strip()
    name = str(payload.get("name", "")).strip()
    dry_run = _coerce_bool(payload.get("dry_run", True), "dry_run")
    subject_template = payload.get(
        "subject_template", "Thank you for supporting {organization_name}"
    )
    body_template = payload.get(
        "body_template",
        "Dear {donor_name},\n\nThank you for your continued interest in {organization_name}.",
    )

    if not email:
        raise ValueError("Field 'email' is required.")
    if not name:
        raise ValueError("Field 'name' is required.")

    sender = None if dry_run else SMTPEmailSender.from_env()
    result = _bot().send_outreach(
        donor_email=email,
        donor_name=name,
        subject_template=subject_template,
        body_template=body_template,
        sender=sender,
    )
    result["dry_run"] = dry_run
    return jsonify(result), 201


@app.get("/health")
def health() -> Response:
    return jsonify({"status": "ok"})


@app.get("/metrics")
@require_role("admin", "auditor")
def metrics() -> Response:
    """Prometheus-compatible text metrics endpoint.

    Exposes basic operational counters so that a Prometheus scraper or
    Grafana agent can ingest them without an external library.
    """
    bot = _bot()
    conn = bot.connection

    opportunities_total = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
    applications_total = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
    pending_applications = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE status IN ('pending','submitted','in_review')"
    ).fetchone()[0]
    donors_total = conn.execute("SELECT COUNT(*) FROM donors").fetchone()[0]
    opted_out_donors = conn.execute("SELECT COUNT(*) FROM donors WHERE opted_out = 1").fetchone()[0]
    audit_log_total = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
    communications_total = conn.execute("SELECT COUNT(*) FROM communications").fetchone()[0]
    uptime_seconds = time.time() - _APP_START_TIME

    lines = [
        "# HELP funding_bot_opportunities_total Total funding opportunities discovered",
        "# TYPE funding_bot_opportunities_total counter",
        f"funding_bot_opportunities_total {opportunities_total}",
        "# HELP funding_bot_applications_total Total grant applications recorded",
        "# TYPE funding_bot_applications_total counter",
        f"funding_bot_applications_total {applications_total}",
        "# HELP funding_bot_pending_applications Applications awaiting a decision",
        "# TYPE funding_bot_pending_applications gauge",
        f"funding_bot_pending_applications {pending_applications}",
        "# HELP funding_bot_donors_total Total donor records",
        "# TYPE funding_bot_donors_total gauge",
        f"funding_bot_donors_total {donors_total}",
        "# HELP funding_bot_opted_out_donors Donors who have opted out of outreach",
        "# TYPE funding_bot_opted_out_donors gauge",
        f"funding_bot_opted_out_donors {opted_out_donors}",
        "# HELP funding_bot_audit_log_entries_total Total audit log entries",
        "# TYPE funding_bot_audit_log_entries_total counter",
        f"funding_bot_audit_log_entries_total {audit_log_total}",
        "# HELP funding_bot_communications_total Total outreach emails logged",
        "# TYPE funding_bot_communications_total counter",
        f"funding_bot_communications_total {communications_total}",
        "# HELP funding_bot_uptime_seconds Seconds since the web process started",
        "# TYPE funding_bot_uptime_seconds gauge",
        f"funding_bot_uptime_seconds {uptime_seconds:.3f}",
    ]
    return Response("\n".join(lines) + "\n", mimetype="text/plain; version=0.0.4")


@app.post("/feedback")
@require_role("staff", "admin")
def submit_feedback() -> Response:
    """Accept partner feature-request feedback.

    Expected JSON body::

        {
            "category": "feature_request" | "bug_report" | "general",
            "message":  "Free-text feedback from the partner.",
            "contact":  "optional-reply-to@example.org"
        }

    The entry is stored in the audit log under the ``partner_feedback``
    action so it can be reviewed during monthly compliance reports.
    """
    payload = _get_request_json()
    category = str(payload.get("category", "general")).strip().lower()
    message = str(payload.get("message", "")).strip()
    contact = str(payload.get("contact", "")).strip() or None

    allowed_categories = {"feature_request", "bug_report", "general"}
    if category not in allowed_categories:
        raise ValueError(
            f"Field 'category' must be one of {sorted(allowed_categories)}."
        )
    if not message:
        raise ValueError("Field 'message' is required.")
    if len(message) > MAX_FEEDBACK_MESSAGE_LENGTH:
        raise ValueError(
            f"Field 'message' must not exceed {MAX_FEEDBACK_MESSAGE_LENGTH} characters."
        )
    if contact:
        contact = _validate_email(contact)

    _bot()._log_action(
        "partner_feedback",
        category=category,
        message=message,
        contact=contact,
        submitted_by_role=getattr(g, "current_role", None),
    )
    return jsonify({"status": "received", "category": category}), 201


if __name__ == "__main__":
    app.run(
        host=os.environ.get("FLASK_HOST", "127.0.0.1"),
        port=int(os.environ.get("FLASK_PORT", "5000")),
    )
