# Funding Bot API Reference

This reference covers the machine-consumable HTTP API exposed by `web/app.py`. HTML dashboard pages such as `/dashboard`, `/settings`, and `/translations` are intentionally excluded except where they share the same JSON backing routes.

## Base URL

```text
http://localhost:5000
```

## Authentication

Funding Bot accepts either:

1. **HTTP Basic Auth** on every request, or
2. a **Flask session cookie** established after a successful authenticated request.

Supported roles:

- `admin`
- `staff`
- `auditor`

Example:

```bash
curl -u admin:$ADMIN_PASSWORD http://localhost:5000/health
```

### Role rules

| Role | Typical access |
| --- | --- |
| `admin` | full read/write access |
| `staff` | operational reads plus task/comment/translation actions |
| `auditor` | read-only analytics/task/audit visibility |

## Global conventions

### Content types

- Request bodies: `application/json` unless noted otherwise
- CSV import: `text/csv`, raw request body, or multipart upload field named `file`
- Metrics: `text/plain; version=0.0.4`

### Timestamps

All timestamps are ISO-8601 UTC strings, for example:

```json
"2026-07-19T04:34:57.291556+00:00"
```

### Pagination

**No endpoint currently supports `page`, `limit`, or cursor pagination.**

Use filtering instead:

- `/tasks`, `/task-directory`, `/api/tasks/export`
- `/translations/reviews`

Special case:

- `/audit-log` always returns the latest **100** entries.

### Common error schema

```json
{
  "error": "Human-readable message"
}
```

### Common HTTP status codes

| Code | Meaning |
| --- | --- |
| `200` | success |
| `201` | created |
| `202` | accepted for async queue processing |
| `204` | deleted, no body |
| `400` | validation error, duplicate submission, invalid transition, invalid CSV, unsupported value |
| `401` | missing/invalid credentials |
| `403` | authenticated but forbidden for current role |
| `404` | resource not found |
| `500` | unhandled server error |
| `503` | degraded queue health on `/health/queue` |

## Shared JSON schemas

### `Opportunity`

```json
{
  "type": "object",
  "required": [
    "signature",
    "source",
    "donor_name",
    "title",
    "portal_url",
    "summary",
    "discovered_at",
    "status",
    "raw_data",
    "data_classification"
  ],
  "properties": {
    "signature": { "type": "string" },
    "source": { "type": "string" },
    "donor_name": { "type": "string" },
    "title": { "type": "string" },
    "portal_url": { "type": "string" },
    "summary": { "type": "string" },
    "category": { "type": ["string", "null"] },
    "discovered_at": { "type": "string", "format": "date-time" },
    "status": { "type": "string" },
    "raw_data": { "type": "object", "additionalProperties": true },
    "data_classification": { "enum": ["public", "internal", "confidential", "secret"] }
  }
}
```

### `Application`

```json
{
  "type": ["object", "null"],
  "properties": {
    "id": { "type": "integer" },
    "opportunity_signature": { "type": "string" },
    "donor_name": { "type": "string" },
    "portal_url": { "type": "string" },
    "submitted_at": { "type": "string", "format": "date-time" },
    "status": { "type": "string" },
    "next_action": { "type": "string" },
    "submission_reference": { "type": ["string", "null"] },
    "data_classification": { "enum": ["public", "internal", "confidential", "secret"] }
  }
}
```

### `SubmissionAttempt`

```json
{
  "type": "object",
  "required": ["attempt_number", "succeeded", "happened_at"],
  "properties": {
    "attempt_number": { "type": "integer" },
    "succeeded": { "type": "boolean" },
    "error_message": { "type": ["string", "null"] },
    "happened_at": { "type": "string", "format": "date-time" }
  }
}
```

### `Donor`

```json
{
  "type": "object",
  "required": [
    "email",
    "name",
    "opted_out",
    "preferences",
    "locale",
    "segment",
    "data_classification",
    "field_classifications"
  ],
  "properties": {
    "email": { "type": "string", "format": "email" },
    "name": { "type": "string" },
    "opted_out": { "type": "boolean" },
    "preferences": { "type": "object", "additionalProperties": true },
    "last_contact_at": { "type": ["string", "null"], "format": "date-time" },
    "locale": { "enum": ["en", "bn"] },
    "segment": { "enum": ["corporate", "institutional", "individual", "unknown"] },
    "data_classification": { "enum": ["public", "internal", "confidential", "secret"] },
    "field_classifications": {
      "type": "object",
      "additionalProperties": { "enum": ["public", "internal", "confidential", "secret"] }
    }
  }
}
```

### `Task`

```json
{
  "type": "object",
  "required": [
    "id",
    "title",
    "description",
    "assignee",
    "assigned_to",
    "status",
    "due_date",
    "source",
    "created_at",
    "updated_at",
    "is_overdue",
    "unread_comment_count",
    "data_classification"
  ],
  "properties": {
    "id": { "type": "integer" },
    "external_id": { "type": ["string", "null"] },
    "title": { "type": "string" },
    "description": { "type": "string" },
    "assignee": { "type": "string" },
    "assigned_to": { "type": "string" },
    "assignee_email": { "type": ["string", "null"], "format": "email" },
    "assignee_name": { "type": ["string", "null"] },
    "status": { "enum": ["todo", "in-progress", "done", "blocked"] },
    "due_date": { "type": ["string", "null"], "format": "date" },
    "source": { "type": "string" },
    "created_at": { "type": "string", "format": "date-time" },
    "updated_at": { "type": "string", "format": "date-time" },
    "is_overdue": { "type": "boolean" },
    "unread_comment_count": { "type": "integer" },
    "data_classification": { "enum": ["public", "internal", "confidential", "secret"] }
  }
}
```

