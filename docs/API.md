# API Reference

This document covers the JSON and text endpoints implemented in `web/app.py`.

UI-only routes are listed in [HTML dashboard routes](#html-dashboard-routes) at the end.

## Base URL

Examples below assume a local server:

```text
http://127.0.0.1:5000
```

## Authentication and roles

The dashboard API uses HTTP Basic authentication backed by role-specific environment variables:

| Role | Username | Password env var |
| --- | --- | --- |
| Admin | `admin` | `ADMIN_PASSWORD` |
| Staff | `staff` | `STAFF_PASSWORD` |
| Auditor | `auditor` | `AUDITOR_PASSWORD` |

Example header:

```http
Authorization: Basic base64("<role>:<password>")
```

After a successful authenticated request, Flask also establishes a secure session cookie so browser users can navigate dashboard pages without resending the header on every request.

Session-backed browser requests that use that cookie must also include the CSRF token emitted in the `X-CSRF-Token` response header (and rendered in dashboard forms). JSON form submissions should echo the token in the `X-CSRF-Token` request header. Requests that continue to send an explicit Basic `Authorization` header are treated as machine/API requests and are not blocked by the CSRF middleware.

## Common request/response conventions

- Request bodies are JSON objects unless noted otherwise.
- Successful API responses are JSON except `GET /metrics`, which returns Prometheus text.
- Timestamps use ISO 8601 strings.
- Authenticated responses include rate-limit headers (`X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`). When a limit is exceeded, the API returns `429` plus `Retry-After` and a JSON recovery payload.
- Every response includes `Content-Security-Policy`, `X-Frame-Options`, and `X-Content-Type-Options`. HTTPS responses also include `Strict-Transport-Security`.
- Validation, permission, and domain errors use the shared format:

```json
{
  "error": "Human-readable message"
}
```

### Common status codes

| Status | Meaning |
| --- | --- |
| `200` | Success |
| `201` | Resource created |
| `202` | Accepted for async/background processing |
| `204` | Success with no response body |
| `400` | Validation or domain error |
| `401` | Missing/invalid authentication (`WWW-Authenticate` header included) |
| `403` | Authenticated but insufficient role or scope |
| `404` | Resource not found |
| `429` | Rate limit exceeded; retry after the time in `Retry-After` |
| `500` | Unexpected server error |
| `503` | Health/readiness dependency degraded (`GET /health`, `GET /ready`, `GET /health/queue`) |

## Browser security headers

The Flask middleware applies these defaults:

- `Content-Security-Policy`: `default-src 'self'; base-uri 'self'; form-action 'self'; object-src 'none'; frame-ancestors 'none'; frame-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'`
- `X-Frame-Options`: `DENY` by default (`WEB_X_FRAME_OPTIONS=SAMEORIGIN` is also supported)
- `X-Content-Type-Options`: `nosniff`
- `Strict-Transport-Security`: `max-age=63072000; includeSubDomains` on HTTPS responses

Override the CSP string with `WEB_CONTENT_SECURITY_POLICY` only when you need to permit additional trusted assets.

## CORS for `/api/*`

Cross-origin browser access is enabled only for exact origins listed in `WEB_API_CORS_ALLOWED_ORIGINS`.

- default allowlist: `http://localhost:3000`, `http://127.0.0.1:3000`, `https://localhost:3000`, `https://127.0.0.1:3000`
- allowed methods: `GET, POST, PUT, PATCH, DELETE, OPTIONS`
- allowed request headers: `Authorization, Content-Type, X-CSRF-Token, X-CSRFToken`
- exposed response headers: `Retry-After, WWW-Authenticate, X-CSRF-Token, X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset`
- preflight (`OPTIONS`) requests to `/api/*` return `204` for allowed origins and `403` for origins outside the allowlist

Example:

```bash
curl -i -X OPTIONS http://127.0.0.1:5000/api/tasks/export \
  -H 'Origin: https://dashboard.example.org' \
  -H 'Access-Control-Request-Method: GET'
```

## Rate limiting

The dashboard uses Flask-Limiter with configurable per-endpoint policies:

- **auth/UI routes** such as `/dashboard`, `/settings`, and `/translations`: `30 per minute` by default
- **general API routes** such as `/tasks`, `/donors`, and `/feedback`: `120 per minute` by default
- **export routes** such as `/api/tasks/export`: `10 per minute` by default

Override them with `WEB_AUTH_RATE_LIMIT`, `WEB_API_RATE_LIMIT`, `WEB_EXPORT_RATE_LIMIT`, and optionally `WEB_RATE_LIMIT_STORAGE_URI`.

### Example Python setup

```python
import requests
from requests.auth import HTTPBasicAuth

BASE_URL = "http://127.0.0.1:5000"
ADMIN_AUTH = HTTPBasicAuth("admin", "admin-secret")
STAFF_AUTH = HTTPBasicAuth("staff", "staff-secret")
AUDITOR_AUTH = HTTPBasicAuth("auditor", "auditor-secret")
```

## Endpoint index

| Method | Path | Roles | Purpose |
| --- | --- | --- | --- |
| GET | `/opportunities` | staff, admin, auditor | List discovered opportunities |
| GET | `/opportunities/{signature}` | staff, admin, auditor | Get one opportunity plus application/submission state |
| POST | `/opportunities/{signature}/submit` | admin | Record or update a submission result |
| GET | `/donors` | admin, auditor | List donor records |
| POST | `/donors` | admin | Create/update a donor |
| POST | `/donors/{email}/opt-out` | admin | Mark a donor as opted out |
| GET | `/analytics` | admin, auditor | Return outreach analytics |
| GET | `/audit-log` | admin, auditor | Return the latest audit entries |
| GET | `/translations/locales` | staff, admin, auditor | List supported UI locales |
| GET | `/translations/reviews` | staff, admin, auditor | List translation reviews |
| POST | `/translations/reviews` | staff, admin | Create a translation review |
| POST | `/translations/reviews/{review_id}/decision` | staff, admin | Approve/reject a translation review |
| POST | `/settings/organization` | admin | Save organization profile data |
| POST | `/settings/search` | admin | Save discovery keywords and trusted sources |
| POST | `/settings/credentials` | admin | Register a credential alias |
| POST | `/settings/discover` | admin | Run discovery now or enqueue it |
| POST | `/settings/privacy-policy` | admin | Generate privacy policy artifacts |
| POST | `/settings/test-outreach` | admin | Compose or send a test outreach email |
| GET | `/tasks` or `/task-directory` | staff, admin, auditor | List tasks |
| POST | `/tasks` or `/task-directory` | admin | Create a task |
| GET | `/tasks/{task_id}` or `/task-directory/{task_id}` | staff, admin, auditor | Get one task |
| GET | `/api/tasks/export` | admin, auditor | Export filtered tasks |
| GET | `/api/exports` | admin, auditor | Show export schedule metadata and recent warehouse exports |
| POST | `/api/exports` | admin, auditor | Generate or enqueue warehouse exports |
| POST | `/api/tasks/sync` | admin | Upsert a batch of task records |
| POST | `/api/tasks/import` | admin | Import tasks from CSV |
| POST | `/tasks/{task_id}/assign`, `/tasks/{task_id}/assignment`, `/task-directory/{task_id}/assignment` | admin | Reassign a task |
| GET | `/tasks/{task_id}/comments` | staff, admin, auditor | List comments for a task |
| POST | `/tasks/{task_id}/comments` | staff, admin | Add a task comment |
| PATCH | `/tasks/{task_id}/comments/{comment_id}` | staff, admin | Edit a task comment |
| DELETE | `/tasks/{task_id}/comments/{comment_id}` | staff, admin | Delete a task comment |
| POST | `/tasks/{task_id}/comments/read` | staff, admin, auditor | Mark comments as read |
| POST | `/tasks/{task_id}/status` | staff, admin, auditor | Move a task through the workflow |
| GET | `/health` | none | Basic service health / liveness |
| GET | `/ready` | none | Dependency-aware readiness |
| GET | `/health/queue` | none | Queue-specific health |
| GET | `/metrics` | admin, auditor | Prometheus metrics |
| POST | `/feedback` | staff, admin | Store partner feedback in the audit log |

## Opportunities

### GET `/opportunities`

- **Purpose:** list all discovered opportunities, newest first.
- **Auth:** Basic Auth required.
- **Roles:** `staff`, `admin`, `auditor`
- **Request body:** none

**Response schema (`200`)**

```json
[
  {
    "signature": "string",
    "source": "string",
    "donor_name": "string",
    "title": "string",
    "portal_url": "string",
    "summary": "string",
    "category": "string|null",
    "discovered_at": "ISO-8601 timestamp",
    "status": "string",
    "data_classification": "string",
    "raw_data": {}
  }
]
```

**Example response**

```json
[
  {
    "signature": "opp-123",
    "source": "grants.gov",
    "donor_name": "Example Foundation",
    "title": "Community STEM Grant",
    "portal_url": "https://example.org/grants/stem",
    "summary": "Supports after-school STEM programs.",
    "category": "education",
    "discovered_at": "2026-07-19T04:34:11.817441+00:00",
    "status": "pending",
    "data_classification": "public",
    "raw_data": {
      "deadline": "2026-08-15",
      "grant_amount": "25000"
    }
  }
]
```

**curl**

```bash
curl -u admin:admin-secret http://127.0.0.1:5000/opportunities
```

**Python**

```python
response = requests.get(f"{BASE_URL}/opportunities", auth=ADMIN_AUTH, timeout=30)
print(response.json())
```

## Warehouse exports

### GET `/api/exports`

- **Purpose:** return the effective Celery export schedule plus recent export/retention audit entries.
- **Auth:** Basic Auth required.
- **Roles:** `admin`, `auditor`

**Response schema (`200`)**

```json
{
  "schedule": {
    "hour": 1,
    "minute": 0,
    "datasets": ["donors", "tasks", "matches", "results"],
    "format": "json",
    "output_dir": "generated/exports",
    "archive": true
  },
  "exports": [],
  "count": 0
}
```

### POST `/api/exports`

- **Purpose:** create data-warehouse exports for BI/reporting.
- **Auth:** Basic Auth required.
- **Roles:** `admin`, `auditor`
- **Formats:** `json`, `csv`, `parquet`
- **Datasets:** `donors`, `tasks`, `matches` (opportunity/application matches), `results` (application outcomes)

**Request body**

```json
{
  "datasets": ["donors", "tasks", "matches", "results"],
  "format": "parquet",
  "output_dir": "generated/exports",
  "archive": true,
  "async": false
}
```

When `async` is `true` and the task queue is enabled, the endpoint returns `202` with a Celery task identifier. Otherwise it returns `201` with an export manifest.

**Synchronous response (`201`)**

```json
{
  "datasets": ["donors", "tasks"],
  "format": "json",
  "output_dir": "generated/exports",
  "archive": true,
  "count": 2,
  "artifacts": [
    {
      "dataset": "donors",
      "format": "json",
      "path": "generated/exports/donors_20260719T010000Z.json",
      "row_count": 12,
      "sha256": "..."
    }
  ]
}
```

### GET `/opportunities/{signature}`

- **Purpose:** fetch one opportunity plus its stored application and submission attempts.
- **Auth:** Basic Auth required.
- **Roles:** `staff`, `admin`, `auditor`

**Response schema (`200`)**

```json
{
  "opportunity": {},
  "application": {
    "id": 1,
    "opportunity_signature": "string",
    "donor_name": "string",
    "portal_url": "string",
    "submitted_at": "ISO-8601 timestamp",
    "status": "string",
    "next_action": "string",
    "submission_reference": "string|null",
    "data_classification": "string"
  },
  "submission_attempts": [
    {
      "attempt_number": 1,
      "succeeded": false,
      "error_message": "string|null",
      "happened_at": "ISO-8601 timestamp"
    }
  ]
}
```

**Example response**

```json
{
  "application": {
    "data_classification": "internal",
    "donor_name": "Example Foundation",
    "id": 1,
    "next_action": "Draft proposal narrative",
    "opportunity_signature": "opp-123",
    "portal_url": "https://example.org/grants/stem",
    "status": "pending",
    "submission_reference": null,
    "submitted_at": "2026-07-19T04:34:11.817441+00:00"
  },
  "opportunity": {
    "signature": "opp-123",
    "source": "grants.gov",
    "title": "Community STEM Grant",
    "status": "pending"
  },
  "submission_attempts": [
    {
      "attempt_number": 1,
      "error_message": "Temporary portal timeout",
      "happened_at": "2026-07-19T04:34:11.817441+00:00",
      "succeeded": false
    }
  ]
}
```

**curl**

```bash
curl -u staff:staff-secret http://127.0.0.1:5000/opportunities/opp-123
```

**Python**

```python
response = requests.get(f"{BASE_URL}/opportunities/opp-123", auth=STAFF_AUTH, timeout=30)
print(response.json()["application"])
```

### POST `/opportunities/{signature}/submit`

- **Purpose:** record an application submission result for an opportunity.
- **Auth:** Basic Auth required.
- **Roles:** `admin`

**Request body**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `status` | string | yes | Stored application status |
| `next_action` | string | yes | Human-readable follow-up |
| `submission_reference` | string or `null` | no | External receipt/reference |

**Example request**

```json
{
  "status": "submitted",
  "next_action": "Wait for review",
  "submission_reference": "SUB-001"
}
```

**Response schema (`201`)**

```json
{
  "opportunity_signature": "string",
  "status": "string",
  "next_action": "string",
  "submission_reference": "string|null",
  "submitted_at": "ISO-8601 timestamp"
}
```

**Example success response**

```json
{
  "opportunity_signature": "opp-456",
  "status": "submitted",
  "next_action": "Wait for review",
  "submission_reference": "SUB-001",
  "submitted_at": "2026-07-19T05:00:00+00:00"
}
```

**Example duplicate response (`400`)**

```json
{
  "error": "An application already exists for opportunity 'opp-123'."
}
```

**curl**

```bash
curl -u admin:admin-secret \
  -H 'Content-Type: application/json' \
  -d '{"status":"submitted","next_action":"Wait for review","submission_reference":"SUB-001"}' \
  http://127.0.0.1:5000/opportunities/opp-456/submit
```

**Python**

```python
payload = {
    "status": "submitted",
    "next_action": "Wait for review",
    "submission_reference": "SUB-001",
}
response = requests.post(
    f"{BASE_URL}/opportunities/opp-456/submit",
    auth=ADMIN_AUTH,
    json=payload,
    timeout=30,
)
print(response.json())
```

## Donors

### GET `/donors`

- **Purpose:** list donor records.
- **Auth:** Basic Auth required.
- **Roles:** `admin`, `auditor`

**Response schema (`200`)**

```json
[
  {
    "email": "string",
    "name": "string",
    "segment": "string",
    "locale": "string",
    "opted_out": false,
    "last_contact_at": "ISO-8601 timestamp|null",
    "preferences": {},
    "data_classification": "string",
    "field_classifications": {}
  }
]
```

**curl**

```bash
curl -u auditor:auditor-secret http://127.0.0.1:5000/donors
```

**Python**

```python
response = requests.get(f"{BASE_URL}/donors", auth=AUDITOR_AUTH, timeout=30)
print(response.json()[0]["email"])
```

### POST `/donors`

- **Purpose:** create or update a donor profile.
- **Auth:** Basic Auth required.
- **Roles:** `admin`

**Request body**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `email` | string | yes | Must be a valid email |
| `name` | string | yes | Donor display name |
| `opted_out` | boolean | no | Defaults to `false` |
| `preferences` | object | no | Defaults to `{}` |
| `locale` | string | no | Preferred locale |
| `data_classification` | string | no | Optional override |
| `field_classifications` | object | no | Optional per-field classification map |

**Example request**

```json
{
  "email": "newdonor@example.org",
  "name": "New Donor",
  "opted_out": false,
  "preferences": {
    "segment": "institutional"
  },
  "locale": "bn"
}
```

**Response schema (`201`)**: donor object as returned by `GET /donors`.

**curl**

```bash
curl -u admin:admin-secret \
  -H 'Content-Type: application/json' \
  -d '{"email":"newdonor@example.org","name":"New Donor","preferences":{"segment":"institutional"},"locale":"bn"}' \
  http://127.0.0.1:5000/donors
```

**Python**

```python
payload = {
    "email": "newdonor@example.org",
    "name": "New Donor",
    "preferences": {"segment": "institutional"},
    "locale": "bn",
}
response = requests.post(f"{BASE_URL}/donors", auth=ADMIN_AUTH, json=payload, timeout=30)
print(response.json())
```

### POST `/donors/{email}/opt-out`

- **Purpose:** mark a donor as opted out of outreach.
- **Auth:** Basic Auth required.
- **Roles:** `admin`
- **Request body:** none

**Response schema (`200`)**: donor object with `opted_out: true`

**Example response**

```json
{
  "email": "newdonor@example.org",
  "name": "New Donor",
  "opted_out": true,
  "locale": "bn"
}
```

**curl**

```bash
curl -u admin:admin-secret -X POST \
  http://127.0.0.1:5000/donors/newdonor@example.org/opt-out
```

**Python**

```python
response = requests.post(
    f"{BASE_URL}/donors/newdonor@example.org/opt-out",
    auth=ADMIN_AUTH,
    timeout=30,
)
print(response.json()["opted_out"])
```

## Analytics and audit

### GET `/analytics`

- **Purpose:** return outreach counters.
- **Auth:** Basic Auth required.
- **Roles:** `admin`, `auditor`

**Example response**

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

**curl**

```bash
curl -u auditor:auditor-secret http://127.0.0.1:5000/analytics
```

**Python**

```python
response = requests.get(f"{BASE_URL}/analytics", auth=AUDITOR_AUTH, timeout=30)
print(response.json()["stats"])
```

### GET `/audit-log`

- **Purpose:** return the latest 100 audit log entries.
- **Auth:** Basic Auth required.
- **Roles:** `admin`, `auditor`

**Response schema (`200`)**

```json
[
  {
    "id": 10,
    "happened_at": "ISO-8601 timestamp",
    "action": "string",
    "details": {}
  }
]
```

**curl**

```bash
curl -u admin:admin-secret http://127.0.0.1:5000/audit-log
```

**Python**

```python
response = requests.get(f"{BASE_URL}/audit-log", auth=ADMIN_AUTH, timeout=30)
print(response.json()[0]["action"])
```

## Translation review

### GET `/translations/locales`

- **Purpose:** list supported locales and RTL/LTR metadata.
- **Auth:** Basic Auth required.
- **Roles:** `staff`, `admin`, `auditor`

**Example response**

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

**curl**

```bash
curl -u staff:staff-secret http://127.0.0.1:5000/translations/locales
```

**Python**

```python
response = requests.get(f"{BASE_URL}/translations/locales", auth=STAFF_AUTH, timeout=30)
print(response.json()["locales"])
```

### GET `/translations/reviews`

- **Purpose:** list translation review records.
- **Auth:** Basic Auth required.
- **Roles:** `staff`, `admin`, `auditor`

**Query parameters**

| Name | Type | Description |
| --- | --- | --- |
| `status` | string | Optional filter: `pending`, `approved`, `rejected` |
| `locale` | string | Optional locale filter |

**Example response**

```json
{
  "count": 1,
  "reviews": [
    {
      "id": 1,
      "locale": "bn",
      "translation_key": "outreach.default.subject",
      "source_text": "Hello",
      "translated_text": "হ্যালো",
      "status": "approved",
      "submitter_notes": null,
      "submitted_by_role": "admin",
      "created_at": "2026-07-19T04:34:11.843522+00:00",
      "reviewed_at": "2026-07-19T04:34:11.848667+00:00",
      "reviewed_by_role": "admin",
      "reviewer_notes": "ok",
      "locale_metadata": {
        "code": "bn",
        "direction": "ltr",
        "is_rtl": false
      }
    }
  ]
}
```

**curl**

```bash
curl -u auditor:auditor-secret \
  "http://127.0.0.1:5000/translations/reviews?status=approved&locale=bn"
```

**Python**

```python
response = requests.get(
    f"{BASE_URL}/translations/reviews",
    auth=AUDITOR_AUTH,
    params={"status": "approved", "locale": "bn"},
    timeout=30,
)
print(response.json()["count"])
```

### POST `/translations/reviews`

- **Purpose:** submit a translation for review.
- **Auth:** Basic Auth required.
- **Roles:** `staff`, `admin`

**Request body**

| Field | Type | Required |
| --- | --- | --- |
| `locale` | string | yes |
| `translation_key` | string | yes |
| `source_text` | string | yes |
| `translated_text` | string | yes |
| `submitter_notes` | string or `null` | no |

**Example request**

```json
{
  "locale": "bn",
  "translation_key": "outreach.default.subject",
  "source_text": "Thank you for supporting {organization_name}",
  "translated_text": "{organization_name}কে সমর্থন করার জন্য ধন্যবাদ",
  "submitter_notes": "Initial Bengali draft"
}
```

**Response schema (`201`)**: translation review object.

**curl**

```bash
curl -u admin:admin-secret \
  -H 'Content-Type: application/json' \
  -d '{"locale":"bn","translation_key":"outreach.default.subject","source_text":"Hello","translated_text":"হ্যালো"}' \
  http://127.0.0.1:5000/translations/reviews
```

**Python**

```python
payload = {
    "locale": "bn",
    "translation_key": "outreach.default.subject",
    "source_text": "Hello",
    "translated_text": "হ্যালো",
}
response = requests.post(
    f"{BASE_URL}/translations/reviews",
    auth=ADMIN_AUTH,
    json=payload,
    timeout=30,
)
print(response.json()["status"])
```

### POST `/translations/reviews/{review_id}/decision`

- **Purpose:** approve or reject an existing review.
- **Auth:** Basic Auth required.
- **Roles:** `staff`, `admin`

**Request body**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `status` | string | yes | `approved` or `rejected` |
| `reviewer_notes` | string or `null` | no | Optional reviewer comment |

**Example request**

```json
{
  "status": "approved",
  "reviewer_notes": "Ready for launch."
}
```

**curl**

```bash
curl -u staff:staff-secret \
  -H 'Content-Type: application/json' \
  -d '{"status":"approved","reviewer_notes":"Ready for launch."}' \
  http://127.0.0.1:5000/translations/reviews/1/decision
```

**Python**

```python
response = requests.post(
    f"{BASE_URL}/translations/reviews/1/decision",
    auth=STAFF_AUTH,
    json={"status": "approved", "reviewer_notes": "Ready for launch."},
    timeout=30,
)
print(response.json()["reviewed_by_role"])
```

## Settings and administration

### POST `/settings/organization`

- **Purpose:** save organization profile fields.
- **Auth:** Basic Auth required.
- **Roles:** `admin`

**Request body:** any non-empty JSON object. Common fields include `name`, `mission`, `registration_number`, `privacy_email`, `contact_email`, and `privacy_jurisdictions`.

**Example request**

```json
{
  "name": "Example Org",
  "mission": "Expand STEM access"
}
```

**Example response**

```json
{
  "organization_profile": {
    "name": "Example Org",
    "mission": "Expand STEM access"
  }
}
```

**curl**

```bash
curl -u admin:admin-secret \
  -H 'Content-Type: application/json' \
  -d '{"name":"Example Org","mission":"Expand STEM access"}' \
  http://127.0.0.1:5000/settings/organization
```

**Python**

```python
response = requests.post(
    f"{BASE_URL}/settings/organization",
    auth=ADMIN_AUTH,
    json={"name": "Example Org", "mission": "Expand STEM access"},
    timeout=30,
)
print(response.json()["organization_profile"])
```

### POST `/settings/search`

- **Purpose:** save discovery keywords and trusted sources.
- **Auth:** Basic Auth required.
- **Roles:** `admin`

**Request body**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `keywords` | list of strings or comma-separated string | no | Defaults to empty list |
| `trusted_sources` | list of strings or comma-separated string | no | Defaults to empty list |

**Example response**

```json
{
  "search_settings": {
    "keywords": ["education", "stem"],
    "trusted_sources": ["grants.gov"]
  }
}
```

**curl**

```bash
curl -u admin:admin-secret \
  -H 'Content-Type: application/json' \
  -d '{"keywords":["education","stem"],"trusted_sources":["grants.gov"]}' \
  http://127.0.0.1:5000/settings/search
```

**Python**

```python
response = requests.post(
    f"{BASE_URL}/settings/search",
    auth=ADMIN_AUTH,
    json={"keywords": ["education", "stem"], "trusted_sources": ["grants.gov"]},
    timeout=30,
)
print(response.json()["search_settings"])
```

### POST `/settings/credentials`

- **Purpose:** register a logical credential alias.
- **Auth:** Basic Auth required.
- **Roles:** `admin`

**Request body**

```json
{
  "alias": "smtp",
  "env_var_name": "SMTP_PASSWORD"
}
```

**Response (`201`)**

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

**curl**

```bash
curl -u admin:admin-secret \
  -H 'Content-Type: application/json' \
  -d '{"alias":"smtp","env_var_name":"SMTP_PASSWORD"}' \
  http://127.0.0.1:5000/settings/credentials
```

**Python**

```python
response = requests.post(
    f"{BASE_URL}/settings/credentials",
    auth=ADMIN_AUTH,
    json={"alias": "smtp", "env_var_name": "SMTP_PASSWORD"},
    timeout=30,
)
print(response.json()["credentials"])
```

### POST `/settings/discover`

- **Purpose:** run funding discovery immediately or enqueue it, depending on queue mode.
- **Auth:** Basic Auth required.
- **Roles:** `admin`

**Request body**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `keywords` | list/string | no | Overrides saved keywords for this run |
| `trusted_sources` | list/string | no | Overrides saved trusted sources |

**Synchronous example response (`200`)**

```json
{
  "mode": "cron",
  "legacy_cron_enabled": true,
  "count": 0,
  "new_opportunities": [],
  "duplicate": false,
  "idempotency_key": "34d525e5...",
  "task_run": {
    "task_id": "34d525e5...",
    "task_name": "discover_opportunities",
    "status": "completed",
    "progress": 100,
    "message": "Task completed."
  }
}
```

**Queued example response (`202`)**

```json
{
  "mode": "hybrid",
  "legacy_cron_enabled": true,
  "task_name": "funding_bot.discover_opportunities",
  "task_id": "job-123"
}
```

**curl**

```bash
curl -u admin:admin-secret \
  -H 'Content-Type: application/json' \
  -d '{"keywords":["education"]}' \
  http://127.0.0.1:5000/settings/discover
```

**Python**

```python
response = requests.post(
    f"{BASE_URL}/settings/discover",
    auth=ADMIN_AUTH,
    json={"keywords": ["education"]},
    timeout=60,
)
print(response.status_code, response.json()["mode"])
```

### POST `/settings/privacy-policy`

- **Purpose:** generate jurisdiction-aware privacy policy artifacts.
- **Auth:** Basic Auth required.
- **Roles:** `admin`

**Request body**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `output_dir` | string | yes | Must not be empty |
| `jurisdictions` | list/string | no | Defaults from profile or residency |
| `formats` | list of strings | no | Supported: `html`, `pdf` |
| `effective_date` | `YYYY-MM-DD` | no | Optional policy date |

**Example response (`201`)**

```json
{
  "policies": [
    {
      "jurisdiction": "EU",
      "revision": 1,
      "version": "eu-v1",
      "effective_date": "2026-07-19",
      "data_residency": "US",
      "html_path": "generated/privacy_policies_test/privacy_policy_eu_eu-v1.html",
      "pdf_path": null
    }
  ],
  "residency_status": {
    "data_residency": "US",
    "storage_region": "US",
    "compliant": true
  },
  "versions": [
    {
      "version": "eu-v1",
      "generated_at": "2026-07-19T04:34:11.917415+00:00"
    }
  ]
}
```

**curl**

```bash
curl -u admin:admin-secret \
  -H 'Content-Type: application/json' \
  -d '{"output_dir":"generated/privacy_policies","jurisdictions":["EU"],"formats":["html"],"effective_date":"2026-07-19"}' \
  http://127.0.0.1:5000/settings/privacy-policy
```

**Python**

```python
payload = {
    "output_dir": "generated/privacy_policies",
    "jurisdictions": ["EU"],
    "formats": ["html"],
    "effective_date": "2026-07-19",
}
response = requests.post(
    f"{BASE_URL}/settings/privacy-policy",
    auth=ADMIN_AUTH,
    json=payload,
    timeout=60,
)
print(response.json()["policies"])
```

### POST `/settings/test-outreach`

- **Purpose:** compose a test outreach email and optionally send it.
- **Auth:** Basic Auth required.
- **Roles:** `admin`

**Request body**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `email` | string | yes | Recipient email |
| `name` | string | yes | Recipient display name |
| `dry_run` | boolean | no | Defaults to `true` |
| `subject_template` | string or `null` | no | Custom subject |
| `body_template` | string or `null` | no | Custom body |
| `locale` | string or `null` | no | Locale override |

**Example response (`201`)**

```json
{
  "template_name": "default",
  "email": "donor@example.org",
  "subject": "Thank you for supporting Example Org",
  "body": "Dear Donor Example,\n\nThank you for your continued interest in Example Org.\n\nTo opt out of future outreach, visit https://example.org/unsubscribe.",
  "locale": "en",
  "sent_at": "2026-07-19T04:34:11.929946+00:00",
  "dry_run": true
}
```

**curl**

```bash
curl -u admin:admin-secret \
  -H 'Content-Type: application/json' \
  -d '{"email":"donor@example.org","name":"Donor Example"}' \
  http://127.0.0.1:5000/settings/test-outreach
```

**Python**

```python
response = requests.post(
    f"{BASE_URL}/settings/test-outreach",
    auth=ADMIN_AUTH,
    json={"email": "donor@example.org", "name": "Donor Example"},
    timeout=30,
)
print(response.json()["dry_run"])
```

## Task APIs

### GET `/tasks` and `/task-directory`

- **Purpose:** list tasks visible to the current role.
- **Auth:** Basic Auth required.
- **Roles:** `staff`, `admin`, `auditor`

**Query parameters**

| Name | Description |
| --- | --- |
| `assigned_to` / `assignee` | Filter by assignee |
| `assignee_email` | Filter by assignee email |
| `status` | Filter by task status |
| `due_date_before` | Inclusive due-date upper bound |
| `due_date_after` | Inclusive due-date lower bound |
| `source` | Filter by source |
| `sort` | One of `assignee`, `-assignee`, `title`, `-title`, `due_date`, `-due_date`, `status`, `-status`, `created_at`, `-created_at`, `updated_at`, `-updated_at` |
| `viewer_email` | Adds `unread_comment_count` for that viewer |

`staff` and `auditor` users are scope-limited: non-admin users cannot request another assignee's task list.

**Example response**

```json
[
  {
    "id": 1,
    "external_id": null,
    "title": "Draft narrative",
    "description": "Complete proposal narrative",
    "assigned_to": "staff",
    "assignee": "staff",
    "assignee_email": "staff@example.org",
    "assignee_name": "Staff User",
    "status": "todo",
    "due_date": "2026-07-31",
    "source": "manual",
    "created_at": "2026-07-19T04:34:11.818365+00:00",
    "updated_at": "2026-07-19T04:34:11.818365+00:00",
    "is_overdue": false,
    "unread_comment_count": 0,
    "data_classification": "internal"
  }
]
```

**curl**

```bash
curl -u admin:admin-secret \
  "http://127.0.0.1:5000/tasks?status=todo&sort=due_date"
```

**Python**

```python
response = requests.get(
    f"{BASE_URL}/tasks",
    auth=ADMIN_AUTH,
    params={"status": "todo", "sort": "due_date"},
    timeout=30,
)
print(response.json())
```

### POST `/tasks` and `/task-directory`

- **Purpose:** create a task.
- **Auth:** Basic Auth required.
- **Roles:** `admin`

**Request body**

| Field | Type | Required |
| --- | --- | --- |
| `title` | string | yes |
| `assigned_to` | string | yes |
| `description` | string | no |
| `status` | string | no |
| `due_date` | `YYYY-MM-DD` | no |
| `external_id` | string or `null` | no |
| `source` | string | no |
| `assignee_email` | string or `null` | no |
| `assignee_name` | string or `null` | no |

**Response schema (`201`)**

```json
{
  "task": {},
  "notification": null
}
```

If email delivery is configured, `notification` contains assignment-delivery metadata.

**curl**

```bash
curl -u admin:admin-secret \
  -H 'Content-Type: application/json' \
  -d '{"title":"Review budget","assigned_to":"auditor","description":"Audit budget","due_date":"2026-08-01"}' \
  http://127.0.0.1:5000/tasks
```

**Python**

```python
payload = {
    "title": "Review budget",
    "assigned_to": "auditor",
    "description": "Audit budget",
    "due_date": "2026-08-01",
}
response = requests.post(f"{BASE_URL}/tasks", auth=ADMIN_AUTH, json=payload, timeout=30)
print(response.json()["task"]["id"])
```

### GET `/tasks/{task_id}` and `/task-directory/{task_id}`

- **Purpose:** fetch one task.
- **Auth:** Basic Auth required.
- **Roles:** `staff`, `admin`, `auditor`

**Query parameters**

| Name | Description |
| --- | --- |
| `viewer_email` | Adds unread comment count for that viewer |

**Example response**

```json
{
  "task": {
    "id": 1,
    "title": "Draft narrative",
    "assigned_to": "staff",
    "unread_comment_count": 1
  }
}
```

**curl**

```bash
curl -u staff:staff-secret \
  "http://127.0.0.1:5000/tasks/1?viewer_email=staff@example.org"
```

**Python**

```python
response = requests.get(
    f"{BASE_URL}/tasks/1",
    auth=STAFF_AUTH,
    params={"viewer_email": "staff@example.org"},
    timeout=30,
)
print(response.json()["task"])
```

### GET `/api/tasks/export`

- **Purpose:** export filtered tasks with a count wrapper.
- **Auth:** Basic Auth required.
- **Roles:** `admin`, `auditor`
- **Query parameters:** same as `GET /tasks`

**Example response**

```json
{
  "count": 2,
  "tasks": [
    {
      "id": 2,
      "title": "Review budget"
    },
    {
      "id": 1,
      "title": "Draft narrative"
    }
  ]
}
```

**curl**

```bash
curl -u auditor:auditor-secret \
  "http://127.0.0.1:5000/api/tasks/export?sort=due_date"
```

**Python**

```python
response = requests.get(f"{BASE_URL}/api/tasks/export", auth=AUDITOR_AUTH, timeout=30)
print(response.json()["count"])
```

### POST `/api/tasks/sync`

- **Purpose:** upsert a batch of task objects from another system.
- **Auth:** Basic Auth required.
- **Roles:** `admin`

**Request body**

```json
{
  "source": "external_sync",
  "tasks": [
    {
      "external_id": "ext-1",
      "title": "Imported task",
      "assignee": "staff",
      "status": "pending",
      "due_date": "2026-08-05"
    }
  ]
}
```

**Response (`200`)**

```json
{
  "count": 1,
  "tasks": [
    {
      "id": 3,
      "external_id": "ext-1",
      "title": "Imported task",
      "assigned_to": "staff",
      "status": "todo"
    }
  ]
}
```

**curl**

```bash
curl -u admin:admin-secret \
  -H 'Content-Type: application/json' \
  -d '{"source":"external_sync","tasks":[{"external_id":"ext-1","title":"Imported task","assignee":"staff","status":"pending","due_date":"2026-08-05"}]}' \
  http://127.0.0.1:5000/api/tasks/sync
```

**Python**

```python
payload = {
    "source": "external_sync",
    "tasks": [
        {
            "external_id": "ext-1",
            "title": "Imported task",
            "assignee": "staff",
            "status": "pending",
            "due_date": "2026-08-05",
        }
    ],
}
response = requests.post(f"{BASE_URL}/api/tasks/sync", auth=ADMIN_AUTH, json=payload, timeout=30)
print(response.json())
```

### POST `/api/tasks/import`

- **Purpose:** import tasks from CSV.
- **Auth:** Basic Auth required.
- **Roles:** `admin`

The endpoint accepts:

- raw CSV request body, or
- multipart form upload using the `file` field

Optional source override:

- query string `?source=...`, or
- multipart form field `source`

**CSV columns**

```csv
title,assigned_to,description,status,due_date,external_id,source,assignee_email,assignee_name
Prepare attachments,staff,Gather letters,pending,2026-08-10,ext-2,csv_import,staff@example.org,Staff User
```

**Response (`201`)**

```json
{
  "count": 1,
  "tasks": [
    {
      "id": 4,
      "title": "Prepare attachments",
      "assigned_to": "staff"
    }
  ]
}
```

**curl**

```bash
curl -u admin:admin-secret \
  -H 'Content-Type: text/csv' \
  --data-binary $'title,assigned_to,description,status,due_date\nPrepare attachments,staff,Gather letters,pending,2026-08-10' \
  "http://127.0.0.1:5000/api/tasks/import?source=csv_import"
```

**Python**

```python
csv_text = "title,assigned_to,description,status,due_date\\nPrepare attachments,staff,Gather letters,pending,2026-08-10\\n"
response = requests.post(
    f"{BASE_URL}/api/tasks/import",
    auth=ADMIN_AUTH,
    params={"source": "csv_import"},
    data=csv_text,
    headers={"Content-Type": "text/csv"},
    timeout=30,
)
print(response.json())
```

### POST `/tasks/{task_id}/assign`, `/tasks/{task_id}/assignment`, `/task-directory/{task_id}/assignment`

- **Purpose:** reassign a task and optionally attempt notification delivery.
- **Auth:** Basic Auth required.
- **Roles:** `admin`

**Request body**

```json
{
  "assigned_to": "staff",
  "assignee_email": "staff@example.org",
  "assignee_name": "Staff User"
}
```

**Example response**

```json
{
  "task": {
    "id": 1,
    "assigned_to": "staff",
    "assignment_notification": {
      "status": "skipped",
      "reason": "no_sender",
      "recipient_email": "staff@example.org"
    }
  },
  "notification": {
    "status": "skipped",
    "reason": "no_sender",
    "recipient_email": "staff@example.org"
  }
}
```

**curl**

```bash
curl -u admin:admin-secret \
  -H 'Content-Type: application/json' \
  -d '{"assigned_to":"staff","assignee_email":"staff@example.org","assignee_name":"Staff User"}' \
  http://127.0.0.1:5000/tasks/1/assign
```

**Python**

```python
response = requests.post(
    f"{BASE_URL}/tasks/1/assign",
    auth=ADMIN_AUTH,
    json={
        "assigned_to": "staff",
        "assignee_email": "staff@example.org",
        "assignee_name": "Staff User",
    },
    timeout=30,
)
print(response.json()["notification"])
```

### GET `/tasks/{task_id}/comments`

- **Purpose:** list task comments and unread count.
- **Auth:** Basic Auth required.
- **Roles:** `staff`, `admin`, `auditor`

**Query parameters**

| Name | Description |
| --- | --- |
| `viewer_email` | Optional email for unread count calculation |

**Example response**

```json
{
  "task": {
    "id": 1,
    "title": "Draft narrative",
    "unread_comment_count": 1
  },
  "comments": [
    {
      "id": 1,
      "task_id": 1,
      "author": "admin@example.org",
      "content": "Need attachments",
      "created_at": "2026-07-19T04:34:11.828057+00:00",
      "updated_at": "2026-07-19T04:34:11.828057+00:00",
      "data_classification": "internal"
    }
  ],
  "unread_count": 1
}
```

**curl**

```bash
curl -u staff:staff-secret \
  "http://127.0.0.1:5000/tasks/1/comments?viewer_email=staff@example.org"
```

**Python**

```python
response = requests.get(
    f"{BASE_URL}/tasks/1/comments",
    auth=STAFF_AUTH,
    params={"viewer_email": "staff@example.org"},
    timeout=30,
)
print(response.json()["comments"])
```

### POST `/tasks/{task_id}/comments`

- **Purpose:** create a task comment.
- **Auth:** Basic Auth required.
- **Roles:** `staff`, `admin`

**Request body**

```json
{
  "author": "admin@example.org",
  "content": "Please add a timeline."
}
```

**Response (`201`)**

```json
{
  "id": 1,
  "task_id": 1,
  "author": "admin@example.org",
  "content": "Please add a timeline.",
  "created_at": "2026-07-19T04:34:11.828057+00:00",
  "updated_at": "2026-07-19T04:34:11.828057+00:00",
  "data_classification": "internal"
}
```

**curl**

```bash
curl -u admin:admin-secret \
  -H 'Content-Type: application/json' \
  -d '{"author":"admin@example.org","content":"Please add a timeline."}' \
  http://127.0.0.1:5000/tasks/1/comments
```

**Python**

```python
response = requests.post(
    f"{BASE_URL}/tasks/1/comments",
    auth=ADMIN_AUTH,
    json={"author": "admin@example.org", "content": "Please add a timeline."},
    timeout=30,
)
print(response.json()["id"])
```

### PATCH `/tasks/{task_id}/comments/{comment_id}`

- **Purpose:** update a comment body.
- **Auth:** Basic Auth required.
- **Roles:** `staff`, `admin`

**Request body**

```json
{
  "content": "Please add a timeline and budget."
}
```

**curl**

```bash
curl -u admin:admin-secret \
  -X PATCH \
  -H 'Content-Type: application/json' \
  -d '{"content":"Please add a timeline and budget."}' \
  http://127.0.0.1:5000/tasks/1/comments/1
```

**Python**

```python
response = requests.patch(
    f"{BASE_URL}/tasks/1/comments/1",
    auth=ADMIN_AUTH,
    json={"content": "Please add a timeline and budget."},
    timeout=30,
)
print(response.json()["content"])
```

### DELETE `/tasks/{task_id}/comments/{comment_id}`

- **Purpose:** delete a comment.
- **Auth:** Basic Auth required.
- **Roles:** `staff`, `admin`
- **Response:** `204 No Content`

**curl**

```bash
curl -u admin:admin-secret -X DELETE \
  http://127.0.0.1:5000/tasks/1/comments/1
```

**Python**

```python
response = requests.delete(f"{BASE_URL}/tasks/1/comments/1", auth=ADMIN_AUTH, timeout=30)
print(response.status_code)
```

### POST `/tasks/{task_id}/comments/read`

- **Purpose:** mark task comments as read for a specific email address.
- **Auth:** Basic Auth required.
- **Roles:** `staff`, `admin`, `auditor`

**Request body**

```json
{
  "reader_email": "staff@example.org"
}
```

**Response (`200`)**

```json
{
  "task_id": 1,
  "reader_email": "staff@example.org",
  "last_read_at": "2026-07-19T04:34:11.970785+00:00",
  "unread_count": 0
}
```

**curl**

```bash
curl -u staff:staff-secret \
  -H 'Content-Type: application/json' \
  -d '{"reader_email":"staff@example.org"}' \
  http://127.0.0.1:5000/tasks/1/comments/read
```

**Python**

```python
response = requests.post(
    f"{BASE_URL}/tasks/1/comments/read",
    auth=STAFF_AUTH,
    json={"reader_email": "staff@example.org"},
    timeout=30,
)
print(response.json()["unread_count"])
```

### POST `/tasks/{task_id}/status`

- **Purpose:** transition a task through the workflow.
- **Auth:** Basic Auth required.
- **Roles:** `staff`, `admin`, `auditor`

Admins may move any task. Other roles may only move tasks assigned to themselves.

**Request body**

```json
{
  "status": "in-progress"
}
```

**Accepted statuses**

- `todo`
- `in-progress`
- `done`
- `blocked`

**Example response**

```json
{
  "task": {
    "id": 1,
    "title": "Draft narrative",
    "status": "in-progress",
    "notification": "Task 'Draft narrative' moved from todo to in-progress."
  },
  "notification": "Task 'Draft narrative' moved from todo to in-progress."
}
```

**curl**

```bash
curl -u staff:staff-secret \
  -H 'Content-Type: application/json' \
  -d '{"status":"in-progress"}' \
  http://127.0.0.1:5000/tasks/1/status
```

**Python**

```python
response = requests.post(
    f"{BASE_URL}/tasks/1/status",
    auth=STAFF_AUTH,
    json={"status": "in-progress"},
    timeout=30,
)
print(response.json()["notification"])
```

## Operations

### GET `/health`

- **Purpose:** return lightweight liveness status for the web process and database.
- **Auth:** none
- **Response codes:** `200` when healthy; `503` when the process cannot reach the database

**Example response**

```json
{
  "status": "ok",
  "healthy": true,
  "checks": {
    "application": {
      "status": "ok"
    },
    "database": {
      "status": "ok"
    }
  },
  "queue": {
    "mode": "cron",
    "queue_enabled": false,
    "legacy_cron_enabled": true,
    "queue_name": "funding-bot"
  }
}
```

**curl**

```bash
curl http://127.0.0.1:5000/health
```

**Python**

```python
response = requests.get(f"{BASE_URL}/health", timeout=30)
print(response.json()["status"])
```

### GET `/ready`

- **Purpose:** return readiness status for database, Redis, Celery, and connectors.
- **Auth:** none
- **Response codes:** `200` when all required dependencies are healthy or intentionally disabled; `503` otherwise

**Example response**

```json
{
  "status": "ok",
  "ready": true,
  "failing_checks": [],
  "checks": {
    "database": { "status": "ok" },
    "redis": { "status": "disabled" },
    "celery": { "status": "disabled" },
    "connectors": { "status": "ok", "count": 6, "healthy_count": 6 }
  }
}
```

### GET `/health/queue`

- **Purpose:** return queue-only health details.
- **Auth:** none
- **Response codes:** `200` when `status` is `ok` or `disabled`; `503` otherwise

**Example degraded response (`503`)**

```json
{
  "status": "degraded",
  "queue_name": "celery",
  "broker_reachable": false,
  "timeout_seconds": 2.0,
  "active_tasks": 0,
  "pending_tasks": 0,
  "queue_depth": 0,
  "worker_count": 0,
  "workers": [],
  "error": "Timed out while contacting the Celery broker: broker timed out"
}
```

**curl**

```bash
curl http://127.0.0.1:5000/health/queue
```

**Python**

```python
response = requests.get(f"{BASE_URL}/health/queue", timeout=30)
print(response.status_code, response.json()["status"])
```

### GET `/metrics`

- **Purpose:** expose Prometheus-compatible text metrics.
- **Auth:** Basic Auth required.
- **Roles:** `admin`, `auditor`
- **Response type:** `text/plain; version=0.0.4`

**Example response**

```text
# HELP funding_bot_opportunities_total Total funding opportunities discovered
# TYPE funding_bot_opportunities_total counter
funding_bot_opportunities_total 1
# HELP funding_bot_tasks_total Total collaboration tasks
# TYPE funding_bot_tasks_total gauge
funding_bot_tasks_total 2
```

**curl**

```bash
curl -u admin:admin-secret http://127.0.0.1:5000/metrics
```

**Python**

```python
response = requests.get(f"{BASE_URL}/metrics", auth=ADMIN_AUTH, timeout=30)
print(response.text.splitlines()[:4])
```

## Feedback

### POST `/feedback`

- **Purpose:** store partner feedback in the audit log.
- **Auth:** Basic Auth required.
- **Roles:** `staff`, `admin`

**Request body**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `category` | string | no | `feature_request`, `bug_report`, or `general`; defaults to `general` |
| `message` | string | yes | Max 2000 characters |
| `contact` | string or `null` | no | Optional reply-to email |

**Example request**

```json
{
  "category": "general",
  "message": "Looks good",
  "contact": "staff@example.org"
}
```

**Response (`201`)**

```json
{
  "status": "received",
  "category": "general"
}
```

**curl**

```bash
curl -u staff:staff-secret \
  -H 'Content-Type: application/json' \
  -d '{"category":"general","message":"Looks good","contact":"staff@example.org"}' \
  http://127.0.0.1:5000/feedback
```

**Python**

```python
payload = {
    "category": "general",
    "message": "Looks good",
    "contact": "staff@example.org",
}
response = requests.post(f"{BASE_URL}/feedback", auth=STAFF_AUTH, json=payload, timeout=30)
print(response.json())
```

## Error responses

All API errors use:

```json
{
  "error": "message"
}
```

### Common examples

**Missing authentication (`401`)**

```json
{
  "error": "Authentication required"
}
```

**Insufficient role (`403`)**

```json
{
  "error": "Forbidden"
}
```

**Validation error (`400`)**

```json
{
  "error": "Field 'status' is required."
}
```

**Not found (`404`)**

```json
{
  "error": "Task 999 does not exist."
}
```

**Unexpected error (`500`)**

```json
{
  "error": "Internal server error"
}
```

## HTML dashboard routes

These routes are part of `web/app.py` but are HTML/redirect endpoints rather than REST responses:

| Method | Path | Roles | Behavior |
| --- | --- | --- | --- |
| GET | `/` | none | Redirects to `/dashboard` |
| GET | `/dashboard` | staff, admin, auditor | Renders the main dashboard |
| GET | `/dashboard/tasks` | staff, admin, auditor | Renders the task board |
| GET | `/settings` | staff, admin, auditor | Renders the settings page |
| GET | `/translations` | staff, admin, auditor | Renders the translation review page |
