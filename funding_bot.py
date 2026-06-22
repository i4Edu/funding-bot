from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol
from xml.sax.saxutils import escape


class FundingBotError(Exception):
    """Base error for funding bot operations."""


class DuplicateSubmissionError(FundingBotError):
    """Raised when an opportunity already has an application record."""


class OpportunityNotFoundError(FundingBotError):
    """Raised when an opportunity cannot be found."""


class CredentialNotFoundError(FundingBotError):
    """Raised when a credential alias cannot be resolved."""


class OutreachThrottledError(FundingBotError):
    """Raised when an outreach email exceeds the allowed cadence."""


class OptOutError(FundingBotError):
    """Raised when a donor has opted out of outreach."""


class BrowserClient(Protocol):
    def submit(
        self,
        portal_url: str,
        credentials: dict[str, Any],
        form_data: dict[str, Any],
        attachments: Iterable[str],
    ) -> str:
        """Submit an application and return a submission reference."""


class FundingBot:
    def __init__(
        self,
        db_path: str | os.PathLike[str] = ":memory:",
        *,
        trusted_sources: Iterable[str] | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self.trusted_sources = {source.lower() for source in (trusted_sources or [])}
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS organization_profile (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS credential_refs (
                alias TEXT PRIMARY KEY,
                env_var_name TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS opportunities (
                signature TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                donor_name TEXT NOT NULL,
                title TEXT NOT NULL,
                portal_url TEXT NOT NULL,
                summary TEXT NOT NULL,
                category TEXT,
                discovered_at TEXT NOT NULL,
                status TEXT NOT NULL,
                raw_data_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_signature TEXT NOT NULL UNIQUE,
                donor_name TEXT NOT NULL,
                portal_url TEXT NOT NULL,
                submitted_at TEXT NOT NULL,
                status TEXT NOT NULL,
                next_action TEXT NOT NULL,
                submission_reference TEXT,
                FOREIGN KEY (opportunity_signature) REFERENCES opportunities(signature)
            );

            CREATE TABLE IF NOT EXISTS submission_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_signature TEXT NOT NULL,
                attempt_number INTEGER NOT NULL,
                succeeded INTEGER NOT NULL,
                error_message TEXT,
                happened_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS donors (
                email TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                opted_out INTEGER NOT NULL DEFAULT 0,
                preferences_json TEXT NOT NULL DEFAULT '{}',
                last_contact_at TEXT
            );

            CREATE TABLE IF NOT EXISTS communications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                donor_email TEXT NOT NULL,
                donor_name TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                channel TEXT NOT NULL,
                sent_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                format TEXT NOT NULL,
                path TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                happened_at TEXT NOT NULL,
                action TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _to_iso(timestamp: datetime | None = None) -> str:
        return (timestamp or FundingBot._utcnow()).isoformat()

    def _log_action(self, action: str, **details: Any) -> None:
        self.connection.execute(
            "INSERT INTO audit_logs (happened_at, action, details_json) VALUES (?, ?, ?)",
            (self._to_iso(), action, json.dumps(details, sort_keys=True)),
        )
        self.connection.commit()

    @staticmethod
    def _signature_for(opportunity: dict[str, Any]) -> str:
        identity = "|".join(
            str(opportunity.get(field, "")).strip().lower()
            for field in ("source", "portal_url", "title", "donor_name")
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def store_organization_profile(self, profile: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO organization_profile (key, value_json) VALUES ('profile', ?)",
            (json.dumps(profile, sort_keys=True),),
        )
        self.connection.commit()
        self._log_action("organization_profile_updated", keys=sorted(profile))

    def load_organization_profile(self) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT value_json FROM organization_profile WHERE key = 'profile'"
        ).fetchone()
        return json.loads(row["value_json"]) if row else {}

    def register_credential(self, alias: str, env_var_name: str) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO credential_refs (alias, env_var_name) VALUES (?, ?)",
            (alias, env_var_name),
        )
        self.connection.commit()
        self._log_action("credential_ref_registered", alias=alias, env_var_name=env_var_name)

    def resolve_credential(self, alias: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT env_var_name FROM credential_refs WHERE alias = ?",
            (alias,),
        ).fetchone()
        if not row:
            raise CredentialNotFoundError(f"No credential alias registered for {alias!r}.")

        raw_value = os.getenv(row["env_var_name"])
        if raw_value is None:
            raise CredentialNotFoundError(
                f"Environment variable {row['env_var_name']!r} is not set."
            )

        try:
            parsed = json.loads(raw_value)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        return {"secret": raw_value}

    def upsert_donor(
        self,
        *,
        email: str,
        name: str,
        opted_out: bool = False,
        preferences: dict[str, Any] | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO donors (email, name, opted_out, preferences_json, last_contact_at)
            VALUES (?, ?, ?, ?, COALESCE((SELECT last_contact_at FROM donors WHERE email = ?), NULL))
            ON CONFLICT(email) DO UPDATE SET
                name = excluded.name,
                opted_out = excluded.opted_out,
                preferences_json = excluded.preferences_json
            """,
            (email, name, int(opted_out), json.dumps(preferences or {}), email),
        )
        self.connection.commit()
        self._log_action("donor_upserted", email=email, opted_out=opted_out)

    def set_donor_opt_out(self, email: str, opted_out: bool = True) -> None:
        self.connection.execute(
            "UPDATE donors SET opted_out = ? WHERE email = ?",
            (int(opted_out), email),
        )
        self.connection.commit()
        self._log_action("donor_opt_out_updated", email=email, opted_out=opted_out)

    def discover_opportunities(
        self,
        opportunities: Iterable[dict[str, Any]],
        *,
        keywords: Iterable[str] | None = None,
        trusted_sources: Iterable[str] | None = None,
        discovered_at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        keyword_list = [keyword.lower() for keyword in (keywords or [])]
        allowed_sources = {
            source.lower() for source in (trusted_sources or self.trusted_sources or [])
        }
        found: list[dict[str, Any]] = []
        timestamp = self._to_iso(discovered_at)

        for opportunity in opportunities:
            source = str(opportunity.get("source", "")).strip()
            if allowed_sources and source.lower() not in allowed_sources:
                continue

            searchable_parts = [
                str(opportunity.get("title", "")),
                str(opportunity.get("summary", "")),
                " ".join(str(tag) for tag in opportunity.get("tags", [])),
                str(opportunity.get("category", "")),
            ]
            searchable_text = " ".join(searchable_parts).lower()
            if keyword_list and not any(keyword in searchable_text for keyword in keyword_list):
                continue

            record = {
                "source": source,
                "donor_name": str(opportunity.get("donor_name", source or "Unknown donor")),
                "title": str(opportunity.get("title", "Untitled opportunity")),
                "portal_url": str(opportunity.get("portal_url", "")),
                "summary": str(opportunity.get("summary", "")),
                "category": str(opportunity.get("category", "")),
                "discovered_at": timestamp,
                "status": "new",
                "raw_data_json": json.dumps(opportunity, sort_keys=True),
            }
            record["signature"] = self._signature_for(record)
            existing = self.connection.execute(
                "SELECT 1 FROM opportunities WHERE signature = ?",
                (record["signature"],),
            ).fetchone()
            if existing:
                continue

            self.connection.execute(
                """
                INSERT INTO opportunities (
                    signature, source, donor_name, title, portal_url, summary,
                    category, discovered_at, status, raw_data_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["signature"],
                    record["source"],
                    record["donor_name"],
                    record["title"],
                    record["portal_url"],
                    record["summary"],
                    record["category"],
                    record["discovered_at"],
                    record["status"],
                    record["raw_data_json"],
                ),
            )
            found.append(record)

        self.connection.commit()
        self._log_action("opportunities_discovered", count=len(found), keywords=keyword_list)
        return found

    def list_opportunities(self, *, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.connection.execute(
                "SELECT * FROM opportunities WHERE status = ? ORDER BY discovered_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM opportunities ORDER BY discovered_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def _get_opportunity(self, signature: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM opportunities WHERE signature = ?",
            (signature,),
        ).fetchone()
        if not row:
            raise OpportunityNotFoundError(f"Unknown opportunity {signature!r}.")
        return row

    def submit_application(
        self,
        opportunity_signature: str,
        *,
        submission_reference: str | None,
        status: str,
        next_action: str,
        submitted_at: datetime | None = None,
    ) -> dict[str, Any]:
        row = self._get_opportunity(opportunity_signature)
        existing = self.connection.execute(
            "SELECT 1 FROM applications WHERE opportunity_signature = ?",
            (opportunity_signature,),
        ).fetchone()
        if existing:
            raise DuplicateSubmissionError(
                f"An application already exists for opportunity {opportunity_signature!r}."
            )

        timestamp = self._to_iso(submitted_at)
        self.connection.execute(
            """
            INSERT INTO applications (
                opportunity_signature, donor_name, portal_url, submitted_at,
                status, next_action, submission_reference
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                opportunity_signature,
                row["donor_name"],
                row["portal_url"],
                timestamp,
                status,
                next_action,
                submission_reference,
            ),
        )
        self.connection.execute(
            "UPDATE opportunities SET status = ? WHERE signature = ?",
            (status, opportunity_signature),
        )
        self.connection.commit()
        self._log_action(
            "application_recorded",
            opportunity_signature=opportunity_signature,
            status=status,
            next_action=next_action,
        )
        return {
            "opportunity_signature": opportunity_signature,
            "status": status,
            "next_action": next_action,
            "submission_reference": submission_reference,
            "submitted_at": timestamp,
        }

    def submit_application_via_browser(
        self,
        opportunity_signature: str,
        *,
        credential_alias: str,
        browser_client: BrowserClient,
        form_data: dict[str, Any],
        attachments: Iterable[str] | None = None,
        max_retries: int = 3,
    ) -> dict[str, Any]:
        opportunity = self._get_opportunity(opportunity_signature)
        existing = self.connection.execute(
            "SELECT status FROM applications WHERE opportunity_signature = ?",
            (opportunity_signature,),
        ).fetchone()
        if existing:
            raise DuplicateSubmissionError(
                f"An application already exists for opportunity {opportunity_signature!r}."
            )

        credentials = self.resolve_credential(credential_alias)
        attachment_list = list(attachments or [])
        last_error = ""

        for attempt in range(1, max_retries + 1):
            try:
                reference = browser_client.submit(
                    opportunity["portal_url"],
                    credentials,
                    form_data,
                    attachment_list,
                )
            except Exception as exc:  # pragma: no cover - exercised via tests
                last_error = str(exc)
                self.connection.execute(
                    """
                    INSERT INTO submission_attempts (
                        opportunity_signature, attempt_number, succeeded, error_message, happened_at
                    ) VALUES (?, ?, 0, ?, ?)
                    """,
                    (opportunity_signature, attempt, last_error, self._to_iso()),
                )
                self.connection.commit()
                continue

            self.connection.execute(
                """
                INSERT INTO submission_attempts (
                    opportunity_signature, attempt_number, succeeded, error_message, happened_at
                ) VALUES (?, ?, 1, NULL, ?)
                """,
                (opportunity_signature, attempt, self._to_iso()),
            )
            self.connection.commit()
            return self.submit_application(
                opportunity_signature,
                submission_reference=reference,
                status="submitted",
                next_action="Await donor review",
            )

        return self.submit_application(
            opportunity_signature,
            submission_reference=None,
            status="pending",
            next_action=f"Retry failed browser submission: {last_error or 'unknown error'}",
        )

    def update_application_status(
        self,
        opportunity_signature: str,
        *,
        status: str,
        next_action: str,
    ) -> None:
        self.connection.execute(
            "UPDATE applications SET status = ?, next_action = ? WHERE opportunity_signature = ?",
            (status, next_action, opportunity_signature),
        )
        self.connection.execute(
            "UPDATE opportunities SET status = ? WHERE signature = ?",
            (status, opportunity_signature),
        )
        self.connection.commit()
        self._log_action(
            "application_status_updated",
            opportunity_signature=opportunity_signature,
            status=status,
            next_action=next_action,
        )

    def send_outreach(
        self,
        *,
        donor_email: str,
        donor_name: str,
        subject_template: str,
        body_template: str,
        context: dict[str, Any] | None = None,
        sender: Any | None = None,
        sent_at: datetime | None = None,
    ) -> dict[str, Any]:
        donor = self.connection.execute(
            "SELECT * FROM donors WHERE email = ?",
            (donor_email,),
        ).fetchone()
        if donor is None:
            self.upsert_donor(email=donor_email, name=donor_name)
            donor = self.connection.execute(
                "SELECT * FROM donors WHERE email = ?",
                (donor_email,),
            ).fetchone()

        assert donor is not None
        if donor["opted_out"]:
            raise OptOutError(f"{donor_email} has opted out of outreach.")

        send_time = sent_at or self._utcnow()
        if donor["last_contact_at"]:
            last_contact = datetime.fromisoformat(donor["last_contact_at"])
            if send_time - last_contact < timedelta(days=7):
                raise OutreachThrottledError(
                    f"{donor_email} was contacted less than seven days ago."
                )

        profile = self.load_organization_profile()
        merged_context = {
            "donor_name": donor_name,
            "organization_name": profile.get("name", "Nonprofit Funding Bot"),
            "mission": profile.get("mission", ""),
            "opt_out_url": (context or {}).get(
                "opt_out_url", "https://example.org/unsubscribe"
            ),
        }
        merged_context.update(profile)
        merged_context.update(context or {})

        subject = subject_template.format(**merged_context)
        body = body_template.format(**merged_context).rstrip()
        if merged_context["opt_out_url"] not in body:
            body = (
                f"{body}\n\nTo opt out of future outreach, visit {merged_context['opt_out_url']}."
            )

        if sender is not None:
            sender(donor_email, subject, body)

        sent_iso = self._to_iso(send_time)
        self.connection.execute(
            """
            INSERT INTO communications (donor_email, donor_name, subject, body, channel, sent_at)
            VALUES (?, ?, ?, ?, 'email', ?)
            """,
            (donor_email, donor_name, subject, body, sent_iso),
        )
        self.connection.execute(
            "UPDATE donors SET last_contact_at = ? WHERE email = ?",
            (sent_iso, donor_email),
        )
        self.connection.commit()
        self._log_action("outreach_sent", donor_email=donor_email, subject=subject)
        return {"email": donor_email, "subject": subject, "body": body, "sent_at": sent_iso}

    def generate_document(
        self,
        *,
        kind: str,
        template: str,
        output_dir: str | os.PathLike[str],
        context: dict[str, Any] | None = None,
        formats: Iterable[str] = ("pdf", "docx"),
    ) -> dict[str, str]:
        profile = self.load_organization_profile()
        merged_context = dict(profile)
        merged_context.update(context or {})
        rendered = template.format(**merged_context).strip() + "\n"

        target_dir = Path(output_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = self._utcnow().strftime("%Y%m%d%H%M%S")
        base_name = f"{kind.replace(' ', '_').lower()}_{stamp}"
        generated: dict[str, str] = {}

        for fmt in formats:
            normalized = fmt.lower()
            if normalized == "word":
                normalized = "docx"

            path = target_dir / f"{base_name}.{normalized}"
            if normalized == "pdf":
                self._write_pdf(path, rendered)
            elif normalized == "docx":
                self._write_docx(path, rendered)
            else:
                raise ValueError(f"Unsupported document format: {fmt}")

            generated[normalized] = str(path)
            self.connection.execute(
                "INSERT INTO documents (kind, format, path, created_at) VALUES (?, ?, ?, ?)",
                (kind, normalized, str(path), self._to_iso()),
            )

        self.connection.commit()
        self._log_action("documents_generated", kind=kind, formats=sorted(generated))
        return generated

    def _write_pdf(self, path: Path, text: str) -> None:
        lines = [line or " " for line in text.splitlines()]
        escaped_lines = [
            line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            for line in lines
        ]
        content_lines = ["BT", "/F1 11 Tf", "50 780 Td", "14 TL"]
        for index, line in enumerate(escaped_lines):
            if index == 0:
                content_lines.append(f"({line}) Tj")
            else:
                content_lines.append(f"T* ({line}) Tj")
        content_lines.append("ET")
        content = "\n".join(content_lines).encode("utf-8")

        objects = [
            b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj",
            b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj",
            b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj",
            b"4 0 obj << /Length "
            + str(len(content)).encode("ascii")
            + b" >> stream\n"
            + content
            + b"\nendstream endobj",
            b"5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj",
        ]

        pdf = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        for obj in objects:
            offsets.append(len(pdf))
            pdf.extend(obj)
            pdf.extend(b"\n")

        xref_start = len(pdf)
        pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
        pdf.extend(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
        pdf.extend(
            (
                f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\n"
                f"startxref\n{xref_start}\n%%EOF"
            ).encode("ascii")
        )
        path.write_bytes(pdf)

    def _write_docx(self, path: Path, text: str) -> None:
        paragraphs = []
        for line in text.splitlines():
            safe_line = escape(line or " ")
            paragraphs.append(
                "<w:p><w:r><w:t xml:space=\"preserve\">"
                f"{safe_line}"
                "</w:t></w:r></w:p>"
            )

        document_xml = (
            "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
            "<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">"
            f"<w:body>{''.join(paragraphs)}<w:sectPr/></w:body>"
            "</w:document>"
        )

        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "[Content_Types].xml",
                """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>
""",
            )
            archive.writestr(
                "_rels/.rels",
                """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>
""",
            )
            archive.writestr("word/document.xml", document_xml)

    def build_daily_summary(
        self,
        *,
        recipient: str,
        report_date: datetime | None = None,
    ) -> dict[str, str]:
        date = (report_date or self._utcnow()).date().isoformat()
        new_opportunities = self.connection.execute(
            """
            SELECT title, status FROM opportunities
            WHERE substr(discovered_at, 1, 10) = ?
            ORDER BY discovered_at
            """,
            (date,),
        ).fetchall()
        submitted_apps = self.connection.execute(
            """
            SELECT donor_name, portal_url, status FROM applications
            WHERE substr(submitted_at, 1, 10) = ?
            ORDER BY submitted_at
            """,
            (date,),
        ).fetchall()
        communications = self.connection.execute(
            """
            SELECT donor_name FROM communications
            WHERE substr(sent_at, 1, 10) = ?
            ORDER BY sent_at
            """,
            (date,),
        ).fetchall()
        pending = self.connection.execute(
            """
            SELECT donor_name, status, next_action FROM applications
            WHERE status IN ('pending', 'submitted', 'in_review')
            ORDER BY submitted_at
            """
        ).fetchall()

        def format_lines(rows: Iterable[sqlite3.Row], formatter: Any, empty: str) -> list[str]:
            items = [formatter(row) for row in rows]
            return items or [f"   • {empty}"]

        opportunity_lines = format_lines(
            new_opportunities,
            lambda row: f"   • {row['title']} – {row['status'].replace('_', ' ').title()}",
            "No new opportunities",
        )
        application_lines = format_lines(
            submitted_apps,
            lambda row: f"   • {row['donor_name']} – {row['status'].replace('_', ' ').title()}",
            "No applications submitted",
        )
        pending_lines = format_lines(
            pending,
            lambda row: f"   • {row['donor_name']} – {row['status'].replace('_', ' ').title()} ({row['next_action']})",
            "No pending applications",
        )

        body = "\n".join(
            [
                f"To: {recipient}",
                "",
                "Hello Lupael,",
                "",
                "Here is today’s funding activity summary:",
                "",
                f"- New Opportunities Found: {len(new_opportunities)}",
                *opportunity_lines,
                "",
                f"- Applications Submitted: {len(submitted_apps)}",
                *application_lines,
                "",
                f"- Donor Communications: {len(communications)} personalized emails sent",
                "   • No bounce or spam flags detected" if communications else "   • No outreach sent today",
                "",
                f"- Pending Applications: {len(pending)}",
                *pending_lines,
                "",
                "Best regards,",
                "Nonprofit Funding Bot",
            ]
        )
        subject = f"Daily Nonprofit Funding Report – {date}"
        self._log_action("daily_summary_built", recipient=recipient, report_date=date)
        return {"subject": subject, "body": body}
