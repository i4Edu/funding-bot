# Regression testing

The regression suite lives in `tests/test_regression.py` and is executed with `pytest`.
It protects previously fixed issues around:

- daily summary formatting
- credential vault backends (`EnvVarVault` and `FileVault`)
- monthly audit report edge cases (empty months and leap-year activity)
- deduplication output stability

## Install dependencies

```bash
pip install -r requirements-dev.txt
```

## Run the regression suite

```bash
python -m pytest tests/test_regression.py -q
```

## Validate snapshots

Use the snapshot helper to compare generated output with the committed snapshots:

```bash
python scripts/snapshot_tool.py validate
```

## Update snapshots

If an intentional output change is made, regenerate the stored snapshots:

```bash
python scripts/snapshot_tool.py update
```

Review the snapshot diff before committing it.

## CI

- `.github/workflows/regression-snapshots.yml` runs the regression suite and validates snapshots on pushes and pull requests.
- `.github/workflows/snapshot-update.yml` provides a manual workflow for regenerating the snapshot files and uploading them as an artifact.