### `TaskComment`

```json
{
  "type": "object",
  "required": ["id", "task_id", "author", "content", "created_at", "updated_at"],
  "properties": {
    "id": { "type": "integer" },
    "task_id": { "type": "integer" },
    "author": { "type": "string" },
    "content": { "type": "string" },
    "created_at": { "type": "string", "format": "date-time" },
    "updated_at": { "type": "string", "format": "date-time" },
    "data_classification": { "enum": ["public", "internal", "confidential", "secret"] }
  }
}
```

### `TranslationReview`

```json
{
  "type": "object",
  "required": [
    "id",
    "locale",
    "translation_key",
    "source_text",
    "translated_text",
    "status",
    "created_at",
    "locale_metadata"
  ],
  "properties": {
    "id": { "type": "integer" },
    "locale": { "enum": ["en", "bn", "ar", "ur"] },
    "translation_key": { "type": "string" },
    "source_text": { "type": "string" },
    "translated_text": { "type": "string" },
    "status": { "enum": ["pending", "approved", "rejected"] },
    "submitter_notes": { "type": ["string", "null"] },
    "submitted_by_role": { "type": ["string", "null"] },
    "created_at": { "type": "string", "format": "date-time" },
    "reviewed_at": { "type": ["string", "null"], "format": "date-time" },
    "reviewed_by_role": { "type": ["string", "null"] },
    "reviewer_notes": { "type": ["string", "null"] },
    "locale_metadata": {
      "type": "object",
      "properties": {
        "code": { "type": "string" },
        "display_name": { "type": "string" },
        "native_name": { "type": "string" },
        "direction": { "enum": ["ltr", "rtl"] },
        "is_rtl": { "type": "boolean" }
      }
    }
  }
}
```

### `QueueHealthSnapshot`

```json
{
  "type": "object",
  "required": [
    "status",
    "mode",
    "active_modes",
    "queue_enabled",
    "legacy_cron_enabled",
    "queue_name",
    "broker_reachable",
    "timeout_seconds",
    "active_tasks",
    "pending_tasks",
    "queue_depth",
    "worker_count",
    "workers"
  ],
  "properties": {
    "status": { "enum": ["ok", "disabled", "degraded"] },
    "mode": { "type": "string" },
    "active_modes": { "type": "array", "items": { "type": "string" } },
    "queue_enabled": { "type": "boolean" },
    "legacy_cron_enabled": { "type": "boolean" },
    "queue_name": { "type": "string" },
    "broker_reachable": { "type": "boolean" },
    "timeout_seconds": { "type": "number" },
    "active_tasks": { "type": "integer" },
    "pending_tasks": { "type": "integer" },
    "queue_depth": { "type": "integer" },
    "worker_count": { "type": "integer" },
    "workers": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "name": { "type": "string" },
          "status": { "type": "string" },
          "active_tasks": { "type": "integer" },
          "reserved_tasks": { "type": "integer" },
          "scheduled_tasks": { "type": "integer" }
        }
      }
    },
    "error": { "type": ["string", "null"] }
  }
}
```

## Endpoint index

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| `GET` | `/opportunities` | staff/admin/auditor | list discovered opportunities |
| `GET` | `/opportunities/{signature}` | staff/admin/auditor | opportunity + application + submission attempts |
| `POST` | `/opportunities/{signature}/submit` | admin | record application submission |
| `GET` | `/donors` | admin/auditor | list donors |
| `POST` | `/donors` | admin | create/update donor |
| `POST` | `/donors/{email}/opt-out` | admin | set donor opt-out |
| `GET` | `/analytics` | admin/auditor | outreach event counters |
| `GET` | `/audit-log` | admin/auditor | latest 100 audit entries |
| `POST` | `/settings/organization` | admin | update organization profile |
| `POST` | `/settings/search` | admin | update discovery defaults |
| `POST` | `/settings/credentials` | admin | register credential alias |
| `POST` | `/settings/discover` | admin | run or enqueue discovery |
| `POST` | `/settings/privacy-policy` | admin | generate privacy policy artifacts |
| `POST` | `/settings/test-outreach` | admin | compose/send donor outreach |
| `GET` | `/translations/locales` | staff/admin/auditor | list UI locales |
| `GET` | `/translations/reviews` | staff/admin/auditor | list translation reviews |
| `POST` | `/translations/reviews` | staff/admin | create translation review |
| `POST` | `/translations/reviews/{review_id}/decision` | staff/admin | approve/reject translation review |
| `GET` | `/tasks` | staff/admin/auditor | list tasks |
| `GET` | `/task-directory` | staff/admin/auditor | alias of `/tasks` |
| `POST` | `/tasks` | admin | create task |
| `POST` | `/task-directory` | admin | alias of `/tasks` |
| `PUT` | `/tasks/{task_id}` | admin | update task |
| `GET` | `/tasks/{task_id}` | staff/admin/auditor | fetch task |
| `GET` | `/task-directory/{task_id}` | staff/admin/auditor | alias of `/tasks/{task_id}` |
| `GET` | `/api/tasks/export` | admin/auditor | filtered task export envelope |
| `POST` | `/api/tasks/sync` | admin | upsert external tasks |
| `POST` | `/api/tasks/import` | admin | CSV task import |
| `POST` | `/tasks/{task_id}/assign` | admin | reassign task |
| `POST` | `/tasks/{task_id}/assignment` | admin | alias of `/tasks/{task_id}/assign` |
| `POST` | `/task-directory/{task_id}/assignment` | admin | alias of `/tasks/{task_id}/assign` |
| `GET` | `/tasks/{task_id}/comments` | staff/admin/auditor | list comments + unread count |
| `POST` | `/tasks/{task_id}/comments` | staff/admin | create comment |
| `PATCH` | `/tasks/{task_id}/comments/{comment_id}` | staff/admin | edit comment |
| `DELETE` | `/tasks/{task_id}/comments/{comment_id}` | staff/admin | delete comment |
| `POST` | `/tasks/{task_id}/comments/read` | staff/admin/auditor | mark comments read |
| `POST` | `/tasks/{task_id}/status` | staff/admin/auditor | transition task workflow state |
| `GET` | `/health` | none | app + queue summary |
| `GET` | `/health/queue` | none | queue snapshot, `503` when degraded |
| `GET` | `/metrics` | admin/auditor | Prometheus text metrics |
| `POST` | `/feedback` | staff/admin | partner feedback intake |

