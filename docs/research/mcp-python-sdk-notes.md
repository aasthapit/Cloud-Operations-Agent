All research verified against the installed 2.1.1 package and a working E2E gateway prototype. Here is the report.

---

# MCP Python SDK (official) - August 2026 state

**Critical headline: the SDK went 2.0 on 2026-07-28.** `FastMCP` was renamed `MCPServer`, `mcp.server.fastmcp.*` moved to `mcp.server.mcpserver.*`, the low-level `Server` was rebuilt (constructor params instead of decorators), the client was collapsed into a single `Client` class, and Pydantic fields went snake_case in Python (wire format unchanged via aliases). Everything below is verified against `mcp==2.1.1` installed locally (imports, `inspect.signature`, and a running downstream+gateway+client E2E), plus the official docs at https://py.sdk.modelcontextprotocol.io/.

## 1. Version and requirements

**Verdict: pin `mcp==2.1.1` (released 2026-08-25), Python >=3.10.** v2.0.0 shipped 2026-07-28; v1.29.1 (2026-08-24) is the v1 maintenance line. Notable deps (from PyPI metadata): `httpx2>=2.5.0` (replaces httpx), `mcp-types==2.1.1` (types split into an exact-pinned companion package), `sse-starlette>=3.0.0`, `starlette`, `uvicorn>=0.31.1`, `pydantic>=2.12`, `anyio>=4.9`, `jsonschema`, `opentelemetry-api`. Extras: `cli` (typer, dotenv), `rich`. WebSocket transport and the `ws` extra are gone.
Docs: https://pypi.org/project/mcp/ , https://py.sdk.modelcontextprotocol.io/whats-new/

## 2. High-level server (MCPServer, formerly FastMCP)

**Verdict: `from mcp.server import MCPServer` (also importable from `mcp.server.mcpserver`). Transport options moved off the constructor onto `run()` / `streamable_http_app()`.**

```python
from pydantic import BaseModel, Field
from mcp.server import MCPServer
from mcp.server.mcpserver import Context

mcp = MCPServer("Library", version="1.0.0")   # name, title, description, instructions, ... version

class BookResult(BaseModel):
    title: str
    author: str
    year: int

@mcp.tool()   # name/title/description/annotations/icons/meta/structured_output kwargs available
async def search_books(
    query: str = Field(description="Title or author to search for."),
    limit: int = Field(default=5, ge=1, le=50),
    ctx: Context = None,
) -> BookResult:
    """Search the catalog by title or author."""     # -> tool description
    await ctx.info(f"searching for {query!r}")       # signature is info(data: Any, *, logger_name=None)
    return BookResult(title=f"About {query}", author="A. Writer", year=2020)

if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="127.0.0.1", port=9101)
```

- **Schema derivation**: tool name from function name, description from docstring, `inputSchema` from type hints (JSON Schema 2020-12); `Annotated[str, Field(...)]` or `= Field(...)` defaults add descriptions/constraints, which are validated before your function runs.
- **Structured output**: the return annotation becomes `outputSchema`. Pydantic models / TypedDict / dataclasses / `dict[str, X]` map to `structuredContent` unwrapped; scalars and lists get wrapped as `{"result": ...}`. `@mcp.tool(structured_output=False)` opts out. Returning `TextContent`/`Image`/`Audio`/`EmbeddedResource` blocks no longer produces structuredContent (v2 change). A `content` text rendering is emitted alongside automatically (verified: JSON text block mirrors the structured content).
- **Context injection**: detected by the `Context` type annotation (parameter name irrelevant); never appears in the input schema. Members: `request_id`, `headers`, `session`, `request_context` (`.lifespan_context`), `await ctx.report_progress(...)`, `await ctx.read_resource(uri)`, `await ctx.elicit(...)`, and notification helpers (section 5). `ctx.info/debug/warning/error/log` exist and work but now take `data: Any` (not `message=`) and emit `MCPDeprecationWarning` - MCP logging is deprecated per SEP-2577 as of protocol 2026-07-28 (observed at runtime); prefer stdlib `logging`.
- **run() signature** (verified): `run(transport: Literal["stdio","sse","streamable-http"] = "stdio", **kwargs)`, where streamable-http kwargs are exactly `host="127.0.0.1", port=8000, streamable_http_path="/mcp", json_response=False, stateless_http=False, event_store=None, retry_interval=None, max_request_body_size=4194304 (4 MiB), transport_security=None`. Serves at `http://host:port/mcp` by default. `stateless_http=True` = fresh transport per request, no session ID (use for horizontally scaled deployments). Constructor no longer accepts host/port and `MCP_*` env vars / `.env` reading were removed.
- **Mounting in Starlette/FastAPI** (verified E2E): `mcp.streamable_http_app(...)` returns a Starlette ASGI app (same kwargs as run() minus port). When you `Mount` it inside a larger app you MUST run the session manager in your outer lifespan or the first request dies with "RuntimeError: Task group is not initialized":

