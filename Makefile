SHELL := /bin/bash
.DEFAULT_GOAL := help

EXECUTION_MODE ?= local
PYTHON ?= python
PIP ?= $(PYTHON) -m pip
NPM ?= npm
DOCKER ?= docker
COMPOSE ?= $(DOCKER) compose
IMAGE_NAME ?= funding-bot:latest
NODE_IMAGE ?= node:24-bookworm-slim
DOCS_IMAGE ?= python:3.11-slim
CONTAINER_NAME ?= funding-bot-dev
BOT_DB_PATH ?= funding_bot.db
DATA_DIR ?= data
APP_PORT ?= 5000
DOCS_SERVE_PORT ?= 8000
DOCS_BUILD_DIR ?= docs/_build
DOCKER_ENV_FILE ?= .env
COMPOSE_PROFILES ?=
PYTHON_SOURCES ?= funding_bot.py celery_app.py celery_tasks.py task_queue.py web tests
DEV_PYTHON_TOOLS ?= pre-commit ruff black isort mypy flake8
DOCKER_APP_COMMAND ?= python -m flask --app web.app run --host 0.0.0.0 --port 5000
COMPOSE_PROFILE_FLAGS := $(foreach profile,$(COMPOSE_PROFILES),--profile $(profile))

ifeq ($(EXECUTION_MODE),docker)
PYTHON_EXEC = $(DOCKER) run --rm -v "$(CURDIR)":/app -w /app -e BOT_DB_PATH=/app/$(BOT_DB_PATH) $(IMAGE_NAME) /bin/sh -lc
NODE_EXEC = $(DOCKER) run --rm -v "$(CURDIR)":/app -w /app $(NODE_IMAGE) /bin/sh -lc
DOCS_EXEC = $(DOCKER) run --rm -p $(DOCS_SERVE_PORT):$(DOCS_SERVE_PORT) -v "$(CURDIR)":/app -w /app $(DOCS_IMAGE) /bin/sh -lc
else
PYTHON_EXEC = /bin/sh -lc
NODE_EXEC = /bin/sh -lc
DOCS_EXEC = /bin/sh -lc
endif

.PHONY: help ensure-runtime setup install test lint format type-check typecheck \
	docker-build docker-run compose-up compose-down db-migrate db-reset db-seed \
	docs-build docs-serve clean

help: ## Show available targets and key variables.
	@printf 'Usage: make <target> [EXECUTION_MODE=local|docker]\n\n'
	@printf 'Key variables:\n'
	@printf '  EXECUTION_MODE  local or docker (current: %s)\n' "$(EXECUTION_MODE)"
	@printf '  BOT_DB_PATH     SQLite database path (current: %s)\n' "$(BOT_DB_PATH)"
	@printf '  IMAGE_NAME      Docker image tag (current: %s)\n' "$(IMAGE_NAME)"
	@printf '  APP_PORT        Web dashboard port (current: %s)\n' "$(APP_PORT)"
	@printf '  DOCS_SERVE_PORT Docs server port (current: %s)\n\n' "$(DOCS_SERVE_PORT)"
	@printf 'Targets:\n'
	@awk 'BEGIN {FS = ":.*## "}; /^[a-zA-Z0-9_.-]+:.*## / {printf "  %-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

ensure-runtime:
ifeq ($(EXECUTION_MODE),docker)
	@$(MAKE) --no-print-directory docker-build
else
	@:
endif

setup: install db-migrate ## Install dependencies and initialize the configured database.

install: ensure-runtime ## Install dependencies for the selected execution mode.
	@if [ "$(EXECUTION_MODE)" = "docker" ]; then \
		echo "Python dependencies are baked into $(IMAGE_NAME)."; \
	else \
		$(PYTHON_EXEC) 'python -m pip install --upgrade pip && python -m pip install -r requirements.txt -r requirements-dev.txt $(DEV_PYTHON_TOOLS)'; \
	fi
	@$(NODE_EXEC) 'if [ -f package-lock.json ]; then npm ci; elif [ -f package.json ]; then npm install; else echo "Skipping Node.js dependencies: no package.json found."; fi'

test: ensure-runtime ## Run the Python test suite.
	@$(PYTHON_EXEC) 'python -m unittest discover -s tests -q'

lint: ensure-runtime ## Run configured linters, or fall back to Python syntax checks.
	@$(PYTHON_EXEC) 'set -e; \
		if python -m ruff --version >/dev/null 2>&1; then \
			python -m ruff check $(PYTHON_SOURCES); \
		elif python -m flake8 --version >/dev/null 2>&1; then \
			python -m flake8 $(PYTHON_SOURCES); \
		else \
			python -m compileall $(PYTHON_SOURCES); \
			echo "No dedicated linter configured; ran compileall syntax checks instead."; \
		fi'

format: ensure-runtime ## Run the configured formatter when available.
	@$(PYTHON_EXEC) 'set -e; \
		if python -m ruff format --help >/dev/null 2>&1; then \
			python -m ruff format $(PYTHON_SOURCES); \
		elif python -m black --version >/dev/null 2>&1 && python -m isort --version >/dev/null 2>&1; then \
			python -m black $(PYTHON_SOURCES); \
			python -m isort --profile black --line-length 100 --filter-files $(PYTHON_SOURCES); \
		elif python -m black --version >/dev/null 2>&1; then \
			python -m black $(PYTHON_SOURCES); \
		else \
			echo "No formatter configured; nothing to format."; \
		fi'

