"""Context resolution: claims -> application -> verified placements.

The whole point of this module after the live cutover is the FR-CTX-2
contract. The fleet registry PROPOSES placements; the cluster API CONFIRMS
them. Every case below runs both halves against the doubles in fakes.py, so a
"resolved" outcome here means a fake cluster actually returned pods.
"""

from __future__ import annotations

import pytest
from fakes import APP_LABEL, APP_NS, DEGRADED, HEALTHY, UNREACHABLE, FakeGateway
from registry_fixtures import seeded_registry  # noqa: F401 - fixture import

from cloudops.agent import context as ctx_resolution
from cloudops.agent.context import Claims, Clarify, Onboarding, Resolved
from cloudops.agent.models import AppInstance, ResolvedContext

# LiveFleet resolves cluster records through the MongoDB registry since the
# live cutover, so the fleet doubles in this module need the seeded mongomock
# registry standing behind them.
pytestmark = pytest.mark.usefixtures("seeded_registry")

# What the orchestrator passes every turn, from config/models.yaml agent.* .
DEFAULT_POLICY = {"default_environment": "prod"}
STRICT_POLICY = {"default_environment": None}

PAYMENTS = Claims(sub="app-developer", name="App Developer", groups=["payments-eng"])
RETAIL = Claims(sub="platform-sre", name="Platform SRE", groups=["retail-sre"])
LOGISTICS = Claims(sub="logistics-dev", groups=["logistics-eng"])
NEWCOMER = Claims(sub="new-joiner", groups=["newcomers"])


async def resolve(gateway, registry, claims, text, pending=None, prior=None,
                  policy=DEFAULT_POLICY):
    return await ctx_resolution.resolve(claims, text, registry, gateway, prior, pending, policy)


def _prior_payments_prod() -> ResolvedContext:
    """A thread that already resolved payments-api in prod (the F1 baseline)."""
    return ResolvedContext(
        scope="app", user_sub="app-developer", user_name="App Developer",
        groups=["payments-eng"], application="payments-api", app_label=APP_LABEL,
        environment="prod",
        instances=[AppInstance(cluster=HEALTHY, namespace=APP_NS, environment="prod")],
        clusters=[HEALTHY],
    )


