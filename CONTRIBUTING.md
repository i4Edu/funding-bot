# Contributing to Funding Bot

Thanks for contributing to Funding Bot. This guide explains how to set up a local environment, prepare changes, open pull requests, and participate in reviews.

## Code of conduct

By participating in this project, you agree to:

- be respectful, patient, and constructive in issues, pull requests, reviews, and chat
- assume positive intent while still giving direct, actionable feedback
- avoid harassment, discrimination, personal attacks, or sharing sensitive organizational data
- keep security, privacy, accessibility, and nonprofit user impact in mind when proposing changes

Project maintainers may remove or moderate comments, reviews, or contributions that do not meet these expectations. Security concerns should be reported through the process in [docs/SECURITY.md](docs/SECURITY.md), not disclosed publicly first.

## Ways to contribute

Common contribution areas include:

- connector improvements for grants, CSR, NGO, or crowdfunding sources
- dashboard and API enhancements for staff/admin/auditor workflows
- task queue reliability, deployment, and observability improvements
- tests, accessibility fixes, translation updates, and documentation

## Local setup

### Prerequisites

- Python 3.11+
- `pip`
- Docker and Docker Compose (recommended for full-stack testing)
- Node.js/npm only if you need to run accessibility checks

### Initial environment

1. Clone the repository and enter it:

   ```bash
   git clone <your-fork-or-upstream-url>
   cd funding-bot
   ```

2. Create local configuration:

   ```bash
   cp .env.example .env
   ```

3. Install Python dependencies:

   ```bash
   pip install -r web/requirements.txt
   ```

4. Optional: install accessibility test dependencies:

   ```bash
   npm install
   ```

5. Set local environment values in `.env` for at least:
   - `ADMIN_PASSWORD`, `STAFF_PASSWORD`, `AUDITOR_PASSWORD`
   - `BOT_DB_PATH`
   - SMTP variables if you plan to test real delivery
   - `ENABLE_TASK_QUEUE`, `CELERY_BROKER_URL`, and `CELERY_RESULT_BACKEND` if you plan to test queue-backed workflows

### Running locally

- Run tests:

  ```bash
  python -m unittest
  ```

- Run the web dashboard:

  ```bash
  python -m flask --app web.app run
  ```

- Run a worker when queue mode is enabled:

  ```bash
  celery -A celery_app:celery_app worker --loglevel=info --queues funding-bot
  ```

- Run the full Compose stack:

  ```bash
  docker compose --profile queue up --build
  ```

See [README.md](README.md), [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md), and [docs/COLLABORATION.md](docs/COLLABORATION.md) for runtime and workflow details.

## Branching and pull request guidelines

1. Create a focused branch from the latest default branch.
2. Keep each pull request scoped to one feature, fix, or documentation task when possible.
3. Update related docs and tests with the code change. If behavior changes, the README or `docs/` should usually change too.
4. Use clear commit messages that describe the user-visible or operator-visible change.
5. Push your branch and open a pull request with:
   - problem statement / motivation
   - summary of the solution
   - testing performed
   - screenshots or API examples for dashboard/UI changes
   - deployment or migration notes when queue, Docker, or Kubernetes behavior changes

### PR checklist

Before requesting review, confirm that you have:

- [ ] rebased or merged the latest target branch changes as needed
- [ ] run the relevant existing tests locally
- [ ] updated documentation, examples, or environment variable references
- [ ] called out any follow-up work, tradeoffs, or known limitations
- [ ] removed secrets, credentials, and private nonprofit data from code, fixtures, and screenshots

## Review process and expectations

### What authors should expect

- At least one reviewer should verify correctness, regressions, and operational impact.
- Changes affecting security, permissions, privacy/compliance, connector behavior, or deployment topology should receive especially careful review.
- Reviewers may ask for tests, docs, or clearer migration notes before approval.

### What reviewers should check

Reviewers should prioritize:

- correctness of data flow between connectors, queue workers, dashboard, and database
- safety around secrets, SMTP behavior, privacy, consent, and audit logging
- test coverage for changed behavior, especially queue mode, dashboard routes, and connector logic
- accessibility and translation impact for UI/content changes
- deployment implications for Docker Compose, Kubernetes manifests, or environment variables

### Review etiquette

- Be specific and actionable; explain the risk or expected outcome behind requested changes.
- Distinguish blocking issues from optional suggestions.
- Use pull request comments to ask questions early instead of batching surprises late in review.
- Resolve threads only when the concern is addressed or there is explicit agreement on a follow-up.

## Documentation standards

When you change contributor-facing, operator-facing, or user-facing behavior:

- update `README.md` for high-level workflows and architecture
- update the relevant file in `docs/` for deeper guidance
- include new environment variables, commands, or routes in the appropriate docs

## Testing guidance

Run the smallest relevant existing test scope first, then widen if your change crosses boundaries. Examples:

```bash
python -m unittest tests.test_funding_bot -v
python -m unittest tests.test_web_app -v
python -m unittest tests.test_celery_tasks -v
npm run test:a11y
```

Do not add new tooling solely for a contribution unless maintainers have agreed it is needed.

## Reporting security issues

Please do not open public issues or pull requests for unpatched vulnerabilities. Follow the reporting guidance in [docs/SECURITY.md](docs/SECURITY.md).
