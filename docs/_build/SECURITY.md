# Security Policy

This document explains how to report vulnerabilities in the funding bot, how maintainers handle security incidents, and what operators should do to keep deployments secure.

## Supported versions

Security fixes are prioritized for the latest supported code in the default branch and the most recent tagged release. Older deployments should be upgraded before requesting non-critical fixes.

## Reporting a vulnerability

Please **do not open a public GitHub issue** for suspected vulnerabilities.

Use one of these private channels instead:

1. **Preferred:** GitHub private vulnerability reporting / GitHub Security Advisories for this repository.
2. **Fallback email:** `lupael@i4e.com.bd`
   - Use the subject line: `[SECURITY] funding-bot vulnerability report`
   - Include encrypted details if possible
   - Provide a safe callback address and your disclosure preferences

## What to include in a report

Please include:

- affected version, branch, commit SHA, or deployment image tag
- vulnerability type and impacted component
- reproduction steps or proof of concept
- severity and likely impact
- whether credentials, personal data, or donor data may be exposed
- any suggested mitigation or patch ideas
- whether you want public credit after disclosure

## Responsible disclosure timeline

Maintainers will use the following target timeline:

- **Within 2 business days:** acknowledge receipt
- **Within 5 business days:** complete initial triage and confirm severity / scope
- **Within 14 calendar days:** share remediation plan or compensating control guidance
- **Within 30 calendar days:** target fix for critical and high-severity issues when a patch is feasible
- **Within 90 calendar days:** coordinated public disclosure target for most validated issues

If a fix requires more time, maintainers should provide status updates at least every 14 days until the issue is resolved or a workaround is published.

## Disclosure workflow

1. Receive the report through a private channel.
2. Acknowledge receipt and assign an internal owner.
3. Reproduce the issue and classify severity, exploitability, and data exposure risk.
4. Apply temporary mitigations if immediate containment is needed.
5. Prepare and validate a fix.
6. Coordinate release notes, deployment guidance, and any required secret rotation steps.
7. Publish the advisory after remediation, or earlier if active exploitation requires rapid notice.

## CVE process

When a confirmed vulnerability meets CVE publication criteria, maintainers should:

1. create or update a GitHub Security Advisory
2. request a CVE ID through GitHub's CNA flow, if available, or through the appropriate CNA / MITRE process
3. document affected versions, fixed versions, severity, CWE mapping, and mitigation guidance
4. link the advisory, release notes, and remediation commit or tag
5. notify downstream operators when they need to rotate secrets, revoke tokens, or restore data

## Incident response procedures

When an incident affects a running deployment:

### 1. Triage

- confirm whether the incident is active, historical, or a false positive
- identify impacted systems, users, donors, credentials, and data classes
- preserve logs, task metadata, and audit records needed for investigation

### 2. Containment

- disable exposed credentials, API tokens, and SMTP secrets
- isolate affected web, worker, or database infrastructure
- pause risky automation such as donor outreach or connector polling if needed
- restrict dashboard access to admins and responders only

### 3. Eradication and recovery

- remove the root cause
- deploy the fix to web, CLI, and worker environments
- rotate credentials and invalidate sessions where applicable
- restore from clean backups if integrity is uncertain
- verify `/health` and `/health/queue` before resuming normal operations

### 4. Communication

- notify maintainers and deployment owners immediately
- notify affected partners or donors when required by contract or law
- publish operator guidance for patches, workarounds, and secret rotation

### 5. Post-incident review

- document timeline, root cause, blast radius, and lessons learned
- add tests, alerts, or runbooks that would have prevented recurrence
- update this policy and deployment guidance if the incident revealed gaps

## Periodic automated penetration testing checklist

Run this checklist on a recurring schedule (recommended: monthly for internet-facing deployments, quarterly for lower-risk internal environments, and before major releases):

- [ ] verify dependency vulnerability scanning for Python and Node dependencies
- [ ] run application tests before the security scan to confirm a stable baseline
- [ ] scan authenticated dashboard routes for broken access control and role leakage
- [ ] test login flows for weak default credentials and missing rate limits
- [ ] verify CSRF, XSS, template injection, and input-validation protections on form and JSON endpoints
- [ ] exercise file/document generation paths for path traversal and unsafe template rendering
- [ ] validate queue, broker, and worker endpoints are not publicly exposed
- [ ] confirm `/metrics`, `/health`, and `/health/queue` expose no secrets or sensitive internals
- [ ] test audit log and donor-export paths for unauthorized data access
- [ ] review SMTP, credential alias, and environment-variable handling for accidental secret disclosure
- [ ] verify container and deployment images use current patched base images
- [ ] record findings, owners, due dates, and retest results in the security advisory or internal tracker

## Security best practices

Operators and contributors should:

- keep dependencies updated and promptly apply security patches
- store secrets in environment variables or a secrets manager, never in source control
- rotate SMTP, API, and dashboard credentials regularly
- enforce strong unique passwords for `admin`, `staff`, and `auditor` roles
- run the dashboard and Flower behind authentication and trusted network boundaries
- use HTTPS/TLS in every non-local deployment
- keep CSRF protection enabled for all session-backed dashboard forms and AJAX actions
- tune `WEB_AUTH_RATE_LIMIT`, `WEB_API_RATE_LIMIT`, and `WEB_EXPORT_RATE_LIMIT` to match expected traffic
- limit broker, database, and worker network access to required services only
- review audit logs regularly for unusual discovery, outreach, and settings changes
- back up the SQLite database and verify restore procedures
- minimize donor data retention and follow documented GDPR deletion/export processes
- validate security changes in CI and again before release

## Out of scope

Please avoid testing that could harm production availability or privacy, including:

- denial-of-service attacks against shared environments
- social engineering of maintainers or partners
- physical security testing
- access to third-party infrastructure not owned by this project
