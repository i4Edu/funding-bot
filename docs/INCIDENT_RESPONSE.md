# Incident Response Runbook

This runbook covers suspected or confirmed exposure of donor, organization, credential-reference, or outreach data stored by Funding Bot.

## Severity guide

| Severity | Example |
| --- | --- |
| SEV-1 | Confirmed exposure of secret or confidential donor/organization data |
| SEV-2 | Suspected unauthorized access with incomplete evidence |
| SEV-3 | Policy violation or near miss with no confirmed exposure |

## 1. Detect and declare

1. Open an incident ticket and assign an incident commander.
2. Record detection time, reporter, affected environment, and known scope.
3. Preserve logs, database copies, and deployment metadata needed for forensics.
4. Classify the incident severity using the table above.

## 2. Contain

1. Rotate `FUNDING_BOT_ENCRYPTION_KEY` if secret or confidential profile/donor data may be exposed.
2. Rotate SMTP, connector, and vault credentials referenced by Funding Bot.
3. Disable impacted integrations, queue workers, or dashboard access paths.
4. Snapshot the affected SQLite database before making cleanup changes.

## 3. Eradicate

1. Identify the root cause (credential misuse, vulnerable dependency, misconfiguration, or code defect).
2. Remove unauthorized accounts, tokens, or access paths.
3. Patch the defect and deploy the fix.
4. Verify audit logs include the timeline of classification or access changes relevant to the breach.

## 4. Recover

1. Restore service in stages, starting with internal-only access.
2. Validate donor records, organization profile data, and outreach queues.
3. Confirm encrypted fields still decrypt correctly after any key rotation or data migration.
4. Monitor audit logs, queue health, and outbound communications for at least 24 hours.

## 5. Notification procedures

### Internal notification

Notify the incident commander, engineering owner, security/compliance owner, and nonprofit operations lead immediately after SEV-1 or SEV-2 declaration.

### External notification

1. Determine affected data subjects and jurisdictions.
2. For donor or organization data classified `confidential` or `secret`, prepare regulator and partner notices with:
   - incident summary
   - affected data classes and fields
   - time window
   - containment/remediation steps
   - recommended actions for recipients
3. Notify impacted donors, partner organizations, and platform owners without undue delay once scope is reliable.
4. Follow local legal deadlines (for example GDPR-style 72 hour expectations where applicable).

## 6. Evidence checklist

- incident ticket ID
- timestamps for detection, declaration, containment, and recovery
- affected tables/records
- audit log excerpts
- credential rotation confirmation
- customer/regulator notification status

## 7. Post-incident review

1. Complete a retrospective within 5 business days.
2. Document root cause, blast radius, gaps in controls, and follow-up actions.
3. Update classification tags, retention settings, and this runbook if the incident exposed missing safeguards.
