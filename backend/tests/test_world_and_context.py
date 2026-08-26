"""Mock fleet (resolver, scenario faults, consistency) and context resolution."""

from pathlib import Path

import pytest

from cloudops.agent import context as ctx_resolution
from cloudops.agent.context import Claims, Clarify, Onboarding, Resolved
from cloudops.common.config import load_yaml
from cloudops.mockfleet import World

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


@pytest.fixture(scope="module")
def world() -> World:
    return World.from_config_dir(CONFIG_DIR)


@pytest.fixture(scope="module")
def registry() -> dict:
    return load_yaml(CONFIG_DIR / "fleet" / "applications.yaml")


class FakeGatewayClient:
    """Routes the two tools context resolution uses straight to the World."""

    def __init__(self, world: World) -> None:
        self.world = world

    async def call(self, tool: str, args: dict, timeout_s: float = 30.0) -> dict:
        if tool == "ocp__resolve_cluster":
            return self.world.resolve_cluster(args["query"])
        if tool == "obs__find_app_placements":
            return self.world.find_app_placements(args["app_label"])
        raise AssertionError(f"unexpected tool {tool}")


class TestFleetResolver:
    def test_fleet_is_fleet_scale(self, world):
        listed = world.list_clusters()
        assert listed["total"] == 180  # 6 explicit + 174 synthetic

    def test_exact_alias_and_fuzzy(self, world):
        assert world.resolve_cluster("pe2")["matches"][0]["name"] == "prod-east-2"
        assert world.resolve_cluster("prod-east-2")["count"] == 1
        fuzzy = world.resolve_cluster("prod-eats-2")  # typo -> close match
        assert any(m["name"] == "prod-east-2" for m in fuzzy["matches"])

    def test_label_selector(self, world):
        got = world.resolve_cluster("tier=payments")
        assert {m["name"] for m in got["matches"]} == {"prod-east-1", "prod-east-2"}

    def test_unknown_cluster_raises(self, world):
        with pytest.raises(ValueError):
            world.get_cluster_info("nope")

    def test_determinism(self):
        a = World.from_config_dir(CONFIG_DIR)
        b = World.from_config_dir(CONFIG_DIR)
        assert a.get_nodes("prod-east-2") == b.get_nodes("prod-east-2")
        assert a.get_golden_signals("prod-east-2", "payments-prod", "payments-api") == \
            b.get_golden_signals("prod-east-2", "payments-prod", "payments-api")


class TestScenarioFaults:
    def test_degraded_cluster_signals(self, world):
        ops = world.get_cluster_operators("prod-east-2")
        assert "ingress" in ops["critical_degraded"]
        pools = world.get_machine_config_pools("prod-east-2")
        assert pools["any_updating"] is True
        nodes = world.get_nodes("prod-east-2")
        assert len(nodes["cordoned"]) == 2

    def test_unattestable_cluster(self, world):
        alerts = world.get_firing_alerts("prod-eu-1")
        assert alerts["watchdog_present"] is False

    def test_both_servers_tell_one_story(self, world):
        """The observability view must agree with the OpenShift view (FR-MCP-6)."""
        workloads = world.get_workloads("prod-east-2", "payments-prod", "payments-api")
        alerts = world.get_firing_alerts("prod-east-2", "payments-prod")
        assert workloads["pods"]["crashloop"]  # ocp sees crashloops
        assert any(a["name"] == "KubePodCrashLooping" for a in alerts["critical"])  # obs agrees

    def test_healthy_sibling_instance(self, world):
        healthy = world.get_workloads("prod-east-1", "payments-prod", "payments-api")
        assert healthy["pods"]["crashloop"] == []
        assert healthy["replicas_mismatch"] == []


class TestContextResolution:
    async def _resolve(self, world, registry, claims, text, pending=None, prior=None):
        return await ctx_resolution.resolve(
            claims, text, registry, FakeGatewayClient(world), prior, pending
        )

    @pytest.mark.asyncio
    async def test_single_app_zero_questions(self, world, registry):
        claims = Claims(sub="app-developer", groups=["payments-eng"])
        outcome, pending = await self._resolve(world, registry, claims,
                                               "why is my app flaky in prod?")
        assert isinstance(outcome, Resolved)
        assert outcome.context.application == "payments-api"
        assert outcome.context.environment == "prod"
        assert set(outcome.context.clusters) == {"prod-east-1", "prod-east-2"}
        assert pending is None

    @pytest.mark.asyncio
    async def test_multi_app_asks_exactly_one_question(self, world, registry):
        claims = Claims(sub="platform-sre", groups=["retail-sre"])
        outcome, pending = await self._resolve(world, registry, claims, "is my stuff healthy?")
        assert isinstance(outcome, Clarify)
        assert len(outcome.options) == 6
        assert pending["kind"] == "application"

    @pytest.mark.asyncio
    async def test_clarification_reply_by_number(self, world, registry):
        claims = Claims(sub="platform-sre", groups=["retail-sre"])
        _, pending = await self._resolve(world, registry, claims, "is my stuff healthy?")
        outcome, _ = await self._resolve(world, registry, claims, "1", pending=pending)
        # option 1 is alphabetically first: audit-log (single env -> resolved)
        assert isinstance(outcome, Resolved)
        assert outcome.context.application == pending["options"][0]

    @pytest.mark.asyncio
    async def test_no_apps_onboarding(self, world, registry):
        claims = Claims(sub="new-joiner", groups=["newcomers"])
        outcome, _ = await self._resolve(world, registry, claims, "why is my app slow?")
        assert isinstance(outcome, Onboarding)
        assert "register" in outcome.message.lower()

    @pytest.mark.asyncio
    async def test_named_app_outside_registered_set(self, world, registry):
        claims = Claims(sub="new-joiner", groups=["newcomers"])
        outcome, _ = await self._resolve(world, registry, claims, "it's inventory-sync")
        assert isinstance(outcome, Resolved)
        assert outcome.context.application == "inventory-sync"
        assert outcome.context.outside_registered_set is True

    @pytest.mark.asyncio
    async def test_env_ambiguity_asks(self, world, registry):
        claims = Claims(sub="app-developer", groups=["payments-eng"])
        outcome, pending = await self._resolve(world, registry, claims,
                                               "how is payments-api doing?")
        assert isinstance(outcome, Clarify)
        assert set(outcome.options) == {"prod", "nonprod"}
        assert pending["kind"] == "environment"

    @pytest.mark.asyncio
    async def test_direct_cluster_attest(self, world, registry):
        claims = Claims(sub="platform-sre", groups=["retail-sre"])
        outcome, _ = await self._resolve(world, registry, claims, "attest pe2")
        assert isinstance(outcome, Resolved)
        assert outcome.context.scope == "cluster"
        assert outcome.context.clusters == ["prod-east-2"]

    @pytest.mark.asyncio
    async def test_unauthenticated_runs_nothing(self, world, registry):
        outcome, _ = await self._resolve(world, registry, Claims(), "help")
        assert isinstance(outcome, Onboarding)