---

## Opportunities API

### `GET /opportunities`

Returns all discovered opportunities ordered by newest `discovered_at` first.

Query parameters: none.

Example:

```bash
curl -u staff:$STAFF_PASSWORD http://localhost:5000/opportunities
```

Example response:

```json
[
  {
    "signature": "56237e8c90e12da6203aafcb70e36e6cf91c7cd33f15e7952181a4282b62d857",
    "source": "GlobalGiving",
    "donor_name": "i4Edu Partners",
    "title": "Girls in STEM",
    "portal_url": "https://example.org/opportunities/girls-in-stem",
    "summary": "Support after-school robotics labs for girls.",
    "category": "Education",
    "discovered_at": "2026-07-19T04:34:57.282065+00:00",
    "status": "submitted",
    "data_classification": "public",
    "raw_data": {
      "tags": ["stem", "robotics", "girls"],
      "deadline": "2026-09-30"
    }
  }
]
```

### `GET /opportunities/{signature}`

Returns the opportunity plus the linked application record, if one exists, and any browser submission attempts.

Example:

```bash
curl -u staff:$STAFF_PASSWORD \
  http://localhost:5000/opportunities/$SIGNATURE
```

Response schema:

```json
{
  "type": "object",
  "required": ["opportunity", "application", "submission_attempts"],
  "properties": {
    "opportunity": { "$ref": "#/definitions/Opportunity" },
    "application": { "$ref": "#/definitions/Application" },
    "submission_attempts": {
      "type": "array",
      "items": { "$ref": "#/definitions/SubmissionAttempt" }
    }
  }
}
```

### `POST /opportunities/{signature}/submit`

Admin-only endpoint for recording an application submission against an opportunity.

Request body:

```json
{
  "status": "submitted",
  "next_action": "Await donor review",
  "submission_reference": "APP-2026-001"
}
```

Rules:

- `status` is required
- `next_action` is required
- `submission_reference` may be `null`
- duplicate submissions return `400`

Example:

```bash
curl -u admin:$ADMIN_PASSWORD \
  -X POST http://localhost:5000/opportunities/$SIGNATURE/submit \
  -H 'Content-Type: application/json' \
  -d '{"status":"submitted","next_action":"Await donor review","submission_reference":"APP-2026-001"}'
```

Success response (`201`):

```json
{
  "opportunity_signature": "56237e8c90e12da6203aafcb70e36e6cf91c7cd33f15e7952181a4282b62d857",
  "status": "submitted",
  "next_action": "Await donor review",
  "submission_reference": "APP-2026-001",
  "submitted_at": "2026-07-19T04:34:57.287688+00:00"
}
```

---

## Donors and outreach API

### `GET /donors`

Lists all donors ordered by name/email.

Filtering: none.

```bash
curl -u auditor:$AUDITOR_PASSWORD http://localhost:5000/donors
```

### `POST /donors`

Creates or updates a donor.

Request body:

```json
{
  "email": "donor@example.org",
  "name": "Ada Donor",
  "opted_out": false,
  "preferences": {
    "interests": ["stem"]
  },
  "locale": "bn",
  "data_classification": "secret",
  "field_classifications": {
    "preferences": "secret"
  }
}
```

Notes:

- `email` and `name` are required
- `opted_out` defaults to `false`
- `preferences` must be an object
- `locale` is validated against outreach locales: `en`, `bn`
- if omitted, `segment` remains `unknown`

Success response (`201`): `Donor`

### `POST /donors/{email}/opt-out`

Marks a donor as opted out and writes a consent withdrawal record.

```bash
curl -u admin:$ADMIN_PASSWORD \
  -X POST http://localhost:5000/donors/donor@example.org/opt-out
```

Success response: updated `Donor` object.

### `GET /analytics`

Returns outreach counters grouped by event type.

```bash
curl -u auditor:$AUDITOR_PASSWORD http://localhost:5000/analytics
```

Example response:

```json
{
  "stats": {
    "sent": 0,
    "opened": 0,
    "clicked": 0,
    "bounced": 0,
    "unsubscribed": 0
  }
}
```

### `POST /settings/test-outreach`

Composes and optionally sends donor outreach.

Request body:

```json
{
  "email": "donor@example.org",
  "name": "Ada Donor",
  "dry_run": true,
  "locale": "bn",
  "subject_template": "Thanks for supporting {organization_name}",
  "body_template": "Dear {donor_name}, thank you for supporting {organization_name}."
}
```

Behavior:

- `dry_run` defaults to `true`
- if `subject_template` and `body_template` are both omitted, the default catalog template is used
- if `dry_run` is `false`, SMTP credentials must be configured
- opt-out and 7-day throttling are enforced

