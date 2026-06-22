#!/usr/bin/env bash
# onboard.sh — Nonprofit Funding Bot partner onboarding script
#
# Usage:
#   ./scripts/onboard.sh [--env-file PATH] [--db-path PATH] [--skip-docker]
#
# This script guides a new NGO partner through:
#   1. Verifying prerequisites (Docker, Python 3.11+)
#   2. Generating a .env file from .env.example if one is not already present
#   3. Prompting for required secrets (SMTP, dashboard passwords)
#   4. Optionally building and starting the Docker Compose stack
#   5. Running a smoke-test against the /health endpoint
#
# Secrets are written only to the local .env file and are never echoed to
# stdout or stored in shell history (read -s is used for sensitive inputs).

set -euo pipefail

# ── Defaults ────────────────────────────────────────────────────────────────
ENV_FILE=".env"
DB_PATH="/app/data/funding_bot.db"
SKIP_DOCKER=false
HEALTH_URL="http://localhost:5000/health"

# ── Argument parsing ─────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)   ENV_FILE="$2";   shift 2 ;;
    --db-path)    DB_PATH="$2";    shift 2 ;;
    --skip-docker) SKIP_DOCKER=true; shift ;;
    -h|--help)
      sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

# ── Helpers ──────────────────────────────────────────────────────────────────
info()    { echo "[INFO]  $*"; }
success() { echo "[OK]    $*"; }
warn()    { echo "[WARN]  $*" >&2; }
error()   { echo "[ERROR] $*" >&2; exit 1; }

prompt_secret() {
  local var_name="$1"
  local description="$2"
  local value
  read -rsp "  ${description}: " value
  echo
  echo "${var_name}=${value}" >> "$ENV_FILE"
}

prompt_value() {
  local var_name="$1"
  local description="$2"
  local default_val="${3:-}"
  local value
  read -rp "  ${description} [${default_val}]: " value
  value="${value:-$default_val}"
  echo "${var_name}=${value}" >> "$ENV_FILE"
}

# ── Step 1: Prerequisites ────────────────────────────────────────────────────
info "Checking prerequisites…"

if ! command -v python3 &>/dev/null; then
  error "Python 3 is not installed. Install Python 3.11 or later and retry."
fi

python_version=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
required_major=3
required_minor=11
actual_major=$(echo "$python_version" | cut -d. -f1)
actual_minor=$(echo "$python_version" | cut -d. -f2)

if [[ "$actual_major" -lt "$required_major" ]] || \
   { [[ "$actual_major" -eq "$required_major" ]] && [[ "$actual_minor" -lt "$required_minor" ]]; }; then
  error "Python ${required_major}.${required_minor}+ is required (found ${python_version})."
fi
success "Python ${python_version} found."

if [[ "$SKIP_DOCKER" == false ]]; then
  if ! command -v docker &>/dev/null; then
    warn "Docker not found. Use --skip-docker to set up without containers."
    SKIP_DOCKER=true
  else
    success "Docker found."
  fi
fi

# ── Step 2: Generate .env ────────────────────────────────────────────────────
info "Configuring environment…"

if [[ -f "$ENV_FILE" ]]; then
  warn "${ENV_FILE} already exists. Skipping generation (delete it to re-run setup)."
else
  if [[ ! -f ".env.example" ]]; then
    error ".env.example not found. Run this script from the repository root."
  fi
  cp ".env.example" "$ENV_FILE"
  info "Created ${ENV_FILE} from .env.example."
  echo ""
  echo "Please supply your configuration values."
  echo "Passwords will not be displayed as you type."
  echo ""

  prompt_value  "SMTP_HOST"     "SMTP server hostname"    "localhost"
  prompt_value  "SMTP_PORT"     "SMTP server port"        "587"
  prompt_value  "SMTP_USERNAME" "SMTP login username"     ""
  prompt_secret "SMTP_PASSWORD" "SMTP login password"
  prompt_value  "SMTP_FROM"     "Envelope From address"   ""
  prompt_value  "SMTP_USE_TLS"  "Enable STARTTLS (1/0)"   "1"
  prompt_value  "BOT_DB_PATH"   "SQLite database path"    "$DB_PATH"
  prompt_secret "ADMIN_PASSWORD"   "Admin dashboard password"
  prompt_secret "STAFF_PASSWORD"   "Staff dashboard password"
  prompt_secret "AUDITOR_PASSWORD" "Auditor dashboard password"
  success "${ENV_FILE} written."
fi

# ── Step 3: Python dependencies ──────────────────────────────────────────────
info "Installing Python dependencies…"
if [[ -f "web/requirements.txt" ]]; then
  python3 -m pip install --quiet -r web/requirements.txt
  success "Dependencies installed."
else
  python3 -m pip install --quiet "flask>=2.3.0"
  success "Flask installed."
fi

# ── Step 4: Run tests ────────────────────────────────────────────────────────
info "Running test suite…"
if python3 -m unittest discover -s tests -q 2>&1; then
  success "All tests passed."
else
  warn "Some tests failed — check output above before proceeding to production."
fi

# ── Step 5: Docker Compose ───────────────────────────────────────────────────
if [[ "$SKIP_DOCKER" == false ]]; then
  info "Building and starting Docker Compose stack…"
  docker compose --env-file "$ENV_FILE" up --build -d
  success "Stack started."

  info "Waiting for the health endpoint to respond…"
  for i in $(seq 1 15); do
    if curl -sf "$HEALTH_URL" | grep -q '"ok"'; then
      success "Health check passed: ${HEALTH_URL}"
      break
    fi
    sleep 2
    if [[ "$i" -eq 15 ]]; then
      warn "Health check did not respond after 30 s. Check: docker compose logs web"
    fi
  done
else
  info "Skipping Docker startup (--skip-docker set)."
  info "To start manually:  python -m flask --app web.app run"
fi

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  Nonprofit Funding Bot — onboarding complete"
echo ""
echo "  Dashboard:  http://localhost:5000/dashboard"
echo "  Health:     http://localhost:5000/health"
echo "  Metrics:    http://localhost:5000/metrics"
echo ""
echo "  Quick CLI:"
echo "    python -m funding_bot send-daily-summary --dry-run"
echo "    python -m funding_bot monthly-audit-report"
echo ""
echo "  Feedback API (staff/admin role required):"
echo "    POST http://localhost:5000/feedback"
echo "════════════════════════════════════════════════════════════════"
