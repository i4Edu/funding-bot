# Funding Bot Video Walkthroughs

This guide tracks onboarding and feature walkthrough videos for staff. Replace placeholder URLs with published YouTube, LMS, or internal recording links as videos become available.

## Walkthrough library

| Topic | Audience | Video link | Related docs |
| --- | --- | --- | --- |
| Setup and first run | New admins and staff | [Placeholder walkthrough](https://example.com/funding-bot/videos/setup-and-first-run) | [QUICKSTART.md](QUICKSTART.md), [ENV_VARS.md](ENV_VARS.md) |
| First connector search | Operators configuring discovery | [Placeholder walkthrough](https://example.com/funding-bot/videos/first-connector-search) | [CONNECTORS.md](CONNECTORS.md), [GLOSSARY.md#connector](GLOSSARY.md#connector) |
| Reviewing deduplication results | Reviewers and analysts | [Placeholder walkthrough](https://example.com/funding-bot/videos/reviewing-dedup-results) | [GLOSSARY.md#deduplication](GLOSSARY.md#deduplication), [README.md](../README.md) |
| Exports and compliance handoff | Admins and auditors | [Placeholder walkthrough](https://example.com/funding-bot/videos/exports-and-compliance) | [COMPLIANCE.md](COMPLIANCE.md), [GLOSSARY.md#export](GLOSSARY.md#export) |

## Guide structure

### 1. Setup and first run

Recommended flow:

1. clone the repository and install Python dependencies,
2. copy `.env.example` to `.env`,
3. set dashboard passwords and local database path,
4. launch the dashboard or Docker Compose stack,
5. verify `/dashboard` and `/health`.

Primary docs:

- [Quickstart](QUICKSTART.md)
- [Environment variables](ENV_VARS.md)

### 2. First connector search

Recommended flow:

1. explain what a [connector](GLOSSARY.md#connector) is,
2. choose a built-in connector,
3. run `python -m funding_bot test-connector --connector <slug>`,
4. review keyword expansion and sample results,
5. show where normalized records appear in the app workflow.

Primary docs:

- [Connector guide](CONNECTORS.md)
- [Glossary: connector](GLOSSARY.md#connector)
- [Glossary: keyword mapping](GLOSSARY.md#keyword-mapping)

### 3. Reviewing deduplication results

Recommended flow:

1. define [deduplication](GLOSSARY.md#deduplication) and a [match](GLOSSARY.md#match),
2. compare duplicate candidates and stable signatures,
3. show how normalized fields affect duplicate review,
4. demonstrate how staff decide whether to merge or keep records separate,
5. call out audit and follow-up implications.

Primary docs:

- [Glossary: deduplication](GLOSSARY.md#deduplication)
- [Glossary: match](GLOSSARY.md#match)
- [README overview](../README.md)

### 4. Exports and compliance handoff

Recommended flow:

1. explain export-ready record selection,
2. demonstrate CSV or compliance export output,
3. review secure delivery expectations,
4. confirm audit logging and retention checks,
5. identify follow-up tasks for the requester.

Primary docs:

- [Compliance procedures](COMPLIANCE.md)
- [Glossary: export](GLOSSARY.md#export)
- [Glossary: retention](GLOSSARY.md#retention)

## Recording checklist

Use this checklist when publishing or replacing a placeholder video:

- confirm the narration matches current CLI and dashboard labels,
- blur or remove secrets, tokens, and real donor data,
- add timestamps for setup, connectors, deduplication, and exports,
- include links back to the matching docs page in the video description,
- update this file with the final public or internal URL.