Example:

```bash
curl -u admin:$ADMIN_PASSWORD \
  -X POST http://localhost:5000/settings/test-outreach \
  -H 'Content-Type: application/json' \
  -d '{"email":"donor@example.org","name":"Ada Donor","dry_run":true}'
```

Success response (`201`):

```json
{
  "email": "donor@example.org",
  "subject": "Thank you for supporting i4Edu",
  "body": "Dear Ada Donor, ...",
  "sent_at": "2026-07-19T04:34:57.000000+00:00",
  "locale": "bn",
  "dry_run": true
}
```

### `POST /feedback`

Captures partner/operator feedback in the audit log.

Request body:

```json
{
  "category": "feature_request",
  "message": "Please add grant deadline reminders.",
  "contact": "ops@example.org"
}
```

Allowed `category` values:

- `feature_request`
- `bug_report`
- `general`

Success response (`201`):

```json
{
  "status": "received",
  "category": "feature_request"
}
```

---

## Audit API

### `GET /audit-log`

Returns the latest 100 audit entries.

```bash
curl -u auditor:$AUDITOR_PASSWORD http://localhost:5000/audit-log
```

Example entry:

```json
{
  "id": 18,
  "happened_at": "2026-07-19T04:34:57.302126+00:00",
  "action": "translation_review_updated",
  "details": {
    "review_id": 1,
    "locale": "bn",
    "translation_key": "outreach.default.subject",
    "status": "approved",
    "reviewed_by_role": "staff"
  }
}
```

---

## Settings and admin API

### `POST /settings/organization`

Stores organization profile fields under the `profile` settings record.

Request body: arbitrary JSON object with at least one field.

Typical fields:

```json
{
  "name": "i4Edu",
  "mission": "Expand access to STEM education",
  "website": "https://example.org",
  "contact_email": "hello@example.org",
  "privacy_jurisdictions": ["EU", "US"],
  "data_classification": "internal",
  "field_classifications": {
    "mission": "internal",
    "contact_email": "confidential"
  }
}
```

Success response:

```json
{
  "organization_profile": {
    "name": "i4Edu",
    "mission": "Expand access to STEM education",
    "privacy_jurisdictions": ["EU", "US"]
  }
}
```

### `POST /settings/search`

Updates persisted discovery defaults.

Request body:

```json
{
  "keywords": ["education", "stem"],
  "trusted_sources": ["GlobalGiving", "Grants.gov"]
}
```

Both fields may also be comma-separated strings.

Response:

```json
{
  "search_settings": {
    "keywords": ["education", "stem"],
    "trusted_sources": ["GlobalGiving", "Grants.gov"]
  }
}
```

### `POST /settings/credentials`

Registers a credential alias without storing the secret value itself.

Request body:

```json
{
  "alias": "smtp",
  "env_var_name": "SMTP_PASSWORD"
}
```

Success response (`201`):

```json
{
  "credentials": [
    {
      "alias": "smtp",
      "env_var_name": "SMTP_PASSWORD"
    }
  ]
}
```

### `POST /settings/discover`

Runs opportunity discovery immediately or enqueues it, depending on queue configuration.

Request body:

```json
{
  "keywords": ["education"],
  "trusted_sources": ["GlobalGiving"]
}
```

Sync response (`200`):

```json
{
  "count": 1,
  "new_opportunities": [
    {
      "title": "Education Innovation Grant"
    }
  ],
  "mode": "cron",
  "legacy_cron_enabled": true
}
```

Async response (`202`):

```json
{
  "mode": "hybrid",
  "task_id": "job-123",
  "task_name": "funding_bot.discover_opportunities",
  "idempotency_key": "abc123",
  "legacy_cron_enabled": true
}
```

### `POST /settings/privacy-policy`

Generates privacy policy artifacts and records version history.

Request body:

```json
{
  "output_dir": "generated/privacy_policies",
  "jurisdictions": ["EU", "US"],
  "formats": ["html", "pdf"],
  "effective_date": "2026-07-19"
}
```

Rules:

- `output_dir` is required and must be non-empty
- valid `jurisdictions`: `US`, `EU`, `ASIA`
- valid `formats`: `html`, `pdf`

Success response (`201`):

```json
{
  "policies": [
    {
      "jurisdiction": "EU",
      "revision": 1,
      "version": "eu-v1",
      "html_path": "generated/privacy_policies/eu-v1.html",
      "pdf_path": "generated/privacy_policies/eu-v1.pdf"
    }
  ],
  "residency_status": {
    "data_residency": "EU",
    "storage_region": "EU",
    "compliant": true
  },
  "versions": []
}
```

---

## Translation API

### `GET /translations/locales`

Returns supported UI locales.

```bash
curl -u staff:$STAFF_PASSWORD http://localhost:5000/translations/locales
```

Example response:

```json
{
  "locales": [
    {
      "code": "en",
      "display_name": "English",
      "native_name": "English",
      "direction": "ltr",
      "is_rtl": false
    },
    {
      "code": "ar",
      "display_name": "Arabic",
      "native_name": "العربية",
      "direction": "rtl",
      "is_rtl": true
    }
  ]
}
```

### `GET /translations/reviews`

Lists translation reviews.

Filters:

- `status=pending|approved|rejected`
- `locale=en|bn|ar|ur`

```bash
curl -u admin:$ADMIN_PASSWORD \
  'http://localhost:5000/translations/reviews?status=approved&locale=bn'
```

Example response:

