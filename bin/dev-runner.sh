#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "$ROOT_DIR"

usage() {
  cat <<'EOF'
Usage: ./bin/dev-runner.sh <setup|run|format|lint|test|help>

Commands:
  setup   Install Python development dependencies and optional Node dependencies.
  run     Start the local Flask development server with hot reload.
  format  Apply ruff fixes, sort imports with isort, and format code with black.
  lint    Check ruff, import ordering, and black formatting without modifying files.
  test    Run the unittest suite and quick smoke tests.
  help    Show this help message.
EOF
}

setup_environment() {
  "$PYTHON_BIN" -m pip install --upgrade pip
  "$PYTHON_BIN" -m pip install -r requirements-dev.txt

  if command -v npm >/dev/null 2>&1 && [[ -f package-lock.json ]]; then
    npm install
  else
    echo "Skipping Node dependency install (npm or package-lock.json not available)."
  fi
}

run_server() {
  "$PYTHON_BIN" scripts/run_dev_web.py
}

format_code() {
  "$PYTHON_BIN" -m ruff check . --fix
  "$PYTHON_BIN" -m isort .
  "$PYTHON_BIN" -m black .
}

lint_code() {
  "$PYTHON_BIN" -m ruff check .
  "$PYTHON_BIN" -m isort . --check-only
  "$PYTHON_BIN" -m black . --check
}

run_tests() {
  "$PYTHON_BIN" -m unittest discover -s tests
  "$PYTHON_BIN" -m pytest tests/test_smoke.py -m quick -q --maxfail=1
}

case "${1:-help}" in
  setup)
    setup_environment
    ;;
  run)
    run_server
    ;;
  format)
    format_code
    ;;
  lint)
    lint_code
    ;;
  test)
    run_tests
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac
