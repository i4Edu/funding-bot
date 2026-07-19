# Test Fixtures

Funding Bot now ships with shared `pytest` fixtures in `tests/conftest.py` and runs them in parallel with `pytest-xdist` by default.

## Running tests

```bash
pip install -r requirements-dev.txt
pytest
```

Useful variants:

```bash
pytest -n 0 tests/test_pytest_fixtures.py
pytest tests/test_funding_bot.py -k task
pytest --flaky-report generated/flaky-tests.json
```

`pytest.ini` enables `-n auto --dist loadscope`, so modules and unittest classes stay grouped on the same worker while independent files still run in parallel.

## Artifact and database fixtures

- `artifact_root` / `artifact_dir` create worker-safe directories under `.pytest-artifacts/`
- `db_path` gives each test its own SQLite database path
- `document_output_dir` gives each test a private document output directory
- `funding_bot` creates a `FundingBot` instance wired to the per-test database
- `db_cursor` exposes a raw SQLite cursor for low-level assertions

## Transaction fixtures

Use these when the code under test calls `commit()` internally:

- `db_transaction` yields a `DatabaseTransaction` harness with `bot`, `connection`, `rollback()`, and `blocked_commits`
- `transactional_funding_bot` is the same `FundingBot` instance wrapped in a savepoint-backed connection proxy

Example:

```python
def test_task_write_isolated(db_transaction):
    db_transaction.bot.create_task(title="Draft brief", assigned_to="staff")
    assert db_transaction.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    db_transaction.rollback()
    assert db_transaction.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
```

## Factory fixtures

- `donor_factory(**overrides)` creates and returns a donor record
- `task_factory(**overrides)` creates and returns a task record
- `connector_factory(connector_type='grants-portal', **overrides)` builds a connector, defaulting to demo transport
- `document_factory(**overrides)` generates document files and returns their paths

Pre-seeded scenario fixtures build on those factories:

- `donors`
- `tasks`
- `connectors`
- `documents`
- `organization_profile`

## Mock fixtures

- `api_mocks` patches Funding Bot's TLS HTTP session helper and records outbound API calls
- `redis_mock` provides an in-memory Redis-like client for queue tests
- `celery_task_mock` records `.delay()` and `.apply_async()` calls
- `celery_app_mock` simulates `send_task()` and worker inspection
- `service_mocks` returns all of the above in one dictionary

Example API mock:

```python
def test_connector_request(api_mocks):
    api_mocks.register_json("POST", "https://api.example.test/opps", {"rows": []})
    payload = _default_http_json_client("https://api.example.test/opps", {"keywords": ["education"]})
    assert payload == {"rows": []}
```

## Dashboard smoke fixture

`smoke_client` creates an authenticated Flask test client plus admin/staff/auditor headers for end-to-end dashboard checks.