```json
{
  "count": 1,
  "reviews": [
    {
      "id": 1,
      "locale": "bn",
      "translation_key": "outreach.default.subject",
      "source_text": "Thank you for supporting {organization_name}",
      "translated_text": "{organization_name}কে সমর্থন করার জন্য ধন্যবাদ",
      "status": "approved",
      "submitter_notes": "Initial draft",
      "submitted_by_role": "admin",
      "created_at": "2026-07-19T04:34:57.299652+00:00",
      "reviewed_at": "2026-07-19T04:34:57.301311+00:00",
      "reviewed_by_role": "staff",
      "reviewer_notes": "Looks good",
      "locale_metadata": {
        "code": "bn",
        "display_name": "Bengali",
        "native_name": "বাংলা",
        "direction": "ltr",
        "is_rtl": false
      }
    }
  ]
}
```

### `POST /translations/reviews`

Creates a review request.

Request body:

```json
{
  "locale": "bn",
  "translation_key": "outreach.default.subject",
  "source_text": "Thank you for supporting {organization_name}",
  "translated_text": "{organization_name}কে সমর্থন করার জন্য ধন্যবাদ",
  "submitter_notes": "Initial Bengali draft"
}
```

Success response (`201`): `TranslationReview`

### `POST /translations/reviews/{review_id}/decision`

Approves or rejects a pending review.

Request body:

```json
{
  "status": "approved",
  "reviewer_notes": "Ready for launch."
}
```

Allowed `status` values here:

- `approved`
- `rejected`

Success response: updated `TranslationReview`

---

## Task and collaboration API

### Task filters and sorting

Supported by `/tasks`, `/task-directory`, and `/api/tasks/export`:

- `assignee` or `assigned_to`
- `assignee_email`
- `status`
- `due_date_before` or `due_before`
- `due_date_after` or `due_after`
- `source`
- `viewer_email` (computes `unread_comment_count`)
- `sort`
- `sort_by`
- `sort_order`

Supported `sort` values:

- `updated_at`, `-updated_at`
- `assignee`, `-assignee`
- `title`, `-title`
- `status`, `-status`
- `due_date`, `-due_date`
- `created_at`, `-created_at`

### `GET /tasks` and `GET /task-directory`

Returns a filtered task list.

Role scoping:

- `admin` and `auditor` can view all tasks
- `staff` can only view staff-lane tasks

Example:

```bash
curl -u admin:$ADMIN_PASSWORD \
  'http://localhost:5000/tasks?status=todo&due_date_before=2026-07-31&sort=due_date&viewer_email=staff@example.org'
```

### `POST /tasks` and `POST /task-directory`

Creates a task.

Request body:

```json
{
  "title": "Prepare kickoff notes",
  "assigned_to": "staff",
  "description": "Outline collaboration steps",
  "status": "todo",
  "due_date": "2026-07-20",
  "external_id": "import-42",
  "source": "manual",
  "assignee_email": "staff@example.org",
  "assignee_name": "Staff User"
}
```

Notes:

- `title`, `assigned_to`/`assignee`, and `due_date` are required
- accepted status aliases include `pending -> todo`, `completed -> done`, `in_progress -> in-progress`

Success response (`201`):

```json
{
  "task": {
    "id": 1,
    "title": "Prepare kickoff notes",
    "assigned_to": "staff",
    "status": "todo",
    "due_date": "2026-07-20"
  },
  "notification": {
    "status": "sent"
  }
}
```

### `PUT /tasks/{task_id}`

Updates any subset of task fields.

Request body:

```json
{
  "title": "Prepare kickoff notes",
  "description": "Add owner list",
  "assignee": "auditor",
  "status": "blocked",
  "due_date": "2026-07-22"
}
```

Success response:

```json
{
  "task": { "id": 1 }
}
```

### `GET /tasks/{task_id}` and `GET /task-directory/{task_id}`

Returns one task.

Optional query parameter:

- `viewer_email` — includes unread count for that viewer

Example:

```bash
curl -u admin:$ADMIN_PASSWORD \
  'http://localhost:5000/tasks/1?viewer_email=staff@example.org'
```

Response:

```json
{
  "task": {
    "id": 1,
    "title": "Prepare kickoff notes",
    "assigned_to": "staff",
    "status": "todo",
    "unread_comment_count": 0
  }
}
```

### `GET /api/tasks/export`

Same filters as `/tasks`, but wrapped in an export envelope.

```bash
curl -u auditor:$AUDITOR_PASSWORD \
  'http://localhost:5000/api/tasks/export?sort=due_date'
```

Response:

```json
{
  "count": 2,
  "tasks": [
    {
      "id": 1,
      "title": "Prepare kickoff notes",
      "assigned_to": "staff",
      "status": "todo"
    }
  ]
}
```

### `POST /api/tasks/sync`

Bulk upserts external tasks.

Request body:

```json
{
  "source": "external_sync",
  "tasks": [
    {
      "external_id": "sync-1",
      "title": "Import prior backlog",
      "assigned_to": "staff",
      "status": "todo",
      "due_date": "2026-07-09"
    }
  ]
}
```

Success response:

```json
{
  "count": 1,
  "tasks": [
    {
      "external_id": "sync-1",
      "status": "todo"
    }
  ]
}
```

### `POST /api/tasks/import`

Imports CSV tasks.

Accepted input styles:

1. raw `text/csv` request body
2. multipart form upload field `file`

Accepted columns:

- `external_id`
- `title`
- `description`
- `assigned_to`
- `status`
- `due_date`
- `source`

Example:

```bash
curl -u admin:$ADMIN_PASSWORD \
  -X POST 'http://localhost:5000/api/tasks/import?source=csv_import' \
  -H 'Content-Type: text/csv' \
  --data-binary $'external_id,title,description,assigned_to,status,due_date,source\ncsv-1,Import kickoff checklist,Legacy onboarding,staff,todo,2026-07-10,csv_seed'
```

