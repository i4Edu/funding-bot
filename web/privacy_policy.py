from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "privacy_policies"
_ENVIRONMENT = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)

JURISDICTION_DETAILS: dict[str, dict[str, Any]] = {
    "US": {
        "title": "United States Privacy Notice",
        "regulations": ["Applicable U.S. state privacy laws, including CCPA/CPRA where required"],
        "rights": [
            "Request access to the personal information we maintain about you.",
            "Request deletion of eligible personal information, subject to legal exceptions.",
            "Request correction of inaccurate personal information.",
            "Opt out of certain disclosures or uses where required by state law.",
        ],
        "transfer_clause": "We use contractual and operational safeguards before transferring data outside the selected hosting region.",
    },
    "EU": {
        "title": "European Union Privacy Notice",
        "regulations": [
            "General Data Protection Regulation (GDPR)",
            "Applicable local member-state privacy rules",
        ],
        "rights": [
            "Access, rectify, erase, or restrict processing of your personal data.",
            "Object to processing based on legitimate interests and request data portability where applicable.",
            "Withdraw consent at any time when processing relies on consent.",
            "Lodge a complaint with your local supervisory authority.",
        ],
        "transfer_clause": "Where cross-border transfers are necessary, we rely on appropriate safeguards such as Standard Contractual Clauses or equivalent local mechanisms.",
    },
    "ASIA": {
        "title": "Asia-Pacific Privacy Notice",
        "regulations": [
            "Applicable Asia-Pacific privacy and data protection laws in the jurisdictions where we operate"
        ],
        "rights": [
            "Request access to or correction of your personal information.",
            "Withdraw consent or request deletion where local law grants that right.",
            "Ask how personal data is used, disclosed, and retained.",
            "Submit a complaint to the relevant regulator or dispute-resolution body where available.",
        ],
        "transfer_clause": "Cross-border transfers are assessed against local legal requirements, contractual controls, and hosting-region commitments before data is moved.",
    },
}


def _normalize_jurisdiction(value: str) -> str:
    normalized = value.strip().upper()
    if normalized not in JURISDICTION_DETAILS:
        raise ValueError(f"Unsupported privacy policy jurisdiction {value!r}.")
    return normalized


def _normalize_profile(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": profile.get("name") or "Your Organization",
        "mission": profile.get("mission") or "supporting mission-driven programs",
        "registration_number": profile.get("registration_number") or "Not provided",
        "website": profile.get("website") or "https://example.org",
        "contact_email": profile.get("contact_email")
        or profile.get("privacy_email")
        or "privacy@example.org",
        "privacy_email": profile.get("privacy_email")
        or profile.get("contact_email")
        or "privacy@example.org",
        "address": profile.get("address") or "Address not provided",
        "data_categories": profile.get("data_categories")
        or [
            "contact details",
            "donation history",
            "communication preferences",
            "application and operational records",
        ],
        "subprocessors": profile.get("subprocessors")
        or ["managed hosting providers", "email delivery services"],
        "retention_summary": profile.get("retention_summary")
        or "We retain data only as long as needed for nonprofit operations, legal obligations, and donor stewardship.",
    }


def _render_text_from_html(html: str) -> str:
    collapsed = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    collapsed = re.sub(r"</(p|li|h1|h2|h3|section|ul|ol)>", "\n", collapsed, flags=re.IGNORECASE)
    collapsed = re.sub(r"<[^>]+>", "", collapsed)
    collapsed = re.sub(r"\n{3,}", "\n\n", collapsed)
    return collapsed.strip() + "\n"


def generate_privacy_policy_content(
    *,
    organization_profile: dict[str, Any],
    jurisdiction: str,
    data_residency: str,
    version: str,
    effective_date: str,
) -> dict[str, str]:
    normalized_jurisdiction = _normalize_jurisdiction(jurisdiction)
    profile = _normalize_profile(organization_profile)
    context = {
        **profile,
        "jurisdiction": normalized_jurisdiction,
        "data_residency": data_residency,
        "version": version,
        "effective_date": effective_date,
        "details": JURISDICTION_DETAILS[normalized_jurisdiction],
    }
    html = _ENVIRONMENT.get_template("privacy_policy.html").render(**context)
    text = _ENVIRONMENT.get_template("privacy_policy.txt").render(**context)
    text = _render_text_from_html(text)
    return {"html": html, "text": text}