```python
from contextlib import asynccontextmanager
from starlette.applications import Starlette
from starlette.routing import Mount

@asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        yield

app = Starlette(routes=[Mount("/", app=mcp.streamable_http_app())], lifespan=lifespan)
# FastAPI: identical - FastAPI(lifespan=lifespan); app.mount("/", mcp.streamable_http_app())
# Multiple servers: enter each server.session_manager.run() on one AsyncExitStack in the lifespan.
```
Docs: https://py.sdk.modelcontextprotocol.io/run/ , https://py.sdk.modelcontextprotocol.io/run/asgi/ , https://py.sdk.modelcontextprotocol.io/servers/tools/ , https://py.sdk.modelcontextprotocol.io/servers/structured-output/ , https://py.sdk.modelcontextprotocol.io/handlers/context/

## 3. Client

**Verdict: `streamablehttp_client` + `ClientSession` layering is GONE. Use `from mcp import Client`; `async with` is the whole lifecycle (connect + negotiate on enter, disconnect on exit; no separate connect()/close(), object not reusable after exit).**

```python
from mcp import Client
from mcp.types import TextContent

async with Client("http://127.0.0.1:9101/mcp") as client:   # URL | MCPServer | Server | StdioServerParameters | Transport
    client.server_info, client.server_capabilities, client.protocol_version  # populated after enter
    listed = await client.list_tools()          # kwargs: cursor=None, meta=None, cache_mode="use"|"refresh"|"bypass"
    for tool in listed.tools:
        tool.name, tool.description, tool.input_schema, tool.output_schema   # snake_case in Python
    result = await client.call_tool("search_books", {"query": "dune"})       # (name, arguments, read_timeout_seconds=None, progress_callback=None, *, input_responses=None, request_state=None, meta=None)
    result.is_error            # tool exceptions do NOT raise client-side; they come back as is_error=True
    result.structured_content  # dict matching output_schema (or None)
    for block in result.content:
        if isinstance(block, TextContent):
            print(block.text)
```

- **Custom headers/auth/timeouts** go on an `httpx2.AsyncClient` passed to the transport function (which replaced `streamablehttp_client`):

```python
import httpx2
from mcp.client.streamable_http import streamable_http_client  # (url, *, http_client=None, terminate_on_close=True)

async with httpx2.AsyncClient(headers={"Authorization": "Bearer ..."}, timeout=httpx2.Timeout(30.0, read=300.0)) as hc:
    async with Client(streamable_http_client("http://host:8000/mcp", http_client=hc)) as client:
        ...
```