Success response (`201`):

```json
{
  "count": 1,
  "tasks": [
    {
      "external_id": "csv-1",
      "title": "Import kickoff checklist"
    }
  ]
}
```

### `POST /tasks/{task_id}/assign`
### `POST /tasks/{task_id}/assignment`
### `POST /task-directory/{task_id}/assignment`

Reassigns a task.

Request body:

```json
{
  "assigned_to": "auditor",
  "assignee_email": "auditor@example.org",
  "assignee_name": "Audit Lane"
}
```

Response:

```json
{
  "task": { "id": 1, "assigned_to": "auditor" },
  "notification": {
    "status": "sent",
    "recipient_email": "auditor@example.org",
    "sent_at": "2026-07-19T04:34:57.000000+00:00"
  }
}
```

Notification statuses:

- `sent`
- `rate_limited`
- `skipped` (`no_sender` or `no_assignee_email`)

### `GET /tasks/{task_id}/comments`

Returns the task, comments, and unread count.

Optional query parameter:

- `viewer_email`

Example:

```bash
curl -u staff:$STAFF_PASSWORD \
  'http://localhost:5000/tasks/1/comments?viewer_email=staff@example.org'
```

Example response:

```json
{
  "task": {
    "id": 1,
    "title": "Prepare kickoff notes"
  },
  "comments": [
    {
      "id": 1,
      "task_id": 1,
      "author": "admin@example.org",
      "content": "Please add a timeline.",
      "created_at": "2026-07-19T04:34:57.297104+00:00",
      "updated_at": "2026-07-19T04:34:57.297104+00:00"
    }
  ],
  "unread_count": 0
}
```

### `POST /tasks/{task_id}/comments`

Creates a comment.

Request body:

```json
{
  "author": "admin@example.org",
  "content": "Please add a timeline."
}
```

Success response (`201`): `TaskComment`

### `PATCH /tasks/{task_id}/comments/{comment_id}`

Request body:

```json
{
  "content": "Please add a timeline and budget."
}
```

Success response: updated `TaskComment`

### `DELETE /tasks/{task_id}/comments/{comment_id}`

Deletes the comment.

Success response: `204 No Content`

### `POST /tasks/{task_id}/comments/read`

Marks comments as read for a viewer.

Request body:

```json
{
  "reader_email": "staff@example.org"
}
```

Response:

```json
{
  "task_id": 1,
  "reader_email": "staff@example.org",
  "last_read_at": "2026-07-19T04:34:57.298789+00:00",
  "unread_count": 0
}
```

### `POST /tasks/{task_id}/status`

Transitions the workflow state.

Request body:

```json
{
  "status": "in-progress"
}
```

Valid transitions:

```text
todo        -> in-progress, blocked
in-progress -> todo, blocked, done
blocked     -> todo, in-progress
done        -> (none)
```

Example:

```bash
curl -u staff:$STAFF_PASSWORD \
  -X POST http://localhost:5000/tasks/1/status \
  -H 'Content-Type: application/json' \
  -d '{"status":"in-progress"}'
```

Success response:

```json
{
  "task": {
    "id": 1,
    "status": "in-progress"
  },
  "notification": "Task 'Prepare kickoff notes' moved from todo to in-progress."
}
```

---

## Health and metrics API

### `GET /health`

Returns app health plus queue status.

```bash
curl http://localhost:5000/health
```

Example response:

```json
{
  "status": "ok",
  "queue": {
    "status": "disabled",
    "mode": "cron",
    "active_modes": ["cron"],
    "queue_enabled": false,
    "legacy_cron_enabled": true,
    "queue_name": "funding-bot",
    "broker_reachable": false,
    "timeout_seconds": 2.0,
    "active_tasks": 0,
    "pending_tasks": 0,
    "queue_depth": 0,
    "worker_count": 0,
    "workers": [],
    "error": "Queue monitoring is disabled because ENABLE_TASK_QUEUE is not enabled."
  }
}
```

### `GET /health/queue`

Returns only the queue snapshot.

- `200` when `status` is `ok` or `disabled`
- `503` when `status` is `degraded`

### `GET /metrics`

Prometheus-style metrics endpoint.

```bash
curl -u admin:$ADMIN_PASSWORD http://localhost:5000/metrics
```

Representative metric families:

- `funding_bot_opportunities_total`
- `funding_bot_applications_total`
- `funding_bot_donors_total`
- `funding_bot_tasks_total`
- `funding_bot_tasks_status_total{status="..."}`
- `funding_bot_queue_health_status`
- `funding_bot_queue_depth`
- `funding_bot_connector_cache_hits_total{connector_id="..."}`

---

## Data model diagram (ERD style)