type-check: ensure-runtime ## Run the configured type checker, or fall back to Python syntax checks.
	@$(PYTHON_EXEC) 'set -e; \
		if python -m mypy --version >/dev/null 2>&1; then \
			python -m mypy .; \
		elif command -v pyright >/dev/null 2>&1; then \
			pyright; \
		else \
			python -m compileall $(PYTHON_SOURCES); \
			echo "No dedicated type checker configured; ran compileall syntax checks instead."; \
		fi'

typecheck: type-check ## Alias for type-check.

docker-build: ## Build the application Docker image.
	@$(DOCKER) build --tag "$(IMAGE_NAME)" .

docker-run: docker-build ## Run the Flask dashboard in Docker.
	@mkdir -p "$(DATA_DIR)"
	@$(DOCKER) run --rm \
		--name "$(CONTAINER_NAME)" \
		-p "$(APP_PORT):5000" \
		$(if $(wildcard $(DOCKER_ENV_FILE)),--env-file "$(DOCKER_ENV_FILE)",) \
		-e BOT_DB_PATH="/app/data/$(notdir $(BOT_DB_PATH))" \
		-v "$(CURDIR)/$(DATA_DIR):/app/data" \
		"$(IMAGE_NAME)" /bin/sh -lc '$(DOCKER_APP_COMMAND)'

compose-up: ## Start docker compose services in detached mode.
	@$(COMPOSE) $(COMPOSE_PROFILE_FLAGS) up -d --build

compose-down: ## Stop docker compose services and remove orphaned containers.
	@$(COMPOSE) $(COMPOSE_PROFILE_FLAGS) down --remove-orphans

db-migrate: ensure-runtime ## Create or migrate the SQLite database schema.
	@$(PYTHON_EXEC) 'python -c "from funding_bot import FundingBot; bot = FundingBot(db_path=\"$(BOT_DB_PATH)\"); bot.close(); print(\"Database ready at $(BOT_DB_PATH)\")"'

db-reset: ## Remove the SQLite database and recreate an empty schema.
	@rm -f "$(BOT_DB_PATH)" "$(BOT_DB_PATH)-journal" "$(BOT_DB_PATH)-wal" "$(BOT_DB_PATH)-shm"
	@$(MAKE) --no-print-directory db-migrate EXECUTION_MODE="$(EXECUTION_MODE)" BOT_DB_PATH="$(BOT_DB_PATH)"

db-seed: db-migrate ## Seed the SQLite database with demo organization, donor, and task data.
	@$(PYTHON_EXEC) 'python -c "from funding_bot import FundingBot; bot = FundingBot(db_path=\"$(BOT_DB_PATH)\"); bot.store_organization_profile({\"name\": \"Demo Nonprofit\", \"mission\": \"Expand access to education funding.\", \"summary_recipient\": \"team@example.org\"}); bot.upsert_donor(email=\"supporter@example.org\", name=\"Sample Supporter\", segment=\"corporate\", locale=\"en\"); bot.create_task(title=\"Review seeded opportunity pipeline\", assigned_to=\"staff\", description=\"Validate the example records created by make db-seed.\", source=\"makefile\"); bot.close(); print(\"Seeded demo data into $(BOT_DB_PATH)\")"'

docs-build: ## Build a browsable documentation bundle in docs/_build.
	@rm -rf "$(DOCS_BUILD_DIR)"
	@mkdir -p "$(DOCS_BUILD_DIR)"
	@cp README.md "$(DOCS_BUILD_DIR)/README.md"
	@find docs -mindepth 1 -maxdepth 1 ! -name '_build' -exec cp -R {} "$(DOCS_BUILD_DIR)/" \;
	@{ \
		printf '%s\n' '<!doctype html>' '<html lang="en">' '<head>' '  <meta charset="utf-8">' '  <title>Funding Bot documentation</title>' '</head>' '<body>' '  <h1>Funding Bot documentation</h1>' '  <p>Generated by <code>make docs-build</code>.</p>' '  <ul>'; \
		find "$(DOCS_BUILD_DIR)" -type f ! -name 'index.html' | sed 's#^$(DOCS_BUILD_DIR)/##' | sort | while read -r file; do \
			printf '    <li><a href="%s">%s</a></li>\n' "$$file" "$$file"; \
		done; \
		printf '%s\n' '  </ul>' '</body>' '</html>'; \
	} > "$(DOCS_BUILD_DIR)/index.html"

docs-serve: docs-build ## Serve the built documentation bundle over HTTP.
	@echo "Serving $(DOCS_BUILD_DIR) at http://127.0.0.1:$(DOCS_SERVE_PORT)"
	@$(DOCS_EXEC) 'python -m http.server $(DOCS_SERVE_PORT) --directory $(DOCS_BUILD_DIR)'

clean: ## Remove generated caches, coverage output, and built docs artifacts.
	@find . -type d \( -name '__pycache__' -o -name '.pytest_cache' -o -name '.mypy_cache' -o -name 'htmlcov' \) -prune -exec rm -rf {} +
	@find . -type f \( -name '*.pyc' -o -name '*.pyo' -o -name '.coverage' -o -name 'flask.log' -o -name 'flask.pid' \) -delete
	@rm -rf "$(DOCS_BUILD_DIR)"
