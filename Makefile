# ClawRange — AI MSP Testbed
# Local validation environment: OpenClaw + n8n + DeerFlow + Ollama
#
# Quick start:
#   cp .env.example .env   # fill in OPENROUTER_API_KEY
#   make start              # bring up OpenClaw + n8n
#   make test               # validate the stack

.DEFAULT_GOAL := help

# ─── Stack Lifecycle ──────────────────────────────────────────────

.PHONY: start start-full stop stop-clean restart reset

start: ## Start core stack (OpenClaw + n8n)
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

reset: ## Wipe all data and start fresh (interactive confirmation)
	@./scripts/reset.sh

# ─── Testing ──────────────────────────────────────────────────────

.PHONY: test test-openclaw test-n8n test-deerflow test-ollama validate

test: ## Run full validation suite (6 tests)
	@./scripts/test_all.sh

test-openclaw: ## Test OpenClaw layer only
	@./scripts/test_openclaw.sh

test-n8n: ## Test n8n workflows only
	@./scripts/test_n8n.sh

test-deerflow: ## Test DeerFlow research layer
	@./scripts/test_deerflow.sh

test-ollama: ## Test local Ollama inference
	@./scripts/test_ollama.sh

validate: ## Validate config files and project structure
	@python3 tests/validate_stack.py
	@if command -v pytest >/dev/null 2>&1; then \
		python3 -m pytest tests/test_validate_stack.py -q; \
	fi

# ─── Docker Inspection ────────────────────────────────────────────

.PHONY: ps logs logs-openclaw logs-n8n health

ps: ## Show running containers
	@docker compose ps 2>/dev/null; \
	if [ -d deerflow ]; then \
		(cd deer-flow 2>/dev/null && COMPOSE_FILE=docker/docker-compose.yaml docker compose ps 2>/dev/null) || true; \
	fi

logs: ## Tail logs from all services
	@docker compose logs -f --tail=50

logs-openclaw: ## Tail OpenClaw logs
	@docker compose logs -f --tail=50 openclaw

logs-n8n: ## Tail n8n logs
	@docker compose logs -f --tail=50 n8n

health: ## Quick health check (no test logic, just curl)
	@[ -f .env ] && set -a && . ./.env && set +a || true; \
	printf "OpenClaw: "; curl -sf --connect-timeout 5 http://localhost:$${OPENCLAW_PORT:-3000}/healthz && echo "OK" || echo "DOWN"
	@[ -f .env ] && set -a && . ./.env && set +a || true; \
	printf "n8n:      "; curl -sf --connect-timeout 5 http://localhost:$${N8N_PORT:-5678}/healthz && echo "OK" || echo "DOWN"
	@[ -f .env ] && set -a && . ./.env && set +a || true; \
	printf "DeerFlow: "; curl -sf --connect-timeout 5 http://localhost:$${DEERFLOW_PORT:-2026}/api/health && echo "OK" || echo "DOWN (optional)"

# ─── Linting ─────────────────────────────────────────────────────

.PHONY: lint format

lint: ## Run ShellCheck on scripts (install: apt install shellcheck)
	@if command -v shellcheck >/dev/null 2>&1; then \
		shellcheck scripts/*.sh && echo "All scripts pass ShellCheck"; \
	else \
		echo "shellcheck not installed (skipping — install with: apt install shellcheck)"; \
	fi

format: ## Check YAML/JSON formatting
	@command -v yamllint >/dev/null 2>&1 && yamllint -d relaxed docker-compose.yml deerflow/config.yaml || echo "yamllint not installed (skipping)"
	@for f in n8n/workflows/*.json openclaw/config/openclaw.json; do \
		python3 -m json.tool "$$f" > /dev/null && echo "OK: $$f" || echo "FAIL: $$f"; \
	done

# ─── Setup ────────────────────────────────────────────────────────

.PHONY: setup env-check

setup: .env ## One-time setup: create .env and generate encryption key
	@echo "Setup complete. Fill in OPENROUTER_API_KEY in .env, then run: make start"

.env: .env.example
	@if [ -f .env ]; then echo ".env already exists. Remove it first to regenerate."; exit 1; fi
	@cp .env.example .env
	@KEY=$$(openssl rand -hex 32) && \
		sed "s/^N8N_ENCRYPTION_KEY=.*/N8N_ENCRYPTION_KEY=$${KEY}/" .env > .env.tmp && \
		mv .env.tmp .env
	@echo "Created .env with generated N8N_ENCRYPTION_KEY."
	@echo "Edit .env and add your OPENROUTER_API_KEY."

env-check: ## Validate .env has required values
	@if [ ! -f .env ]; then echo "FAIL: .env not found. Run: make setup"; exit 1; fi
	@if grep -q '^OPENROUTER_API_KEY=$$' .env || grep -q 'your-key-here' .env; then \
		echo "WARN: OPENROUTER_API_KEY is empty or still has placeholder value"; \
	else \
		echo "OK: OPENROUTER_API_KEY is set"; \
	fi
	@if grep -q '^N8N_ENCRYPTION_KEY=$$' .env || grep -q 'replace-with' .env; then \
		echo "WARN: N8N_ENCRYPTION_KEY is empty or still has placeholder value"; \
	else \
		echo "OK: N8N_ENCRYPTION_KEY is set"; \
	fi
	@if grep -q 'testbed-token-change-me' .env; then \
		echo "WARN: OPENCLAW_GATEWAY_TOKEN still has default value — change it"; \
	else \
		echo "OK: OPENCLAW_GATEWAY_TOKEN is set"; \
	fi

# ─── Cleanup ──────────────────────────────────────────────────────

.PHONY: clean-volumes

clean-volumes: ## Remove Docker volumes (n8n data)
	@docker compose down -v 2>/dev/null || true
	@echo "Volumes removed."

# ─── Help ─────────────────────────────────────────────────────────

.PHONY: help

help: ## Show this help
	@echo "ClawRange — AI MSP Testbed"
	@echo ""
	@echo "Usage: make <target>"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Quick start:"
	@echo "  make setup        # create .env"
	@echo "  make start        # bring up stack"
	@echo "  make test         # validate everything"
