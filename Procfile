# Process manifest for `make dev` (honcho). Five processes, one Ctrl-C.
# The gateway supervises its downstream connections, so start order is not
# load-bearing; the sleeps just keep first-boot logs tidy.
ocp: sh -c 'cd backend && uv run python -m cloudops.mcp_servers.openshift'
gateway: sh -c 'sleep 2 && cd backend && uv run python -m cloudops.gateway'
agent: sh -c 'sleep 4 && cd backend && uv run python -m cloudops.agent'
bff: sh -c 'cd frontend && npm run dev:server'
web: sh -c 'cd frontend && npm run dev:web'
