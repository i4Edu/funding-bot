#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

backup_config=".pytest.ini.pre-mutmut.bak"

restore_pytest_config() {
  if [ -f "$backup_config" ]; then
    mv "$backup_config" pytest.ini
  fi
}

trap restore_pytest_config EXIT

cp pytest.ini "$backup_config"
cp pytest.mutmut.ini pytest.ini

python -m pytest \
  -c pytest.mutmut.ini \
  tests/test_mutation_baseline.py \
  -q

if [ "$#" -gt 0 ]; then
  mutmut run --max-children "${MUTMUT_MAX_CHILDREN:-2}" "$@"
else
  mutmut run --max-children "${MUTMUT_MAX_CHILDREN:-2}"
fi

mutmut results --all true || true
