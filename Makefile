# Cloud Operations Agent - developer entry points.
# Acceptance criterion 1: `make setup && make dev` brings up the full stack
# with only Ollama as a prerequisite (https://ollama.com, `ollama serve`,
# and a tool-capable model: `ollama pull qwen3:4b`).

.PHONY: setup dev check test lint typecheck telemetry-up telemetry-down live-prep live-smoke \
	mongo-up mongo-down mongo-seed run-ocp-mcp run-registry-mcp

setup:  ## install backend (uv) and frontend (npm) dependencies
	cd backend && uv sync
	cd frontend && npm install --no-audit --no-fund

dev:  ## run all six services with hot-reloading config (Ctrl-C stops all)
	uv run --project backend honcho start

check: lint typecheck test  ## acceptance criterion 8

test:
	cd backend && uv run pytest -q

lint:
	cd backend && uv run ruff check src tests

typecheck:
	cd backend && uv run mypy src
	cd frontend && npm run check

live-prep:  ## prepare the six local kind clusters for live mode (idempotent)
	./deploy/live/live-prep.sh

live-smoke:  ## exercise both live backends against the running kind fleet (binds no ports)
	cd backend && CLOUDOPS_BACKEND_MODE=live uv run python -m cloudops.mcp_servers.live_smoke

mongo-up:  ## start the fleet registry's MongoDB on 127.0.0.1:27017
	cd deploy/mongo && docker compose up -d

mongo-down:  ## stop MongoDB (the named volume, and so the seeded data, survives)
	cd deploy/mongo && docker compose down

mongo-seed:  ## load config/fleet/*.yaml into MongoDB (idempotent upserts)
	cd backend && uv run python -m cloudops.registry.seed

run-ocp-mcp:  ## run the OpenShift MCP server standalone on :8011
	cd backend && uv run python -m cloudops.mcp_servers.openshift

run-registry-mcp:  ## run the fleet registry MCP server standalone on :8013
	cd backend && uv run python -m cloudops.mcp_servers.registry

telemetry-up:  ## optional: OTLP collector + Jaeger UI on :16686
	cd deploy && docker compose up -d

telemetry-down:
	cd deploy && docker compose down
