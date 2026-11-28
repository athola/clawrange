# ClawRange — AI MSP Testbed
# Local validation environment: OpenClaw + Workflows + DeerFlow + Ollama
#
# Quick start:
#   cp .env.example .env   # fill in OPENROUTER_API_KEY
#   make start              # bring up OpenClaw + Workflows
#   make test               # validate the stack

.DEFAULT_GOAL := help

# make does not read .env. Recipes that resolve profile ${VAR}s or call the
# bearer-protected API source it first (values are never echoed).
LOAD_ENV = if [ -f .env ]; then set -a; . ./.env; set +a; fi

# ─── Stack Lifecycle ──────────────────────────────────────────────

.PHONY: start start-full stop stop-clean restart reset

start: ## Start core stack (OpenClaw + Workflows)
	@./scripts/start.sh

start-full: ## Start full stack including DeerFlow research layer
	@./scripts/start.sh --with-deerflow

stop: ## Stop all services
	@./scripts/stop.sh

stop-clean: ## Stop all services and remove volumes
	@./scripts/stop.sh --all

restart: ## Restart core stack
	@$(MAKE) stop
	@$(MAKE) start

# ─── Production (Tailscale-secured) ──────────────────────────────

PROD_COMPOSE := -f docker-compose.yml -f docker-compose.prod.yml

.PHONY: start-prod start-prod-full stop-prod

# Same soul.md render guard as make start: a missing bind source would make
# Docker create it as a root-owned directory.
start-prod: ## Start core stack bound to Tailscale + localhost only
	@./scripts/start.sh --ensure-soul
	@docker compose $(PROD_COMPOSE) up -d --build
	@$(LOAD_ENV); \
	echo "Services bound to 127.0.0.1 + $${TAILSCALE_IP:-<TAILSCALE_IP unset>} (Tailscale)"

start-prod-full: ## Start full stack (prod mode) including DeerFlow
	@./scripts/start.sh --ensure-soul
	@docker compose $(PROD_COMPOSE) up -d --build
	@./scripts/start.sh --with-deerflow --skip-core

stop-prod: ## Stop production stack
	@docker compose $(PROD_COMPOSE) down

reset: ## Stop, drop containers and the DeerFlow clone; keeps data/ (asks first)
	@./scripts/reset.sh

# ─── Testing ──────────────────────────────────────────────────────

.PHONY: test test-openclaw test-workflows test-deerflow test-ollama test-research \
	test-unit coverage validate claude-bridge tome-bridge tome-bridge-watch \
	retrieval-eval retrieval-eval-refresh retrieval-harvest retrieval-eval-brain \
	models-check

test: ## Run full validation suite (6 tests)
	@./scripts/tests/all.sh

test-openclaw: ## Test OpenClaw layer only
	@./scripts/tests/openclaw.sh

test-workflows: ## Test workflow endpoints
	@./scripts/tests/workflows.sh

test-deerflow: ## Test DeerFlow research layer
	@./scripts/tests/deerflow.sh

test-ollama: ## Test local Ollama inference
	@./scripts/tests/ollama.sh

test-research: ## Smoke test the /research endpoint with a live topic
	@./scripts/tests/research.sh "$(TOPIC)"

# The task queue API requires a bearer token: export PROXY_AUTH_TOKEN from
# .env (never printed) and refuse to run without it. The old .env.example
# placeholder counts as unset (the API rejects it too, see workflows/auth.py).
# Only the token leaves .env: the bridge's claude session runs on untrusted
# queue text and inherits the bridge's environment, so the provider keys and
# bot token must not be in it. TOME_BRIDGE_TIMEOUT etc. come from the shell.
TOME_BRIDGE_ENV = if [ -f .env ]; then \
		tok=$$(set -a; . ./.env >/dev/null 2>&1; printf '%s' "$${PROXY_AUTH_TOKEN:-}"); \
		if [ -n "$$tok" ]; then PROXY_AUTH_TOKEN=$$tok; fi; \
	fi; \
	export PROXY_AUTH_TOKEN; \
	if [ -z "$${PROXY_AUTH_TOKEN:-}" ] || [ "$${PROXY_AUTH_TOKEN}" = "generate-a-token-here" ]; then \
		echo "PROXY_AUTH_TOKEN is empty or a placeholder: set a real token in .env (the task queue API requires it)." >&2; \
		exit 1; \
	fi

