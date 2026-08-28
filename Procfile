# Process manifest for `make dev` (honcho). One process per service, one Ctrl-C.
# MongoDB is NOT in here: it is a container with its own lifecycle
# (`make mongo-up`), and the registry server starts and serves honest
# "registry unavailable" results without it.
# The gateway supervises its downstream connections, so start order is not
# load-bearing; the sleeps just keep first-boot logs tidy.
ocp: sh -c 'cd backend && uv run python -m cloudops.mcp_servers.openshift'
reg: sh -c 'cd backend && uv run python -m cloudops.mcp_servers.registry'
obs: sh -c 'cd backend && uv run python -m cloudops.mcp_servers.observability'
gateway: sh -c 'sleep 2 && cd backend && uv run python -m cloudops.gateway'
agent: sh -c 'sleep 4 && cd backend && uv run python -m cloudops.agent'
bff: sh -c 'cd frontend && npm run dev:server'
web: sh -c 'cd frontend && npm run dev:web'
