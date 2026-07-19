# Funding Bot Glossary

Use this glossary as a shared reference for the README, API guide, connector guide, and [video walkthroughs](VIDEOS.md).

## Term index

- [Application](#application)
- [Assignee](#assignee)
- [Audit log](#audit-log)
- [Connector](#connector)
- [Connector result cache](#connector-result-cache)
- [Daily summary](#daily-summary)
- [Data classification](#data-classification)
- [Deduplication](#deduplication)
- [Deduplication signature](#deduplication-signature)
- [Donor](#donor)
- [Export](#export)
- [Keyword mapping](#keyword-mapping)
- [Match](#match)
- [Normalization](#normalization)
- [Opportunity](#opportunity)
- [Organization](#organization)
- [Outreach](#outreach)
- [Portal](#portal)
- [Rate limit](#rate-limit)
- [Retention](#retention)
- [Task](#task)
- [Task queue](#task-queue)
- [Template](#template)
- [Worker](#worker)

## Application

An application is the organization's tracked submission for a funding [opportunity](#opportunity). It usually moves through review, drafting, submission, and follow-up steps.

**See also:** [Organization](#organization), [Task](#task)

## Assignee

An assignee is the staff member currently responsible for a [task](#task), review item, or follow-up action in the dashboard or API.

**See also:** [Task](#task), [Outreach](#outreach)

## Audit log

The audit log is the timestamped history of sensitive actions such as data changes, exports, outreach events, and compliance operations.

**See also:** [Export](#export), [Retention](#retention), [Task queue](#task-queue)

## Connector

A connector is the integration layer that queries a funding [portal](#portal), normalizes source records, and returns comparable [opportunity](#opportunity) data for filtering and [deduplication](#deduplication).

**See also:** [Keyword mapping](#keyword-mapping), [Normalization](#normalization), [Connector result cache](#connector-result-cache)

## Connector result cache

The connector result cache stores normalized connector responses so the bot can reuse recent results during retries, fallbacks, or offline validation.

**See also:** [Connector](#connector), [Normalization](#normalization), [Rate limit](#rate-limit)

## Daily summary

The daily summary is the scheduled report that highlights discoveries, submissions, donor activity, and queued work for the day.

**See also:** [Task queue](#task-queue), [Outreach](#outreach)

## Data classification

Data classification labels stored records as `public`, `internal`, `confidential`, or `secret` so the bot can apply the right access and handling rules.

**See also:** [Donor](#donor), [Organization](#organization), [Audit log](#audit-log)

## Deduplication

Deduplication is the process of detecting when two records likely describe the same [opportunity](#opportunity), donor, or imported item so staff do not work the same item twice.

**See also:** [Deduplication signature](#deduplication-signature), [Match](#match), [Normalization](#normalization)

## Deduplication signature

A deduplication signature is the stable comparison value built from normalized fields such as source, title, amount, or URL to support reliable [deduplication](#deduplication).

**See also:** [Deduplication](#deduplication), [Match](#match)

## Donor

A donor is an individual, institution, company, or funder record that the bot tracks for communication history, consent, segmentation, and [outreach](#outreach).

**See also:** [Organization](#organization), [Export](#export), [Data classification](#data-classification)

## Export

An export is a generated file or package, such as CSV, JSON, or compliance output, that bundles selected records for reporting, handoff, or data-subject access.

**See also:** [Audit log](#audit-log), [Donor](#donor), [Retention](#retention)

## Keyword mapping

Keyword mapping expands a search term into source-specific synonyms or categories so a [connector](#connector) can find more relevant [matches](#match).

**See also:** [Connector](#connector), [Portal](#portal)

## Match

A match is a record that satisfies search criteria or a duplicate-comparison rule after the bot evaluates normalized fields, keywords, and scoring logic.

**See also:** [Deduplication](#deduplication), [Keyword mapping](#keyword-mapping), [Opportunity](#opportunity)

## Normalization

Normalization is the cleanup step that reshapes source data into a shared schema before storage, filtering, export, or [deduplication](#deduplication).

**See also:** [Connector](#connector), [Match](#match), [Opportunity](#opportunity)

## Opportunity

An opportunity is a funding lead discovered from a [connector](#connector) or manual import, usually containing a title, funder, deadline, and summary.

**See also:** [Application](#application), [Deduplication](#deduplication)

## Organization

The organization is the nonprofit or team using Funding Bot to discover opportunities, manage records, coordinate [tasks](#task), and send donor [outreach](#outreach).

**See also:** [Donor](#donor), [Application](#application), [Data classification](#data-classification)

## Outreach

Outreach is the set of controlled donor or partner communications sent by staff, including emails, follow-ups, and engagement tracking with consent checks.

**See also:** [Donor](#donor), [Template](#template), [Daily summary](#daily-summary)

## Portal

A portal is an external grant, CSR, NGO, or crowdfunding source that a [connector](#connector) reads from during discovery.

**See also:** [Connector](#connector), [Opportunity](#opportunity)

## Rate limit

A rate limit is the source-side or app-side cap on how frequently requests or outreach actions may run during a given time window.

**See also:** [Connector](#connector), [Outreach](#outreach), [Worker](#worker)

## Retention

Retention is the policy that determines how long data, logs, and generated files are kept before deletion or anonymization.

**See also:** [Audit log](#audit-log), [Export](#export), [Data classification](#data-classification)

## Task

A task is a unit of work assigned to a teammate or background processor, such as reviewing an opportunity, following up with a donor, or generating a report.

**See also:** [Assignee](#assignee), [Task queue](#task-queue), [Application](#application)

## Task queue

The task queue is the asynchronous system that schedules slower jobs, such as discovery runs, report generation, and batched [outreach](#outreach), for background execution.

**See also:** [Task](#task), [Worker](#worker), [Daily summary](#daily-summary)

## Template

A template is the reusable text or document structure used to generate emails, summaries, and application documents with organization-specific data merged in.

**See also:** [Outreach](#outreach), [Export](#export), [Organization](#organization)

## Worker

A worker is the background process that pulls jobs from the [task queue](#task-queue) and runs them outside the main CLI or web request flow.

**See also:** [Task queue](#task-queue), [Connector](#connector), [Rate limit](#rate-limit)