# The claude-sub bridge reads only its token and DOCKER_HOST_IP from .env;
# its claude sessions also get a fixed minimal environment (child_env).
CLAUDE_BRIDGE_ENV = if [ -f .env ]; then \
		tok=$$(set -a; . ./.env >/dev/null 2>&1; printf '%s' "$${CLAUDE_BRIDGE_TOKEN:-}"); \
		ip=$$(set -a; . ./.env >/dev/null 2>&1; printf '%s' "$${DOCKER_HOST_IP:-}"); \
		if [ -n "$$tok" ]; then CLAUDE_BRIDGE_TOKEN=$$tok; fi; \
		if [ -n "$$ip" ]; then DOCKER_HOST_IP=$$ip; fi; \
	fi; \
	export CLAUDE_BRIDGE_TOKEN DOCKER_HOST_IP; \
	if [ -z "$${CLAUDE_BRIDGE_TOKEN:-}" ]; then \
		echo "CLAUDE_BRIDGE_TOKEN is empty: set one in .env (openssl rand -hex 32)." >&2; \
		exit 1; \
	fi

claude-bridge: ## Serve the Claude subscription to the proxy (claude-sub tier)
	@$(CLAUDE_BRIDGE_ENV); python3 scripts/claude_bridge.py

tome-bridge: ## Run one bridge pass for research:tome tasks
	@$(TOME_BRIDGE_ENV); uv run python scripts/tome_bridge.py

tome-bridge-watch: ## Watch task queue for research:tome tasks
	@$(TOME_BRIDGE_ENV); uv run python scripts/tome_bridge.py --watch --interval 60

persona-export: ## Export approved persona learnings to learned.yaml (PROFILE=<name>)
	@test -n "$(PROFILE)" || { echo "usage: make persona-export PROFILE=<name>"; exit 1; }
	@$(LOAD_ENV); uv run python workflows/scripts/persona_export.py "$(PROFILE)"

# PROFILE is passed as argv, never interpolated into the Python source.
# Renders brain approvals plus learned.yaml (overlay only, with a warning,
# when data/brain/brain.db is missing).
persona: ## Render soul.md + identity.md from a profile, brain approvals and learned overlay (PROFILE=<name>)
	@test -n "$(PROFILE)" || { echo "usage: make persona PROFILE=<name>"; exit 1; }
	@$(LOAD_ENV); uv run python workflows/scripts/persona_render.py "$(PROFILE)"

persona-demo: ## Offline walkthrough of the persona meta-learning loop (PROFILE=<name>)
	@uv run python workflows/scripts/persona_demo.py "$${PROFILE:-chief-of-staff}"

# Parallel by default (pytest-xdist): every test gets its own brain and app,
# so workers share nothing. PYTEST_WORKERS=0 runs serially, e.g. to debug
# one test; a number pins the worker count.
PYTEST_WORKERS ?= auto

test-unit: ## Run Python unit tests in parallel (PYTEST_WORKERS=0 for serial)
	@uv run pytest workflows/tests/ -q -n $(PYTEST_WORKERS)

# Source and omit live in [tool.coverage.run] (pyproject.toml): workflows/
# only, tests excluded. Fails under 85%, the project's coverage target.
coverage: ## Unit tests with a source-only coverage report over workflows/
	@uv run pytest workflows/tests/ -q -n $(PYTEST_WORKERS) --cov --cov-report=term-missing:skip-covered --cov-fail-under=85

retrieval-eval: ## Compare search configs on the fixture golden set (offline)
	@cd workflows && uv run python retrieval_eval.py

retrieval-eval-refresh: ## Embed fixture cache misses via OpenRouter, then compare
	@$(LOAD_ENV); \
	cd workflows && uv run python retrieval_eval.py --refresh --check-truncation

