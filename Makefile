# Cloud Operations Agent - developer entry points.
# Acceptance criterion 1: `make setup && make dev` brings up the full stack
# with only Ollama as a prerequisite (https://ollama.com, `ollama serve`,
# and a tool-capable model: `ollama pull qwen3:4b`).

.PHONY: setup dev check test lint typecheck telemetry-up telemetry-down live-prep live-smoke

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

telemetry-up:  ## optional: OTLP collector + Jaeger UI on :16686
	cd deploy && docker compose up -d

telemetry-down:
	cd deploy && docker compose down