```text
+----------------------+        +----------------------+
| organization_profile |        | credential_refs      |
|----------------------|        |----------------------|
| key (PK)             |        | alias (PK)           |
| value_json           |        | env_var_name         |
| data_classification  |        | data_classification  |
| field_classifications|        +----------------------+
+----------+-----------+
           |
           | settings/profile data used by
           v
+----------------------+        +----------------------+
| opportunities        |1      0| applications         |
|----------------------|--------|----------------------|
| signature (PK)       |        | id (PK)              |
| source               |        | opportunity_signature|
| donor_name           |        | donor_name           |
| title                |        | portal_url           |
| portal_url           |        | submitted_at         |
| summary              |        | status               |
| category             |        | next_action          |
| discovered_at        |        | submission_reference |
| status               |        | data_classification  |
| raw_data_json        |        +----------+-----------+
| data_classification  |                   |
+----------+-----------+                   | 1
           | 1                             | 
           |                               | 0..n
           v                               v
+----------------------+        +----------------------+
| submission_attempts  |        | audit_logs           |
|----------------------|        |----------------------|
| id (PK)              |        | id (PK)              |
| opportunity_signature|        | happened_at          |
| attempt_number       |        | action               |
| succeeded            |        | details_json         |
| error_message        |        | data_classification  |
| happened_at          |        +----------------------+
| data_classification  |
+----------------------+

+----------------------+1      0..n+----------------------+
| donors               |----------| consent_records      |
|----------------------|          |----------------------|
| email (PK)           |          | id (PK)              |
| name                 |          | donor_email (FK)     |
| opted_out            |          | channel              |
| preferences_json     |          | status               |
| last_contact_at      |          | consented_at         |
| locale               |          | withdrawn_at         |
| segment              |          | source/proof/notes   |
| data_classification  |          | recorded_at          |
| field_classifications|          | data_classification  |
+----------+-----------+          +----------------------+
           |
           | 1
           | 0..n
           v
+----------------------+1      0..n+----------------------+
| communications       |----------| outreach_events      |
|----------------------|          |----------------------|
| id (PK)              |          | id (PK)              |
| donor_email          |          | communication_id(FK) |
| donor_name           |          | event_type           |
| subject/body         |          | happened_at          |
| channel              |          +----------------------+
| sent_at              |
| data_classification  |
+----------------------+

+----------------------+1      0..n+----------------------+
| tasks                |----------| task_comments        |
|----------------------|          |----------------------|
| id (PK)              |          | id (PK)              |
| external_id          |          | task_id (FK)         |
| title                |          | author               |
| description          |          | content              |
| assignee             |          | created_at/updated_at|
| assignee_email/name  |          | data_classification  |
| status               |          +----------------------+
| due_date             |
| source               |1      0..n+----------------------+
| created_at/updated_at|----------| task_notifications   |
| data_classification  |          |----------------------|
+----------+-----------+          | id (PK)              |
           | 1                    | task_id (FK)         |
           | 0..n                 | recipient_email      |
           v                      | notification_type    |
+----------------------+          | happened_at          |
| task_comment_reads   |          | data_classification  |
|----------------------|          +----------------------+
| task_id (PK/FK)      |
| reader_email (PK)    |
| last_read_at         |
| data_classification  |
+----------------------+

+----------------------+
| translation_reviews  |
|----------------------|
| id (PK)              |
| locale               |
| translation_key      |
| source_text          |
| translated_text      |
| status               |
| submitted/reviewed * |
| data_classification  |
+----------------------+

+----------------------+1      0..n+----------------------+
| task_runs            |----------| task_history         |
|----------------------|          |----------------------|
| task_id (PK)         |          | id (PK)              |
| idempotency_key      |          | task_id              |
| task_name            |          | task_name            |
| status/progress      |          | attempt_number       |
| payload/result/error |          | status               |
| retry/backoff fields |          | happened_at          |
| worker/completed_at  |          | result/error/details |
| dead_lettered        |          | data_classification  |
| data_classification  |          +----------------------+
+----------+-----------+
           |
           | 0..1
           v
+----------------------+
| dead_letter_queue    |
|----------------------|
| id (PK)              |
| task_id              |
| task_name            |
| payload_json         |
| error_message        |
| attempts             |
| failed_at            |
| data_classification  |
+----------------------+
```

## Match explainability

### What “match” means in the current codebase

The current release uses **deterministic filtering**, not a probabilistic ML model.

There are two practical match decisions:

1. **Opportunity-to-organization matching** during discovery
2. **Donor-to-outreach/template matching** during outreach preparation

### Opportunity matching flow

Implemented today in `FundingBot.run_discovery()` and `FundingBot.discover_opportunities()`:

```text
Incoming connector record
        |
        v
[Trusted source filter]
  pass if source is in trusted_sources
        |
        v
[Keyword evidence filter]
  pass if any configured keyword appears in:
  - title
  - summary
  - tags
  - category
        |
        v
[Normalization]
  source, donor_name, title, URL, summary, category
        |
        v
[Deduplication]
  stable signature derived from normalized record
        |
        v
[Persist as opportunity status="new"]
```

### Why an opportunity matched

A human-readable explanation can be derived from persisted fields even though no dedicated `/explain` endpoint exists yet.

Example explanation:

```json
{
  "matched": true,
  "source_check": {
    "matched": true,
    "configured_trusted_sources": ["GlobalGiving", "Grants.gov"],
    "actual_source": "GlobalGiving"
  },
  "keyword_check": {
    "matched": true,
    "configured_keywords": ["education", "stem"],
    "matched_keywords": ["stem"],
    "matched_fields": ["title", "tags", "summary"]
  },
  "dedupe_check": {
    "matched": true,
    "signature": "56237e8c...",
    "was_duplicate": false
  }
}
```

### Donor match explainability

The application does not currently expose a donor-match API, but the donor data model supports an explainability layer for outreach targeting.

Relevant signals already stored:

- `donors.segment`
- `donors.locale`
- `donors.preferences_json`
- `donors.opted_out`
- `donors.last_contact_at`
- `consent_records.status`
- `opportunities.category`
- `opportunities.raw_data.tags`

### Match confidence diagram

Use the following operator rubric when explaining why a donor should be contacted about a discovered opportunity.