- **Long-lived sessions across requests**: internally `Client.__aenter__` builds an `AsyncExitStack` holding an anyio task group, so the usual anyio rule applies - enter and exit must happen in the same task. The blessed app pattern is entering it on a `contextlib.AsyncExitStack` inside your FastAPI/Starlette lifespan (one task) and calling it from request handlers; concurrent `call_tool` from different tasks over one Client works (verified with `asyncio.gather` through my gateway). Do not enter in one request task and exit in another.
- **Reconnect handling** (verified empirically): the streamable HTTP transport auto-reconnects dropped SSE streams with `Last-Event-ID` (max 2 attempts, 1 s default delay; pair with server-side `event_store`/`retry_interval` for resumability). But if the server process dies, there is NO session re-establishment: the in-flight call fails and the Client's internal task group tears down the whole `async with` scope (surfaces as an `ExceptionGroup`). A long-lived holder must supervise: catch, discard the Client, construct and enter a fresh one. Also new in v2: non-2xx HTTP responses become per-request JSON-RPC errors; client timeouts raise code -32001; timeouts are `float` seconds (not timedelta).
- **Caching (new)**: `list_tools()` results carry `ttl_ms`/`cache_scope` and the Client caches by default; use `cache_mode="refresh"`/`"bypass"` or `Client(..., cache=None)`. `tools/list_changed` notifications evict the cache. Relevant for gateways that must always see fresh downstream catalogs.
- **Protocol negotiation**: `Client(..., mode="auto")` default sends a `server/discover` probe (protocol 2026-07-28), falling back to the classic `initialize` handshake; `mode="legacy"` forces the old handshake.
Docs: https://py.sdk.modelcontextprotocol.io/client/ , https://py.sdk.modelcontextprotocol.io/client/transports/ , https://py.sdk.modelcontextprotocol.io/client/caching/

## 4. Gateway pattern (aggregate N downstreams behind one endpoint)

**Verdict: no built-in server-side proxy/mount-of-remote feature in the official SDK. The right approach is exactly what you guessed, with v2 syntax: low-level `Server` (now `from mcp.server import Server` - `mcp.server.lowlevel` is the v1 path) re-exposing downstream `types.Tool` objects with their original `input_schema` verbatim, forwarding `call_tool` through long-lived `Client`s. The low-level Server now has `streamable_http_app()` directly (verified) - `StreamableHTTPSessionManager` still exists in `mcp.server.streamable_http_manager` but you no longer need to touch it beyond `server.session_manager.run()`.** Key v2 gotcha: handlers are constructor parameters (`on_list_tools=`, `on_call_tool=`), NOT decorators, and all handlers take `(ctx, params)`. This exact file ran successfully against a live downstream:

```python
from contextlib import AsyncExitStack, asynccontextmanager
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Mount
import mcp.types as types
from mcp import Client
from mcp.server import Server, ServerRequestContext

DOWNSTREAMS = {"library": "http://127.0.0.1:9101/mcp"}
_clients: dict[str, Client] = {}
_routes: dict[str, tuple[Client, str]] = {}
_tools: list[types.Tool] = []

async def list_tools(ctx: ServerRequestContext, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
    return types.ListToolsResult(tools=_tools)

async def call_tool(ctx: ServerRequestContext, params: types.CallToolRequestParams) -> types.CallToolResult:
    route = _routes.get(params.name)
    if route is None:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Unknown tool: {params.name}")], is_error=True)
    client, original = route
    return await client.call_tool(original, params.arguments or {})   # CallToolResult forwards verbatim

server = Server("Gateway", version="0.1.0", on_list_tools=list_tools, on_call_tool=call_tool)

async def refresh_catalog() -> None:
    _routes.clear(); _tools.clear()
    for prefix, client in _clients.items():
        for tool in (await client.list_tools()).tools:
            alias = f"{prefix}__{tool.name}"
            _routes[alias] = (client, tool.name)
            _tools.append(tool.model_copy(update={"name": alias}))   # input/output schema untouched

@asynccontextmanager
async def lifespan(app):
    async with AsyncExitStack() as stack:
        for prefix, url in DOWNSTREAMS.items():
            _clients[prefix] = await stack.enter_async_context(Client(url))
        await refresh_catalog()
        await stack.enter_async_context(server.session_manager.run())
        yield

app = Starlette(routes=[Mount("/", app=server.streamable_http_app())], lifespan=lifespan)
uvicorn.run(app, host="127.0.0.1", port=9102)
```

