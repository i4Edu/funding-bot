# Testing

## Local unit tests

Run the focused connector suite:

```bash
python -m unittest tests.test_connector_coverage -v
```

Run the accessibility checks:

```bash
python -m unittest tests.test_accessibility_templates -q
npm run test:a11y
```

## Coverage

Generate console, HTML, and JSON coverage reports:

```bash
python -m coverage run --rcfile=.coveragerc -m unittest tests.test_connector_coverage
python -m coverage report -m
python -m coverage html
python -m coverage json -o coverage.json
```

The HTML report is written to `htmlcov/index.html`.

## Connector coverage

The connector subsystem lives in `funding_bot.py` beside unrelated application code,
so CI enforces a focused connector coverage gate instead of a whole-file threshold.

The gate measures executable lines for:

- `GrantsPortalConnector`
- `CSRNetworkConnector`
- `NGODirectoryConnector`
- `FoundationDirectoryConnector`
- `CrowdfundingConnector`
- `GlobalGivingConnector`
- `KickstarterForGoodConnector`
- `ConnectorRegistry`
- `default_connectors()`
- `connector_registry()`
- `create_connector()`

CI runs:

```bash
python scripts/check_connector_coverage.py coverage.json 90
```

That command fails the build if connector coverage drops below 90%.

## Strategy

- keep shared coverage defaults in `.coveragerc`
- exercise connector parsing, rate limiting, retries, credential handling, and registry branches in `tests/test_connector_coverage.py`
- publish `htmlcov/` as a CI artifact for inspection
- keep accessibility and connector coverage jobs separate for easier triage