retrieval-harvest: ## Add the assistant's real brain searches to data/retrieval/golden.json (unlabelled)
	@cd workflows && uv run python retrieval_eval.py --brain ../data/brain/brain.db --harvest

retrieval-eval-brain: ## Compare search configs on the real brain with your labelled queries
	@$(LOAD_ENV); \
	cd workflows && uv run python retrieval_eval.py --brain ../data/brain/brain.db --refresh

models-check: ## Check configured OpenRouter model ids against the live catalog
	@uv run python scripts/check_models.py

validate: ## Validate config files and project structure
	@uv run python tests/validate_stack.py
	@uv run pytest tests/ -q

# ─── Tenant Profiles (multi-tenant template) ──────────────────────

.PHONY: profile persona persona-demo seed-demo persona-export

profile: ## Render a profile's persona files (as make persona) + set it in .env (PROFILE=<name>)
	@test -n "$(PROFILE)" || { echo "usage: make profile PROFILE=<name>"; exit 1; }
	@$(LOAD_ENV); uv run python workflows/scripts/persona_render.py "$(PROFILE)"
	@if [ -f .env ]; then \
		if grep -q '^CLAWRANGE_PROFILE=' .env; then \
			sed -i.bak 's/^CLAWRANGE_PROFILE=.*/CLAWRANGE_PROFILE=$(PROFILE)/' .env && rm -f .env.bak; \
		else echo "CLAWRANGE_PROFILE=$(PROFILE)" >> .env; fi; \
		echo "set CLAWRANGE_PROFILE=$(PROFILE) in .env"; \
	else echo "no .env yet — run 'make setup' first (CLAWRANGE_PROFILE not set)"; fi

seed-demo: ## Load the lead-crm demo leads into the CRM (CRM_DB_PATH: command line, then .env, then data/crm/crm.db)
	@cli_db="$${CRM_DB_PATH:-}"; $(LOAD_ENV); \
	CRM_DB_PATH="$${cli_db:-$${CRM_DB_PATH:-data/crm/crm.db}}" uv run python scripts/seed_demo.py

# ─── Docker Inspection ────────────────────────────────────────────

.PHONY: ps logs logs-openclaw logs-workflows health

ps: ## Show running containers
	@docker compose ps 2>/dev/null; \
	if [ -f deer-flow/docker/docker-compose.yaml ]; then \
		docker ps -a --filter label=com.docker.compose.project=deer-flow; \
	fi

logs: ## Tail logs from all services
	@docker compose logs -f --tail=50

logs-openclaw: ## Tail OpenClaw logs
	@docker compose logs -f --tail=50 openclaw

logs-workflows: ## Tail Workflows logs
	@docker compose logs -f --tail=50 workflows

health: ## Quick health check (no test logic, just curl)
	@[ -f .env ] && set -a && . ./.env && set +a || true; \
	printf "OpenClaw:  "; curl -sf --connect-timeout 5 http://localhost:$${OPENCLAW_PORT:-3000}/healthz && echo " OK" || echo "DOWN"
	@[ -f .env ] && set -a && . ./.env && set +a || true; \
	printf "Workflows: "; curl -sf --connect-timeout 5 http://localhost:$${WORKFLOWS_PORT:-5678}/healthz && echo " OK" || echo "DOWN"
	@[ -f .env ] && set -a && . ./.env && set +a || true; \
	printf "DeerFlow:  "; curl -sf --connect-timeout 5 http://localhost:$${DEERFLOW_PORT:-2026}/health && echo " OK" || echo "DOWN (optional)"

# ─── Linting ─────────────────────────────────────────────────────

.PHONY: lint format format-fix typecheck py-lint

# Python tooling (ruff + mypy + pytest) is managed by uv via pyproject.toml +
# uv.lock. `uv run` resolves the synced .venv automatically, so no manual
# activation is needed and versions stay pinned to the lockfile.

py-lint: ## Lint Python with ruff (uv managed)
	@uv run ruff check .

typecheck: ## Typecheck Python with mypy (uv managed)
	@uv run mypy workflows scripts tests