Verified through the gateway: downstream Field descriptions/constraints and `outputSchema` arrive byte-identical at the end client; `structured_content` and error results pass through; two concurrent calls multiplex over one downstream Client. Notes:
- "Nothing is checked for you" on the low-level Server: it advertises your schemas but does not validate incoming arguments (fine for a proxy - the downstream validates). Raising in a low-level handler becomes a JSON-RPC error, not `is_error=True` (v2 change), so catch downstream failures and return `CallToolResult(..., is_error=True)` yourself if you want model-recoverable errors.
- For production add per-downstream supervision (reconnect on the ExceptionGroup teardown described in section 3) and consider `stateless_http=True` on the gateway app for scale-out.
- Client-side alternative: `from mcp import ClientSessionGroup` aggregates several servers in one process - `ClientSessionGroup(component_name_hook=lambda name, server_info: f"{server_info.name}.{name}")`, `await group.connect_to_server(StreamableHttpParameters(url=...))` (params classes in `mcp.client.session_group`), merged `.tools`/`.resources`/`.prompts` dicts and routed `group.call_tool(...)`. It is a client aggregator only - it does not expose an MCP endpoint, so for a gateway you still front it with the low-level Server.
- **Third-party option**: jlowin's FastMCP (the `fastmcp` package, now a metapackage over `fastmcp-slim`; current `fastmcp==3.4.7`, 2026-08-10) ships a first-class proxy: `from fastmcp.server import create_proxy; proxy = create_proxy("http://example.com/mcp", name="MyProxy"); proxy.run()` - passes backend schemas and results through untouched, composable with its `mount()` for aggregation. Use it if you want proxying off the shelf; the official SDK pattern above keeps you dependency-lean. Docs: https://gofastmcp.com/servers/proxy
Docs: https://py.sdk.modelcontextprotocol.io/advanced/low-level-server/ , https://py.sdk.modelcontextprotocol.io/client/session-groups/

## 5. types module

**Verdict: protocol types live in the new exact-pinned `mcp-types` package (importable as `mcp_types`), but `import mcp.types as types` still works as an alias - keep using it. All fields are snake_case in Python with camelCase wire aliases (dump with `model_dump(by_alias=True, exclude_none=True)`).** Verified field lists:

- `types.Tool`: `name`, `title`, `description`, `input_schema` (alias `inputSchema`, `dict[str, Any]`), `output_schema` (alias `outputSchema`, optional), `execution` (ToolExecution, new), `icons`, `annotations` (ToolAnnotations: `read_only_hint`, `destructive_hint`, `idempotent_hint`, `open_world_hint`), `meta` (alias `_meta`).
- `types.TextContent`: `type: Literal["text"]`, `text: str`, `annotations`, `meta`. (`Content` alias is gone; the union is `ContentBlock`.)
- `types.CallToolResult`: `content: list[ContentBlock]`, `structured_content` (alias `structuredContent`), `is_error: bool` (alias `isError`, default False), `result_type` (alias `resultType`, `"complete" | "input_required"`, new for multi-round-trip), `meta`.
- `types.ListToolsResult`: `tools`, `next_cursor` (alias `nextCursor`), plus new cache fields `ttl_ms` (alias `ttlMs`) and `cache_scope` (alias `cacheScope`).
- `types.CallToolRequestParams`: `name`, `arguments: dict[str, Any] | None`, plus new `input_responses`, `request_state`, `task`, `meta`.
- **tools/list_changed**: from inside a handler, `await ctx.notify_tools_changed()` (plus `notify_resources_changed`, `notify_prompts_changed`, `notify_resource_updated(uri)`) - these feed the new `subscriptions/listen` streams of protocol 2026-07-28; `await ctx.session.send_tool_list_changed()` is the legacy path for pre-2026 clients (`notifications/tools/list_changed`, `types.ToolListChangedNotification`). Client side subscribes via `client.listen(tools_list_changed=True, ...)` returning a `Subscription` context manager - a gateway can use this to re-broadcast downstream catalog changes.
Docs: https://py.sdk.modelcontextprotocol.io/api/mcp_types/ , https://py.sdk.modelcontextprotocol.io/handlers/subscriptions/

