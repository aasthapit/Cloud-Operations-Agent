"""Headless end-to-end triage (NFR-QE-1, gap G7).

The whole backend chain runs inside this test's event loop on kernel-assigned
ports: the OpenShift MCP server, a registry MCP server serving the reg__*
contract, the gateway, the deterministic orchestrator, the check engine, and
the analyst - the last wired to the fake model so the turn is hermetic and
finishes in milliseconds instead of the 15-60 s a local narrative takes. No
cluster, no Mongo, no Ollama, no Docker, no dev ports, no network.

Two seams make that possible and neither is test-only plumbing bolted onto
production code:

- ``build_server(backend=...)`` on the OpenShift MCP takes a pre-built
  backend, so the harness hands it a LiveOpenShiftBackend over FakeFleet.
  Every layer above it - FastMCP schemas, the gateway, the tool loop - is the
  shipped one; only the socket to the cluster is canned.
- the gateway learns its downstreams from config/gateway/servers.yaml, so the
  registry server is registered through a copy of the real config plane
  rather than through a code hook.

What it pins is the contract the console consumes: the fenced payloads on the
event stream (see cloudops.agent.protocol) for the two flows that carry the
most product risk.

  F1 zero-question triage: context resolves payments-api/prod from claims
     alone, the registry's candidate is verified live, the in-scope cluster is
     attested, an 18-section Application 360 report lands, and the analyst
     narrates.
  F2 clarification: an ambiguous SRE question yields exactly one question
     and runs no checks at all.

Claims are seeded the way the A2A path seeds them - RunConfig.custom_metadata
["a2a_metadata"]["claims"], which is where the orchestrator reads them from -
so this exercises the production identity seam rather than a test-only one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from typing import Any

import pytest
import pytest_asyncio
from fakes import HEALTHY, FakeFleet, build_registry_server, default_world
from google.adk.agents.run_config import RunConfig
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from harness import (  # tests/ is on sys.path under pytest's default import mode
    config_dir_with_registry,
    free_port,
    mcp_client,
    serve_asgi,
    serve_fastmcp,
    service_env,
    wait_for_gateway_tools,
)
from registry_fixtures import seeded_registry  # noqa: F401 - fixture import

from cloudops.agent.model_factory import FAKE_NARRATIVE_PREFIX
from cloudops.agent.protocol import extract_fences

# LiveFleet resolves cluster records through the MongoDB registry since the
# live cutover, so the fleet doubles in this module need the seeded mongomock
# registry standing behind them.
pytestmark = pytest.mark.usefixtures("seeded_registry")

APP_NAME = "cloudops-e2e"

# The two allowlists in config/gateway/servers.yaml; the catalog reaching this
# size is the signal that both downstreams connected.
EXPECTED_TOOL_COUNT = 26

CLAIMS = {
    "app_developer": {
        "sub": "app-developer", "name": "App developer",
        "email": "app.developer@example.internal", "groups": ["payments-eng"],
    },
    "platform_sre": {
        "sub": "platform-sre", "name": "Platform SRE",
        "email": "platform.sre@example.internal", "groups": ["retail-sre"],
    },
}


class Turn:
    """The fenced payloads and narrative text yielded by one turn."""

    def __init__(self, texts: list[str]) -> None:
        self.texts = texts
        self.narrative = ""
        self.fences: list[tuple[str, Any]] = []
        for text in texts:
            stripped, found = extract_fences(text)
            self.fences.extend(found)
            if stripped:
                self.narrative += stripped + "\n"

    def of(self, kind: str) -> list[Any]:
        return [payload for k, payload in self.fences if k == kind]

    def one(self, kind: str) -> Any:
        payloads = self.of(kind)
        assert len(payloads) == 1, f"expected exactly one {kind} fence, got {len(payloads)}"
        return payloads[0]


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def stack(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Runner]:
    """Boot the backend in-process and hand back a Runner over the orchestrator.

    Module-scoped because the boot is the expensive part; every case gets its
    own ADK session, so turns stay isolated from each other.
    """
    ports = {name: free_port() for name in ("ocp", "reg", "gateway")}
    config_dir = config_dir_with_registry(tmp_path_factory.mktemp("plane"), ports["reg"])
    monkeypatch = pytest.MonkeyPatch()
    with monkeypatch.context() as mp, service_env(
        mp,
        CLOUDOPS_CONFIG_DIR=str(config_dir),
        CLOUDOPS_MCP_OPENSHIFT_PORT=str(ports["ocp"]),
        CLOUDOPS_GATEWAY_PORT=str(ports["gateway"]),
        CLOUDOPS_GATEWAY_URL=f"http://127.0.0.1:{ports['gateway']}/mcp",
        # The hermetic seam: no inference server is reachable from a test.
        CLOUDOPS_FAKE_LLM="1",
    ):
        # Imported inside the env block: these read settings when called, and
        # the servers.yaml URLs interpolate ${CLOUDOPS_MCP_*_PORT} at load.
        from cloudops.agent.analyst import build_analyst
        from cloudops.agent.orchestrator import TriageOrchestrator
        from cloudops.gateway.app import build_app as build_gateway_app
        from cloudops.mcp_servers.openshift.live import LiveOpenShiftBackend
        from cloudops.mcp_servers.openshift.server import build_server as build_ocp

        backend = LiveOpenShiftBackend(FakeFleet(default_world()))
        async with AsyncExitStack() as stack_ctx:
            await stack_ctx.enter_async_context(
                serve_fastmcp(build_ocp(backend), ports["ocp"]))
            await stack_ctx.enter_async_context(
                serve_fastmcp(build_registry_server(ports["reg"]), ports["reg"]))
            await stack_ctx.enter_async_context(serve_asgi(build_gateway_app(), ports["gateway"]))
            await wait_for_gateway_tools(
                f"http://127.0.0.1:{ports['gateway']}/mcp", EXPECTED_TOOL_COUNT
            )

            runner = Runner(
                agent=TriageOrchestrator(analyst=build_analyst()),
                app_name=APP_NAME,
                session_service=InMemorySessionService(),
            )
            try:
                yield runner
            finally:
                await runner.close()


async def run_turn(runner: Runner, session_id: str, claims: dict[str, Any], text: str) -> Turn:
    """One user message through the orchestrator, claims seeded A2A-style."""
    from google.genai import types

    await runner.session_service.create_session(
        app_name=APP_NAME, user_id=claims["sub"], session_id=session_id
    )
    texts: list[str] = []
    async for event in runner.run_async(
        user_id=claims["sub"],
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text=text)]),
        run_config=RunConfig(custom_metadata={"a2a_metadata": {"claims": claims}}),
    ):
        content = getattr(event, "content", None)
        if content is None or not content.parts:
            continue
        joined = " ".join(p.text for p in content.parts if isinstance(p.text, str))
        if joined.strip():
            texts.append(joined)
    return Turn(texts)


@pytest.mark.asyncio(loop_scope="module")
async def test_zero_question_triage(stack: Runner) -> None:
    """F1: claims alone resolve the app, its placement is verified live, the
    host cluster attests, and the report lands."""
    turn = await run_turn(
        stack, "e2e-f1", CLAIMS["app_developer"], "why is my app flaky in prod?"
    )

    context = turn.one("context")
    assert context["application"] == "payments-api"
    assert context["environment"] == "prod"
    assert context["clusters"] == [HEALTHY]
    # FR-CTX-2: the instance carries what the cluster answered, not what the
    # registry claimed.
    assert context["instances"][0]["verified"] is True
    assert context["instances"][0]["pod_count"] == 2

    attestation = turn.one("attestation")
    assert {c["cluster"]: c["verdict"] for c in attestation["clusters"]} == {HEALTHY: "healthy"}

    reports = turn.of("app360")
    assert reports, "the first resolution must produce an Application 360 report"
    assert all(len(r["sections"]) == 18 for r in reports)
    assert {r["cluster"] for r in reports} == {HEALTHY}

    # The deterministic phases hand off to the analyst, which narrates.
    assert FAKE_NARRATIVE_PREFIX in turn.narrative


@pytest.mark.asyncio(loop_scope="module")
async def test_vague_question_assumes_the_default_environment(stack: Runner) -> None:
    """FR-CTX-8: a sentence naming neither app nor environment still runs the
    whole pipeline; the assumption rides on the context fence, not on a question."""
    turn = await run_turn(stack, "e2e-f1-vague", CLAIMS["app_developer"], "is my app down")

    context = turn.one("context")
    assert context["application"] == "payments-api"
    assert context["environment"] == "prod"
    assert context["environment_assumed"] is True

    assert turn.of("clarify") == []
    assert turn.one("attestation")["clusters"]
    assert {r["cluster"] for r in turn.of("app360")} == {HEALTHY}


@pytest.mark.asyncio(loop_scope="module")
async def test_degraded_instance_reports_its_findings(stack: Runner) -> None:
    """The nonprod instance is genuinely broken in the canned world: the report
    must carry the crashloop and the autoscaling findings that replaced the
    retired Prometheus checks."""
    turn = await run_turn(
        stack, "e2e-nonprod", CLAIMS["app_developer"], "how is payments-api in nonprod?"
    )

    [report] = turn.of("app360")
    assert report["overall_status"] == "critical"
    findings = {
        check["id"]: check["status"]
        for section in report["sections"] for check in section["checks"]
    }
    assert findings["crashloops"] == "fail"
    assert findings["hpa-maxed"] == "warn"
    assert findings["pdb-disruptions"] == "warn"
    # Section 9 no longer runs metric checks; it states the absence instead.
    observability = next(s for s in report["sections"] if s["section"] == 9)
    assert observability["checks"] == []
    assert any("not collected" in item for item in observability["manual_items"])


@pytest.mark.asyncio(loop_scope="module")
async def test_ambiguous_question_asks_once_and_runs_nothing(stack: Runner) -> None:
    """F2: exactly one question, and the check phases never start (FR-CTX-4)."""
    turn = await run_turn(stack, "e2e-f2", CLAIMS["platform_sre"], "is my stuff healthy?")

    clarify = turn.one("clarify")
    assert clarify["kind"] == "application"
    assert len(clarify["options"]) == 6
    assert clarify["question"].strip()

    assert turn.of("context") == []
    assert turn.of("attestation") == []
    assert turn.of("app360") == []
    assert turn.of("phase") == []


@pytest.mark.asyncio(loop_scope="module")
async def test_gateway_namespaces_both_domains(stack: Runner) -> None:
    """The catalog the analyst sees is namespaced per domain (FR-GW-2)."""
    from cloudops.common.settings import get_settings

    async with mcp_client(get_settings().cloudops_gateway_url) as session:
        names = {t.name for t in (await session.list_tools()).tools}
    assert "ocp__resolve_cluster" in names
    assert "ocp__verify_placement" in names
    assert "reg__find_placements" in names
    assert "reg__blast_radius" in names
    assert all(name.startswith(("ocp__", "reg__")) for name in names)
    # The observability domain is gone, allowlist and all.
    assert not any(name.startswith("obs__") for name in names)