# Mirrors the commit gate: the pre-commit shellcheck hook (warning severity),
# then ruff and mypy from uv.lock. A missing shellcheck is a failure, not a skip.
lint: ## Run all linters: ShellCheck (scripts) + ruff + mypy (Python)
	@command -v shellcheck >/dev/null 2>&1 || { \
		echo "ERROR: shellcheck not installed (apt install shellcheck / brew install shellcheck)" >&2; \
		exit 1; }
	@shellcheck --severity=warning scripts/*.sh scripts/tests/*.sh workflows/*.sh && echo "All scripts pass ShellCheck"
	@uv run ruff check .
	@uv run ruff format --check .
	@uv run mypy workflows scripts tests

format: ## Check formatting: YAML/JSON + ruff format (does not modify files)
	@if command -v yamllint >/dev/null 2>&1; then \
		yamllint -d relaxed docker-compose.yml deerflow/config.yaml; \
	else \
		echo "yamllint not installed (skipping YAML style check; check-yaml runs in pre-commit)"; \
	fi
	@rc=0; for f in openclaw/config/openclaw.json; do \
		if python3 -m json.tool "$$f" > /dev/null; then echo "OK: $$f"; \
		else echo "FAIL: $$f"; rc=1; fi; \
	done; exit $$rc
	@uv run ruff format --check .

format-fix: ## Apply ruff formatting + safe auto-fixes
	@uv run ruff format .
	@uv run ruff check --fix .

# ─── Setup ────────────────────────────────────────────────────────

.PHONY: setup env-check hooks

hooks: ## Install git hooks (pre-commit + pre-push) via pre-commit
	@uv run pre-commit install -t pre-commit -t pre-push
	@echo "Git hooks installed: pre-commit (ruff/mypy/format) + pre-push (pytest)."

setup: .env hooks ## One-time setup: .env + gateway token + git hooks
	@echo "Setup complete. Fill in OPENROUTER_API_KEY in .env, then run: make start"

# Single recipe line on purpose: an existing .env (even one older than
# .env.example) is never overwritten, and setup continues on to `hooks`.
.env: .env.example
	@if [ -f .env ]; then \
		echo "NOTE: .env already exists (older than .env.example?) — leaving it untouched."; \
		echo "      Diff it against .env.example for new keys; remove it to regenerate."; \
	else \
		cp .env.example .env && \
		TOKEN=$$(openssl rand -base64 32 | tr -d '/+=' | head -c 40) && \
		sed "s/^OPENCLAW_GATEWAY_TOKEN=.*/OPENCLAW_GATEWAY_TOKEN=$${TOKEN}/" .env > .env.tmp && \
		mv .env.tmp .env && \
		echo "Created .env with generated OPENCLAW_GATEWAY_TOKEN." && \
		echo "Edit .env and add your OPENROUTER_API_KEY."; \
	fi

env-check: ## Validate .env has required values
	@if [ ! -f .env ]; then echo "FAIL: .env not found. Run: make setup"; exit 1; fi
	@if grep -q '^OPENROUTER_API_KEY=$$' .env || grep -q 'your-key-here' .env; then \
		echo "WARN: OPENROUTER_API_KEY is empty or still has placeholder value"; \
	else \
		echo "OK: OPENROUTER_API_KEY is set"; \
	fi
	@if grep -q 'testbed-token-change-me' .env; then \
		echo "WARN: OPENCLAW_GATEWAY_TOKEN still has default value — change it"; \
	else \
		echo "OK: OPENCLAW_GATEWAY_TOKEN is set"; \
	fi

# ─── Cleanup ──────────────────────────────────────────────────────

.PHONY: clean-volumes

clean-volumes: ## Remove Docker volumes
	@docker compose down -v 2>/dev/null || true
	@echo "Volumes removed."

# ─── Help ─────────────────────────────────────────────────────────

.PHONY: help

help: ## Show this help
	@echo "ClawRange — AI MSP Testbed"
	@echo ""
	@echo "Usage: make <target>"
	@echo ""
	@grep -E '^[a-zA-Z0-9_-]+:.*##' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Quick start:"
	@echo "  make setup        # create .env"
	@echo "  make start        # bring up stack"
	@echo "  make test         # validate everything"
