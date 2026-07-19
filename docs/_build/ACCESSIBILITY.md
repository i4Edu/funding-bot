# Accessibility Conformance Status

## Scope

This statement covers the Flask dashboard and operator workflows shipped in this repository:

- `/dashboard`
- `/dashboard/tasks`
- `/settings`
- JSON API routes used by staff, admin, and auditor roles
- CLI output intended for terminal use

## Current status

**Conformance target:** WCAG 2.1 AA  
**Current assessment:** **Partially conforms** (self-assessed)

The project includes several accessibility-first patterns, but it does not yet have full automated or third-party accessibility certification.

## Implemented accessibility support

### Dashboard and settings UI

- Semantic HTML documents with `lang="en"`
- Skip links to main content on dashboard, tasks, and settings pages
- Form controls with explicit labels
- Semantic headings and grouped regions
- Accessible table headers using `scope="col"`
- Navigation landmarks with `role="navigation"` and `aria-label`
- Visible text alternatives for status badges and empty states
- Keyboard-usable Bootstrap-based controls

### API and CLI

- JSON errors return plain, structured messages
- CLI commands produce text/JSON output that can be consumed by screen readers and terminal tooling
- Dry-run modes are available for operational verification without side effects

## Known gaps / limitations

The following items should be treated as open work before claiming full WCAG 2.1 AA conformance:

1. No automated axe/pa11y/Lighthouse accessibility test suite is committed in this repo.
2. No documented screen-reader validation across NVDA, JAWS, or VoiceOver.
3. Color contrast depends on Bootstrap defaults and has not been independently audited in this repository.
4. Dynamic success/error messages in the settings page are not announced through dedicated live regions.
5. There is no formal accessibility issue intake/SLA workflow beyond normal backlog triage.
6. Bengali/localized UI accessibility has not been separately validated.

## Operator guidance

- Use semantic labels and headings when editing templates under `web/templates/`.
- Preserve skip links, table headers, and visible form labels.
- Prefer plain-language error messages in API and CLI output.
- Record accessibility regressions in the audit/compliance review process before release.

## Recommended verification procedure

For each release:

1. Manually test keyboard-only navigation of `/dashboard`, `/dashboard/tasks`, and `/settings`.
2. Verify focus reaches the skip link and all actionable controls.
3. Confirm every new form control has an associated label.
4. Review status/error text for clarity and screen-reader friendliness.
5. Track findings in the monthly compliance review alongside GDPR/retention checks.

## Exceptions

This repository does **not** currently claim audited legal compliance with WCAG, ADA, EN 301 549, or Section 508. The current statement is an engineering status summary for release and operational planning.