class TestResolution:
    @pytest.mark.asyncio
    async def test_single_app_zero_questions(self, gateway, registry):
        outcome, pending = await resolve(gateway, registry, PAYMENTS,
                                         "why is my app flaky in prod?")
        assert isinstance(outcome, Resolved)
        assert outcome.context.application == "payments-api"
        assert outcome.context.environment == "prod"
        assert outcome.context.clusters == [HEALTHY]
        assert pending is None

    @pytest.mark.asyncio
    async def test_registry_proposes_and_the_cluster_confirms(self, gateway, registry):
        """FR-CTX-2 in one assertion: both tools ran, in that order, and the
        instance carries what the cluster actually reported."""
        outcome, _ = await resolve(gateway, registry, PAYMENTS, "how is payments-api in prod?")
        assert isinstance(outcome, Resolved)
        tools = [t for t, _ in gateway.calls]
        assert tools.index("reg__find_placements") < tools.index("ocp__verify_placement")
        [instance] = outcome.context.instances
        assert (instance.verified, instance.pod_count, instance.ready_count) == (True, 2, 2)

    @pytest.mark.asyncio
    async def test_unverified_placements_are_dropped_not_reported(self, gateway, registry):
        """A registry row the cluster does not back is a stale row, not an
        instance. The registry claims checkout on two prod clusters; only one
        of them has the pods, and only that one reaches the report."""
        outcome, _ = await resolve(gateway, registry, RETAIL, "how is checkout in prod?")
        assert isinstance(outcome, Resolved)
        assert {p["cluster"] for p in gateway.registry.find_placements(
            app_id="checkout")["placements"]} == {"acm-hub-1", HEALTHY}
        assert outcome.context.clusters == [HEALTHY]

    @pytest.mark.asyncio
    async def test_all_placements_unverified_is_an_onboarding_answer(
        self, gateway, registry, world
    ):
        world[HEALTHY].ns(APP_NS).pods = []
        outcome, _ = await resolve(gateway, registry, PAYMENTS, "how is payments-api in prod?")
        assert isinstance(outcome, Onboarding)
        assert "registry" in outcome.message.lower()
        assert f"{HEALTHY}/{APP_NS}" in outcome.message
        assert "0 pod(s)" in outcome.message

    @pytest.mark.asyncio
    async def test_unreachable_candidates_are_kept_with_a_note(self, gateway, registry, world):
        """Nothing was denied, so nothing may be declared gone: the placement
        stays, flagged, and the context says why."""
        world[HEALTHY].reachable = False
        outcome, _ = await resolve(gateway, registry, PAYMENTS, "how is payments-api in prod?")
        assert isinstance(outcome, Resolved)
        [instance] = outcome.context.instances
        assert (instance.reachable, instance.verified) == (False, False)
        assert outcome.context.placement_note
        assert "could not be verified" in outcome.context.placement_note

    @pytest.mark.asyncio
    async def test_registry_knows_nothing_about_the_app(self, gateway, registry, monkeypatch):
        monkeypatch.setattr(gateway.registry, "placements", [])
        outcome, _ = await resolve(gateway, registry, PAYMENTS, "how is payments-api?")
        assert isinstance(outcome, Onboarding)
        assert "no placements" in outcome.message

    @pytest.mark.asyncio
    async def test_multi_app_asks_exactly_one_question(self, gateway, registry):
        outcome, pending = await resolve(gateway, registry, RETAIL, "is my stuff healthy?")
        assert isinstance(outcome, Clarify)
        assert len(outcome.options) == 6
        assert pending["kind"] == "application"
        # FR-CTX-4: no checks, and no placement lookups, before the answer.
        assert [t for t, _ in gateway.calls] == []

    @pytest.mark.asyncio
    async def test_clarification_reply_by_number(self, gateway, registry):
        _, pending = await resolve(gateway, registry, RETAIL, "is my stuff healthy?")
        outcome, _ = await resolve(gateway, registry, RETAIL, "1", pending=pending)
        # Option 1 is alphabetically first: audit-log, single environment.
        assert isinstance(outcome, Resolved)
        assert outcome.context.application == pending["options"][0] == "audit-log"
        assert outcome.context.clusters == ["acm-hub-1"]

    @pytest.mark.asyncio
    async def test_no_apps_onboarding(self, gateway, registry):
        outcome, _ = await resolve(gateway, registry, NEWCOMER, "why is my app slow?")
        assert isinstance(outcome, Onboarding)
        assert "register" in outcome.message.lower()

    @pytest.mark.asyncio
    async def test_named_app_outside_registered_set(self, gateway, registry):
        outcome, _ = await resolve(gateway, registry, NEWCOMER, "it's inventory-sync")
        assert isinstance(outcome, Resolved)
        assert outcome.context.application == "inventory-sync"
        assert outcome.context.outside_registered_set is True

    @pytest.mark.asyncio
    async def test_unauthenticated_runs_nothing(self, gateway, registry):
        outcome, _ = await resolve(gateway, registry, Claims(), "help")
        assert isinstance(outcome, Onboarding)
        assert [t for t, _ in gateway.calls] == []


