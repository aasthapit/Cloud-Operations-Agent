"""Mock fleet (resolver, scenario faults, consistency) and context resolution."""

import pytest
from conftest import CONFIG_DIR, WorldGateway

from cloudops.agent import context as ctx_resolution
from cloudops.agent.context import Claims, Clarify, Onboarding, Resolved
from cloudops.agent.models import AppInstance, ResolvedContext
from cloudops.mockfleet import World


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


# What the orchestrator passes every turn, from config/models.yaml agent.* .
DEFAULT_POLICY = {"default_environment": "prod"}
STRICT_POLICY = {"default_environment": None}


def _prior_payments_prod() -> ResolvedContext:
    """A thread that already resolved payments-api in prod (the F1 baseline)."""
    return ResolvedContext(
        scope="app", user_sub="app-developer", user_name="App Developer",
        groups=["payments-eng"], application="payments-api", app_label="payments-api",
        environment="prod",
        instances=[
            AppInstance(cluster="prod-east-1", namespace="payments-prod", environment="prod"),
            AppInstance(cluster="prod-east-2", namespace="payments-prod", environment="prod"),
        ],
        clusters=["prod-east-1", "prod-east-2"],
    )


class TestContextResolution:
    async def _resolve(self, world, registry, claims, text, pending=None, prior=None,
                       policy=DEFAULT_POLICY):
        return await ctx_resolution.resolve(
            claims, text, registry, WorldGateway(world), prior, pending, policy
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
    async def test_env_ambiguity_assumes_the_default(self, world, registry):
        """FR-CTX-8: payments-api runs in both environments, so the old
        behavior asked; with a default configured the turn resolves to prod
        and says so instead of spending the one question."""
        claims = Claims(sub="app-developer", groups=["payments-eng"])
        outcome, pending = await self._resolve(world, registry, claims,
                                               "how is payments-api doing?")
        assert isinstance(outcome, Resolved)
        assert outcome.context.environment == "prod"
        assert outcome.context.environment_assumed is True
        assert pending is None

    @pytest.mark.asyncio
    async def test_env_ambiguity_asks_under_strict_policy(self, world, registry):
        """FR-CTX-8: `default_environment: null` restores strict clarification."""
        claims = Claims(sub="app-developer", groups=["payments-eng"])
        outcome, pending = await self._resolve(world, registry, claims,
                                               "how is payments-api doing?",
                                               policy=STRICT_POLICY)
        assert isinstance(outcome, Clarify)
        assert set(outcome.options) == {"prod", "nonprod"}
        assert pending["kind"] == "environment"

    @pytest.mark.asyncio
    async def test_vague_question_runs_the_full_pipeline(self, world, registry):
        """F1: 'is my app down' carries neither app nor environment, and still
        resolves with zero questions (FR-CTX-8)."""
        claims = Claims(sub="app-developer", groups=["payments-eng"])
        outcome, pending = await self._resolve(world, registry, claims, "is my app down")
        assert isinstance(outcome, Resolved)
        assert outcome.context.scope == "app"
        assert outcome.context.application == "payments-api"
        assert outcome.context.environment == "prod"
        assert outcome.context.environment_assumed is True
        assert pending is None

    @pytest.mark.asyncio
    async def test_single_environment_app_is_not_marked_assumed(self, world, registry):
        """inventory-sync runs only in nonprod: one placement is a fact, not an
        assumption, and the default never enters into it."""
        claims = Claims(sub="logistics-dev", groups=["logistics-eng"])
        outcome, pending = await self._resolve(world, registry, claims, "how is my app?")
        assert isinstance(outcome, Resolved)
        assert outcome.context.application == "inventory-sync"
        assert outcome.context.environment == "nonprod"
        assert outcome.context.environment_assumed is False
        assert pending is None

    @pytest.mark.asyncio
    async def test_stated_environment_is_not_marked_assumed(self, world, registry):
        """An environment the user named is not an assumption either."""
        claims = Claims(sub="app-developer", groups=["payments-eng"])
        outcome, _ = await self._resolve(world, registry, claims,
                                         "how is payments-api doing in nonprod?")
        assert isinstance(outcome, Resolved)
        assert outcome.context.environment == "nonprod"
        assert outcome.context.environment_assumed is False

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

    @pytest.mark.asyncio
    async def test_attest_with_trailing_punctuation(self, world, registry):
        """Regression: 'attest prod-east-2.' captured the dot, fuzzy-matched
        several clusters, and asked to clarify instead of resolving."""
        claims = Claims(sub="app-developer", groups=["payments-eng"])
        outcome, pending = await self._resolve(world, registry, claims, "attest prod-east-2.")
        assert isinstance(outcome, Resolved)
        assert outcome.context.clusters == ["prod-east-2"]
        assert pending is None

    def test_pick_option_exact_beats_substring(self):
        """Regression: 'prod-east-1' is a substring of 'nonprod-east-1', so
        substring-only matching looped the clarification forever."""
        options = ["nonprod-east-1", "prod-east-1", "prod-east-2"]
        assert ctx_resolution._pick_option("prod-east-1", options) == "prod-east-1"
        assert ctx_resolution._pick_option("nonprod-east-1", options) == "nonprod-east-1"
        assert ctx_resolution._pick_option("prod-east-1.", options) == "prod-east-1"
        assert ctx_resolution._pick_option("2", options) == "prod-east-1"
        assert ctx_resolution._pick_option("east", options) is None

    @pytest.mark.asyncio
    async def test_conversational_environment_override(self, world, registry):
        """FR-CTX-7: 'switch to nonprod' moves the same app to its nonprod
        instance without re-asking anything."""
        claims = Claims(sub="app-developer", groups=["payments-eng"])
        outcome, pending = await self._resolve(
            world, registry, claims, "switch to nonprod", prior=_prior_payments_prod()
        )
        assert isinstance(outcome, Resolved)
        assert outcome.context.application == "payments-api"
        assert outcome.context.environment == "nonprod"
        assert outcome.context.clusters == ["nonprod-east-1"]
        assert pending is None

    @pytest.mark.asyncio
    async def test_conversational_application_override(self, world, registry):
        """FR-CTX-7: naming another application mid-thread switches the app."""
        claims = Claims(sub="platform-sre", groups=["retail-sre"])
        outcome, _ = await self._resolve(
            world, registry, claims, "what about checkout?", prior=_prior_payments_prod()
        )
        assert isinstance(outcome, Resolved)
        assert outcome.context.application == "checkout"
        assert outcome.context.environment == "prod"

    @pytest.mark.asyncio
    async def test_application_override_keeps_the_thread_environment(self, world, registry):
        """Switching application does not re-open the environment question:
        catalog runs in both environments, and the thread is already in prod."""
        claims = Claims(sub="platform-sre", groups=["retail-sre"])
        outcome, _ = await self._resolve(
            world, registry, claims, "what about catalog?", prior=_prior_payments_prod()
        )
        assert isinstance(outcome, Resolved)
        assert outcome.context.application == "catalog"
        assert outcome.context.environment == "prod"

    @pytest.mark.asyncio
    async def test_conversational_cluster_override(self, world, registry):
        """FR-CTX-7: an explicit attest request wins over the thread's app scope."""
        claims = Claims(sub="app-developer", groups=["payments-eng"])
        outcome, _ = await self._resolve(
            world, registry, claims, "attest nonprod-east-1", prior=_prior_payments_prod()
        )
        assert isinstance(outcome, Resolved)
        assert outcome.context.scope == "cluster"
        assert outcome.context.clusters == ["nonprod-east-1"]
        assert outcome.context.application is None

    @pytest.mark.asyncio
    async def test_cluster_clarify_reply_resolves_bare_name(self, world, registry):
        """A cluster-clarify reply naming any real cluster resolves, even if
        the name was not in the (possibly truncated) options list."""
        claims = Claims(sub="platform-sre", groups=["retail-sre"])
        pending = {"kind": "cluster", "options": ["prod-east-1", "prod-east-2"]}
        outcome, _ = await self._resolve(
            world, registry, claims, "nonprod-east-1", pending=pending
        )
        assert isinstance(outcome, Resolved)
        assert outcome.context.scope == "cluster"
        assert outcome.context.clusters == ["nonprod-east-1"]
