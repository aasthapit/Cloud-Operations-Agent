"""Boot the real stack per scenario and drive the conversation through it.

This is the part that makes the harness an EVALUATION rather than a mock
check: nothing about the path a message takes is simulated. Inside one
process and one event loop it stands up

  the OpenShift MCP server   over a LiveOpenShiftBackend on the scenario's
                             FakeKube fleet - real FastMCP schemas, real
                             backend, canned sockets
  the fleet registry MCP     the PRODUCTION server, over an in-memory Mongo
                             the real seeder loaded from the scenario's
                             config plane
  the MCP gateway            its real ASGI app, learning both downstreams
                             from a copy of the committed servers.yaml
  the agent                  the real TriageOrchestrator, the real check
                             engine, the real analyst LlmAgent and toolset

and then sends the scenario's turns the way the A2A hop does: claims in
``RunConfig.custom_metadata["a2a_metadata"]["claims"]``, which is where the
orchestrator reads identity from. The only substitution is the model, and
only in fake mode.

Stack reuse: booting five services costs seconds, and suites deliberately
hold their world still across cases, so consecutive cases with the same
world, registry and inference target share one boot (see
``world.stack_key``). Each case still gets its own ADK session, and each
case's turns share theirs, because thread state - the attestation TTL cache,
a pending clarification, the prior scope - is exactly what multi-turn
scenarios exist to exercise.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from cloudops.evals.capture import TurnRecord
from cloudops.evals.suite import Case, Mode
from cloudops.evals.world import build_config_plane, build_world
from cloudops.testkit import (
    FakeFleet,
    free_port,
    seeded_registry,
    serve_asgi,
    serve_fastmcp,
    service_env,
    wait_for_gateway_tools,
)

log = structlog.get_logger("cloudops.evals")

APP_NAME = "cloudops-evals"

# The two allowlists in config/gateway/servers.yaml. The catalog reaching this
# size is the observable signal that both downstreams connected.
EXPECTED_TOOL_COUNT = 26

# The phase tick after which any gateway tool call belongs to the model: the
# orchestrator has closed its own gateway session by then.
NARRATIVE_START = "narrative:start"


@dataclass
class EvalStack:
    """A booted stack plus the recorder watching its gateway."""

    runner: Any
    recorder: Any
    config_dir: Path
    gateway_url: str


@asynccontextmanager
async def eval_stack(case: Case, mode: Mode, workdir: Path) -> AsyncIterator[EvalStack]:
    """Boot the whole backend for one scenario world, in this event loop."""
    from cloudops.evals.capture import ToolCallRecorder

    ports = {name: free_port() for name in ("ocp", "reg", "gateway")}
    config_dir = build_config_plane(case, workdir, ports["reg"])
    env = {
        "CLOUDOPS_CONFIG_DIR": str(config_dir),
        "CLOUDOPS_MCP_OPENSHIFT_PORT": str(ports["ocp"]),
        "CLOUDOPS_MCP_REGISTRY_PORT": str(ports["reg"]),
        "CLOUDOPS_GATEWAY_PORT": str(ports["gateway"]),
        "CLOUDOPS_GATEWAY_URL": f"http://127.0.0.1:{ports['gateway']}/mcp",
    }
    if mode == "fake":
        # The hermetic seam: no inference server is reachable from a CI run.
        env["CLOUDOPS_FAKE_LLM"] = "1"
    if case.inference_api_base:
        env["OLLAMA_API_BASE"] = case.inference_api_base

    with service_env(**env), seeded_registry(config_dir):
        # Imported inside the env block: these read settings when called, and
        # the servers.yaml URLs interpolate ${CLOUDOPS_MCP_*_PORT} at load.
        from google.adk.runners import Runner
        from google.adk.sessions import InMemorySessionService

        from cloudops.agent.analyst import build_analyst
        from cloudops.agent.orchestrator import TriageOrchestrator
        from cloudops.gateway.app import build_app as build_gateway_app
        from cloudops.mcp_servers.openshift.live import LiveOpenShiftBackend
        from cloudops.mcp_servers.openshift.server import build_server as build_ocp
        from cloudops.mcp_servers.registry.server import build_server as build_registry

        backend = LiveOpenShiftBackend(FakeFleet(build_world(case.fleet)))
        recorder = ToolCallRecorder(build_gateway_app())
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(serve_fastmcp(build_ocp(backend), ports["ocp"]))
            await stack.enter_async_context(serve_fastmcp(build_registry(), ports["reg"]))
            await stack.enter_async_context(serve_asgi(recorder, ports["gateway"]))
            await wait_for_gateway_tools(env["CLOUDOPS_GATEWAY_URL"], EXPECTED_TOOL_COUNT)

            runner = Runner(
                agent=TriageOrchestrator(analyst=build_analyst()),
                app_name=APP_NAME,
                session_service=InMemorySessionService(),
            )
            try:
                yield EvalStack(runner=runner, recorder=recorder, config_dir=config_dir,
                                gateway_url=env["CLOUDOPS_GATEWAY_URL"])
            finally:
                await runner.close()


async def run_case(stack: EvalStack, case: Case) -> list[TurnRecord]:
    """Send one scenario's turns through the stack and capture every event."""
    from google.adk.agents.run_config import RunConfig
    from google.genai import types

    claims = case.persona.model_dump()
    session_id = f"eval-{case.id}-{int(time.time() * 1000)}"
    await stack.runner.session_service.create_session(
        app_name=APP_NAME, user_id=case.persona.sub or "anonymous", session_id=session_id
    )

    records: list[TurnRecord] = []
    for index, turn in enumerate(case.turns):
        started = time.monotonic()
        cursor = stack.recorder.snapshot()
        narrative_started_at: float | None = None
        record = TurnRecord(index=index, user=turn.user)
        async for event in stack.runner.run_async(
            user_id=case.persona.sub or "anonymous",
            session_id=session_id,
            new_message=types.Content(role="user", parts=[types.Part(text=turn.user)]),
            run_config=RunConfig(custom_metadata={"a2a_metadata": {"claims": claims}}),
        ):
            if getattr(event, "partial", False):
                continue  # streamed chunk; the aggregated final carries the same text
            content = getattr(event, "content", None)
            if content is None or not content.parts:
                continue
            text = " ".join(p.text for p in content.parts if isinstance(p.text, str))
            if not text.strip():
                continue
            record.add(getattr(event, "author", "") or "", text)
            if narrative_started_at is None and NARRATIVE_START in record.phases():
                narrative_started_at = time.monotonic()

        record.duration_s = round(time.monotonic() - started, 3)
        record.tool_calls = stack.recorder.since(cursor)
        cutoff = narrative_started_at
        record.analyst_tool_calls = (
            [c for c in record.tool_calls if cutoff is not None and c.at >= cutoff]
        )
        log.info("evals.turn", case=case.id, turn=index, outcome=record.outcome(),
                 tool_calls=len(record.tool_calls), analyst_tool_calls=len(record.analyst_tool_calls))
        records.append(record)
    return records