class TestEnvironmentScoping:
    @pytest.mark.asyncio
    async def test_env_ambiguity_assumes_the_default(self, gateway, registry):
        """FR-CTX-8: payments-api runs in both environments, so the strict
        behavior asked; with a default configured the turn resolves to prod
        and says so instead of spending the one question."""
        outcome, pending = await resolve(gateway, registry, PAYMENTS,
                                         "how is payments-api doing?")
        assert isinstance(outcome, Resolved)
        assert outcome.context.environment == "prod"
        assert outcome.context.environment_assumed is True
        assert pending is None

    @pytest.mark.asyncio
    async def test_env_ambiguity_asks_under_strict_policy(self, gateway, registry):
        """FR-CTX-8: `default_environment: null` restores strict clarification."""
        outcome, pending = await resolve(gateway, registry, PAYMENTS,
                                         "how is payments-api doing?", policy=STRICT_POLICY)
        assert isinstance(outcome, Clarify)
        assert set(outcome.options) == {"prod", "nonprod"}
        assert pending["kind"] == "environment"

    @pytest.mark.asyncio
    async def test_vague_question_runs_the_full_pipeline(self, gateway, registry):
        """F1: 'is my app down' carries neither app nor environment, and still
        resolves with zero questions (FR-CTX-8)."""
        outcome, pending = await resolve(gateway, registry, PAYMENTS, "is my app down")
        assert isinstance(outcome, Resolved)
        assert outcome.context.scope == "app"
        assert outcome.context.application == "payments-api"
        assert outcome.context.environment_assumed is True
        assert pending is None

    @pytest.mark.asyncio
    async def test_single_environment_app_is_not_marked_assumed(self, gateway, registry):
        """inventory-sync has one placement: a fact, not an assumption, and the
        default never enters into it."""
        outcome, _ = await resolve(gateway, registry, LOGISTICS, "how is my app?")
        assert isinstance(outcome, Resolved)
        assert outcome.context.application == "inventory-sync"
        assert outcome.context.clusters == ["acm-spoke-1b"]
        assert outcome.context.environment_assumed is False

    @pytest.mark.asyncio
    async def test_stated_environment_is_not_marked_assumed(self, gateway, registry):
        outcome, _ = await resolve(gateway, registry, PAYMENTS,
                                   "how is payments-api doing in nonprod?")
        assert isinstance(outcome, Resolved)
        assert outcome.context.environment == "nonprod"
        assert outcome.context.clusters == [DEGRADED]
        assert outcome.context.environment_assumed is False


class TestClusterScope:
    @pytest.mark.asyncio
    async def test_direct_cluster_attest(self, gateway, registry):
        outcome, _ = await resolve(gateway, registry, RETAIL, "attest s1a")
        assert isinstance(outcome, Resolved)
        assert outcome.context.scope == "cluster"
        assert outcome.context.clusters == [HEALTHY]

    @pytest.mark.asyncio
    async def test_attest_with_trailing_punctuation(self, gateway, registry):
        """Regression: 'attest acm-spoke-1a.' captured the dot, fuzzy-matched
        several clusters, and asked to clarify instead of resolving."""
        outcome, pending = await resolve(gateway, registry, PAYMENTS, f"attest {HEALTHY}.")
        assert isinstance(outcome, Resolved)
        assert outcome.context.clusters == [HEALTHY]
        assert pending is None

    @pytest.mark.asyncio
    async def test_ambiguous_cluster_asks(self, gateway, registry):
        outcome, pending = await resolve(gateway, registry, RETAIL, "attest acm-spoke")
        assert isinstance(outcome, Clarify)
        assert len(outcome.options) == 4
        assert pending["kind"] == "cluster"

    @pytest.mark.asyncio
    async def test_unknown_cluster_says_so(self, gateway, registry):
        outcome, _ = await resolve(gateway, registry, RETAIL, "attest nope-not-here")
        assert isinstance(outcome, Onboarding)
        assert "nope-not-here" in outcome.message

    @pytest.mark.asyncio
    async def test_cluster_clarify_reply_resolves_bare_name(self, gateway, registry):
        """A cluster-clarify reply naming any real cluster resolves, even if
        the name was not in the (possibly truncated) options list."""
        pending = {"kind": "cluster", "options": [HEALTHY, "acm-spoke-1b"]}
        outcome, _ = await resolve(gateway, registry, RETAIL, UNREACHABLE, pending=pending)
        assert isinstance(outcome, Resolved)
        assert outcome.context.scope == "cluster"
        assert outcome.context.clusters == [UNREACHABLE]


