# Compliance Procedures

## Scope

This document summarizes the repository's operational compliance controls for:

- ISO 27001-aligned data handling
- GDPR-oriented privacy operations
- data retention and cleanup procedures
- accessibility review coordination

## ISO 27001-aligned data-handling checklist

Use this checklist for releases, environment changes, and quarterly control reviews.

- [ ] Access to dashboard roles (`ADMIN_PASSWORD`, `STAFF_PASSWORD`, `AUDITOR_PASSWORD`) is limited to approved personnel.
- [ ] Secrets are stored in environment variables, secret files, or platform secret stores; secret values are never committed.
- [ ] Credential aliases only expose alias names and backing environment-variable names.
- [ ] SQLite data storage location (`BOT_DB_PATH`) is documented and access-controlled.
- [ ] Audit logs are enabled and reviewed for privileged actions.
- [ ] Donor opt-out state is enforced before outreach is sent.
- [ ] Monthly compliance/audit reports are generated and retained per policy.
- [ ] Data retention settings are reviewed and approved by an owner.
- [ ] Expired communications, documents, audit logs, and completed operational records are purged on schedule.
- [ ] Accessibility and privacy findings are tracked before release sign-off.
- [ ] Backups, restore tests, and infrastructure change approvals are handled by the deployment owner.
- [ ] Security incidents, privacy complaints, and access revocations are recorded in the operations log.

## GDPR checklist

- [ ] A lawful basis/consent path exists before donor outreach.
- [ ] Opt-out/unsubscribe information is present in outreach content.
- [ ] Subject-access exports can be fulfilled with `gdpr_export()` workflows.
- [ ] Erasure/anonymization requests can be fulfilled with `gdpr_delete()` workflows.
- [ ] Audit evidence exists for exports, deletions, opt-outs, and monthly reviews.
- [ ] Retention settings are documented and do not exceed approved operational need.
- [ ] Personal data in generated documents is deleted when documents expire.
- [ ] Test or demo data is anonymized before sharing outside the team.

## Data retention configuration

The bot supports a stored retention policy plus environment-variable defaults.

### Configurable fields

- `audit_logs_days` → `RETENTION_AUDIT_LOG_DAYS` (default `365`)
- `communications_days` → `RETENTION_COMMUNICATION_DAYS` (default `365`)
- `documents_days` → `RETENTION_DOCUMENT_DAYS` (default `180`)
- `submission_attempts_days` → `RETENTION_SUBMISSION_ATTEMPT_DAYS` (default `90`)
- `completed_tasks_days` → `RETENTION_COMPLETED_TASK_DAYS` (default `180`)

### CLI procedures

Set policy values:

```bash
python -m funding_bot set-data-retention-policy \
  --audit-logs-days 365 \
  --communications-days 365 \
  --documents-days 180 \
  --submission-attempts-days 90 \
  --completed-tasks-days 180
```

Preview cleanup without deleting data:

```bash
python -m funding_bot enforce-data-retention --dry-run
```

Run cleanup:

```bash
python -m funding_bot enforce-data-retention
```

## Operational procedures

### Monthly compliance review

1. Run `python -m funding_bot monthly-audit-report`.
2. Review audit-log anomalies, GDPR operations, and outreach statistics.
3. Run `python -m funding_bot enforce-data-retention --dry-run` to confirm pending deletions.
4. Approve or adjust retention settings if counts are unexpected.
5. Run the real cleanup job and retain the resulting audit evidence.
6. Review accessibility status against `docs/ACCESSIBILITY.md`.

### Subject access / deletion

1. Verify requester identity through your organization process.
2. Export records with the GDPR workflow.
3. Deliver the export securely.
4. If erasure is approved, run the deletion/anonymization workflow.
5. Confirm the audit log contains the export/deletion events.

### Incident response notes

If a privacy or security issue is suspected:

1. stop non-essential outreach or imports if needed,
2. preserve audit evidence,
3. rotate affected credentials,
4. review recent audit log entries,
5. document remediation and retention follow-up.

## Related docs

- [Accessibility status](ACCESSIBILITY.md)
- [Glossary of key terms](GLOSSARY.md)
- [Video walkthroughs](VIDEOS.md)
- [Repository overview](../README.md)
