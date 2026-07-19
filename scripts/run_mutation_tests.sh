#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python -m pytest \
  tests/test_web_app.py \
  tests/test_celery_tasks.py \
  tests/test_collaboration.py \
  -q

if [ "$#" -gt 0 ]; then
  mutmut run --max-children "${MUTMUT_MAX_CHILDREN:-2}" "$@"
else
  mutmut run --max-children "${MUTMUT_MAX_CHILDREN:-2}"
fi

mutmut export-cicd-stats