```text
                    +----------------------+
                    | Compliance gates     |
                    | opted_out? consent?  |
                    | 7-day throttle?      |
                    +----------+-----------+
                               |
                      fail ----+----> NOT ELIGIBLE
                               |
                               v
                    +----------------------+
                    | Source confidence    |
                    | trusted source hit   |
                    +----------+-----------+
                               |
                               v
                    +----------------------+
                    | Opportunity evidence |
                    | keyword/category/tags|
                    +----------+-----------+
                               |
                               v
                    +----------------------+
                    | Donor fit            |
                    | segment/locale/prefs |
                    +----------+-----------+
                               |
                               v
                    +----------------------+
                    | Explainable outcome  |
                    | high / medium / low  |
                    +----------------------+
```

### Confidence bands

| Confidence | Interpretation | Typical evidence |
| --- | --- | --- |
| High | Strong operational fit | trusted source + multiple keyword hits + donor preferences or segment align + locale supported |
| Medium | Reasonable fit needing human review | trusted source + single keyword/category hit + limited donor preference data |
| Low | Weak fit | no explicit preferences, generic segment, or ambiguous keyword hit |
| Not eligible | must not contact | donor opted out, latest consent withdrawn, or contacted within 7 days |

### Example explainability payload

```json
{
  "donor": {
    "email": "donor@example.org",
    "segment": "individual",
    "locale": "bn",
    "opted_out": false
  },
  "opportunity": {
    "signature": "56237e8c...",
    "source": "GlobalGiving",
    "category": "Education"
  },
  "confidence": "high",
  "reasons": [
    "Opportunity source is in the trusted source list.",
    "Keyword 'stem' matched title, tags, and summary.",
    "Donor preference interests include 'stem'.",
    "Donor locale 'bn' is supported by outreach templates."
  ],
  "blocking_checks": {
    "opted_out": false,
    "consent_withdrawn": false,
    "throttled_last_7_days": false
  }
}
```

## WebSocket event schemas

**Current status:** the repository does **not** implement a WebSocket or SSE endpoint in `web/app.py` today. Clients should poll REST endpoints instead.

To keep future realtime integrations compatible with current REST payloads, use this canonical envelope if a WebSocket layer is added:

```json
{
  "event": "task.updated",
  "occurred_at": "2026-07-19T04:34:57.302126+00:00",
  "data": {}
}
```

Recommended event payloads:

### `opportunity.discovered`

```json
{
  "event": "opportunity.discovered",
  "occurred_at": "2026-07-19T04:34:57.282065+00:00",
  "data": {
    "opportunity": { "$ref": "Opportunity" }
  }
}
```

### `task.updated`

```json
{
  "event": "task.updated",
  "occurred_at": "2026-07-19T04:34:57.302126+00:00",
  "data": {
    "task": { "$ref": "Task" },
    "notification": "Task 'Prepare kickoff notes' moved from todo to in-progress."
  }
}
```

### `task.comment.created`

```json
{
  "event": "task.comment.created",
  "occurred_at": "2026-07-19T04:34:57.297104+00:00",
  "data": {
    "task_id": 1,
    "comment": { "$ref": "TaskComment" },
    "unread_count": 1
  }
}
```

### `translation_review.updated`

```json
{
  "event": "translation_review.updated",
  "occurred_at": "2026-07-19T04:34:57.301311+00:00",
  "data": {
    "review": { "$ref": "TranslationReview" }
  }
}
```

### `queue.health.changed`

```json
{
  "event": "queue.health.changed",
  "occurred_at": "2026-07-19T04:34:57.302126+00:00",
  "data": {
    "queue": { "$ref": "QueueHealthSnapshot" }
  }
}
```

## Troubleshooting

### `401 Authentication required`

- send Basic Auth credentials
- confirm the matching role password env var is set:
  - `ADMIN_PASSWORD`
  - `STAFF_PASSWORD`
  - `AUDITOR_PASSWORD`

### `403 Forbidden`

Common causes:

- `staff` user tried to access admin-only routes
- `staff` tried to query another lane, for example `?assignee=auditor`
- `auditor` tried to mutate data

### `400 Field 'tasks' must be a list of task objects.`

Send a JSON object with a top-level `tasks` array to `/api/tasks/sync`.

### `400 CSV row N: ...`

For `/api/tasks/import`:

- verify the header row exists
- only use supported columns
- use valid statuses: `todo`, `in-progress`, `done`, `blocked`
- use ISO dates like `2026-07-31`

### `400 Task status cannot transition ...`

Use the allowed workflow:

```text
todo -> in-progress -> done
```

or move through `blocked` first.

### `400 An application already exists for opportunity ...`

`POST /opportunities/{signature}/submit` is idempotent only in the sense that duplicates are rejected. Fetch the existing opportunity detail instead of re-submitting.

### `400 Invalid donor locale ...`

Outreach locales are currently:

- `en`
- `bn`

### `400 Unsupported locale ...`

Translation/UI locales are currently:

- `en`
- `bn`
- `ar`
- `ur`

### `404 Donor not found`

Create the donor first with `POST /donors`, or verify the email path segment is URL-encoded.

### `503 /health/queue`

The queue snapshot is degraded.

Check:

- `ENABLE_TASK_QUEUE`
- `CELERY_BROKER_URL`
- worker reachability
- broker timeout settings such as `CELERY_HEALTH_TIMEOUT_SECONDS`

### Outreach is rejected even though the donor exists

Possible causes:

- donor `opted_out = true`
- latest consent record is `withdrawn`
- donor was contacted in the last 7 days

### No realtime updates arrive

That is expected in the current release. Poll these endpoints instead:

- `/tasks`
- `/tasks/{task_id}/comments`
- `/translations/reviews`
- `/health/queue`