class TestConversationalOverrides:
    @pytest.mark.asyncio
    async def test_environment_override(self, gateway, registry):
        """FR-CTX-7: 'switch to nonprod' moves the same app to its nonprod
        instance without re-asking anything."""
        outcome, pending = await resolve(gateway, registry, PAYMENTS, "switch to nonprod",
                                         prior=_prior_payments_prod())
        assert isinstance(outcome, Resolved)
        assert outcome.context.application == "payments-api"
        assert outcome.context.environment == "nonprod"
        assert outcome.context.clusters == [DEGRADED]
        assert pending is None

    @pytest.mark.asyncio
    async def test_application_override(self, gateway, registry):
        outcome, _ = await resolve(gateway, registry, RETAIL, "what about checkout?",
                                   prior=_prior_payments_prod())
        assert isinstance(outcome, Resolved)
        assert outcome.context.application == "checkout"
        assert outcome.context.environment == "prod"

    @pytest.mark.asyncio
    async def test_application_override_keeps_the_thread_environment(self, gateway, registry):
        """Switching application does not re-open the environment question:
        catalog runs in both environments, and the thread is already in prod."""
        outcome, _ = await resolve(gateway, registry, RETAIL, "what about catalog?",
                                   prior=_prior_payments_prod())
        assert isinstance(outcome, Resolved)
        assert outcome.context.application == "catalog"
        assert outcome.context.environment == "prod"

    @pytest.mark.asyncio
    async def test_cluster_override_wins_over_app_scope(self, gateway, registry):
        outcome, _ = await resolve(gateway, registry, PAYMENTS, f"attest {DEGRADED}",
                                   prior=_prior_payments_prod())
        assert isinstance(outcome, Resolved)
        assert outcome.context.scope == "cluster"
        assert outcome.context.clusters == [DEGRADED]
        assert outcome.context.application is None


def test_pick_option_exact_beats_substring():
    """Regression: 'prod-east-1' is a substring of 'nonprod-east-1', so
    substring-only matching looped the clarification forever."""
    options = ["nonprod-east-1", "prod-east-1", "prod-east-2"]
    assert ctx_resolution._pick_option("prod-east-1", options) == "prod-east-1"
    assert ctx_resolution._pick_option("nonprod-east-1", options) == "nonprod-east-1"
    assert ctx_resolution._pick_option("prod-east-1.", options) == "prod-east-1"
    assert ctx_resolution._pick_option("2", options) == "prod-east-1"
    assert ctx_resolution._pick_option("east", options) is None


class TestRegistryDouble:
    """The reg__* shapes the agent codes against, pinned on the double so a
    contract drift in the real registry shows up as a failure here too."""

    def test_find_placements_matches_id_or_label(self, app_registry):
        by_id = app_registry.find_placements(app_id="payments-api")
        assert by_id["count"] == 2
        assert {p["cluster"] for p in by_id["placements"]} == {HEALTHY, DEGRADED}
        assert set(by_id["placements"][0]) == {
            "app_id", "application", "app_label", "cluster", "namespace",
            "environment", "lob",
        }

    def test_blast_radius_and_apps_on_cluster(self, app_registry):
        on_cluster = app_registry.list_apps_on_cluster(HEALTHY)
        assert "payments-api" in on_cluster["apps"]
        radius = app_registry.blast_radius(cluster=HEALTHY)
        assert radius["apps"] == on_cluster["apps"]
        assert radius["summary"].endswith("affected")


@pytest.mark.asyncio
async def test_gateway_double_rejects_unknown_tools(gateway):
    """The double must not silently answer a tool the real gateway would
    refuse, or a typo in production code would pass its test."""
    with pytest.raises(Exception, match="unknown tool"):
        await gateway.call("obs__get_firing_alerts", {"cluster": HEALTHY})


def test_fake_gateway_is_the_only_registry(gateway: FakeGateway):
    assert gateway.registry.find_placements(app_id="nothing")["count"] == 0