## 6. Breaking changes in the last year (v1 -> v2, 2026-07-28)

The ones that bite for the above: `FastMCP` -> `MCPServer` and `mcp.server.fastmcp.*` -> `mcp.server.mcpserver.*` (`ctx.fastmcp` -> `ctx.mcp_server`); low-level Server rebuilt (decorators -> `on_*` constructor kwargs, keyword-only, `(ctx, params)` handler shape, no auto-wrapping of bare returns, exceptions -> JSON-RPC errors not isError); transport config moved from constructor/Settings to `run()`/app factories (`MCP_*` env vars and `mount_path` removed); `streamablehttp_client` -> `streamable_http_client` and transport+ClientSession layering -> single `Client` (timeouts in float seconds); `httpx` -> `httpx2` (auth subclasses `httpx2.Auth`, OS trust store via truststore); camelCase -> snake_case Python field names; `mcp.types` -> `mcp-types` package (alias kept); `McpError` -> `MCPError` (raising it in a tool now yields a top-level JSON-RPC error, not isError); sync handlers now run on worker threads (`anyio.to_thread`); tool handler exceptions are masked to clients as "Error executing tool <name>"; 4 MiB request body cap (HTTP 413); WebSocket transport removed; protocol 2026-07-28 adds `server/discover` single-round-trip init, `subscriptions/listen`, multi-round-trip `InputRequiredResult`, and deprecates server-initiated sampling/roots/MCP-logging (SEP-2577, runtime `MCPDeprecationWarning`); auth additions (RFC 9207 issuer validation, SEP-990 identity assertion, client-credentials providers, `PrivateKeyJWTOAuthProvider` replacing RFC7523 provider). Full list: https://py.sdk.modelcontextprotocol.io/migration/

## Pinned versions

```
mcp==2.1.1            # official SDK (mcp-types==2.1.1 comes pinned with it), Python >=3.10
# only if you want off-the-shelf proxying instead of the low-level pattern:
fastmcp==3.4.7        # third-party (jlowin), create_proxy / mount
# if migration is not possible right now: mcp>=1.29.1,<2 keeps the v1 API
```

Working reference files from the verified E2E run (downstream MCPServer, low-level gateway, end client): `/private/tmp/claude-501/-Users-malnec-headless-agent-manager-ui/a867af66-0b38-428f-bb3a-2682691050ce/scratchpad/downstream.py`, `.../gateway.py`, `.../end_client.py`.

Sources: [PyPI mcp](https://pypi.org/project/mcp/), [SDK docs home](https://py.sdk.modelcontextprotocol.io/), [Migration guide](https://py.sdk.modelcontextprotocol.io/migration/), [Running](https://py.sdk.modelcontextprotocol.io/run/), [ASGI mounting](https://py.sdk.modelcontextprotocol.io/run/asgi/), [Tools](https://py.sdk.modelcontextprotocol.io/servers/tools/), [Structured output](https://py.sdk.modelcontextprotocol.io/servers/structured-output/), [Context](https://py.sdk.modelcontextprotocol.io/handlers/context/), [Subscriptions](https://py.sdk.modelcontextprotocol.io/handlers/subscriptions/), [Client](https://py.sdk.modelcontextprotocol.io/client/), [Client transports](https://py.sdk.modelcontextprotocol.io/client/transports/), [Session groups](https://py.sdk.modelcontextprotocol.io/client/session-groups/), [Caching](https://py.sdk.modelcontextprotocol.io/client/caching/), [Low-level Server](https://py.sdk.modelcontextprotocol.io/advanced/low-level-server/), [Protocol versions](https://py.sdk.modelcontextprotocol.io/protocol-versions/), [GitHub releases](https://github.com/modelcontextprotocol/python-sdk/releases), [FastMCP proxy](https://gofastmcp.com/servers/proxy).