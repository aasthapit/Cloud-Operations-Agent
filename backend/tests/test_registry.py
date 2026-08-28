"""Fleet registry tests: the data-access lib, the seeder, and cluster auth.

Two halves, both hermetic (NFR-QE-1):

1. The registry itself against mongomock, seeded by the real seeder from the
   committed fixtures. What is asserted is the behaviour the agent depends
   on: seeding twice changes nothing, "SSOP" resolves to an application, a
   typo still finds the right one, filters narrow rather than widen, and
   blast radius answers with the shape the design doc pins.
2. The OpenShift OAuth exchange against httpx.MockTransport, because the
   basic-auth path is the one credential flow with real moving parts
   (discovery, a 302 whose FRAGMENT carries the token, a TTL cache, and a
   401 that must re-exchange rather than fail).
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest
from registry_fixtures import seeded_registry  # noqa: F401 - fixture import

from cloudops.mcp_servers import kube
from cloudops.mcp_servers.kube import (
    KubeClient,
    OAuthExchangeError,
    basic_token,
    clear_token_cache,
    exchange_basic_for_token,
)
from cloudops.registry import queries
from cloudops.registry.queries import RegistryQueryError
from cloudops.registry.seed import seed

pytestmark = pytest.mark.usefixtures("seeded_registry")


# ---------------------------------------------------------------------------
# seeding
# ---------------------------------------------------------------------------


def test_seeding_is_idempotent(seeded_registry: Any) -> None:  # noqa: F811
    """The fixture already seeded once; a second run must change nothing."""
    from conftest import CONFIG_DIR

    before = {name: seeded_registry[name].count_documents({})
              for name in ("clusters", "apps", "placements")}
    summary = seed(CONFIG_DIR)
    after = {name: seeded_registry[name].count_documents({})
             for name in ("clusters", "apps", "placements")}

    assert before == after
    for name in ("clusters", "apps", "placements"):
        assert summary[name]["inserted"] == 0
        assert summary[name]["updated"] == 0


def test_seeded_clusters_carry_kubeconfig_auth(seeded_registry: Any) -> None:  # noqa: F811
    doc = seeded_registry["clusters"].find_one({"name": "acm-spoke-1a"})
    assert doc["auth"] == {"type": "kubeconfig", "context": "kind-acm-spoke-1a"}
    assert doc["environment"] == "prod"
    assert "s1a" in doc["aliases"]


def test_placements_carry_the_line_of_business(seeded_registry: Any) -> None:  # noqa: F811
    rows = list(seeded_registry["placements"].find({"app_id": "PAY"}))
    assert rows, "payments-api must be placed somewhere"
    assert {r["lob"] for r in rows} == {"Payments"}
    assert {r["cluster"] for r in rows} == {"acm-spoke-1a", "acm-spoke-2a"}


def test_cluster_credentials_never_reach_a_tool_result() -> None:
    """public_cluster is the only door cluster records leave through."""
    record = queries.get_cluster("acm-spoke-1a")
    assert record is not None and "auth" in record
    assert "auth" not in queries.public_cluster(record)


# ---------------------------------------------------------------------------
# resolve_entity
# ---------------------------------------------------------------------------


def test_resolve_entity_finds_an_app_by_its_short_id() -> None:
    """The question this whole tool exists for: 'is app SSOP down?'"""
    result = queries.resolve_entity("SSOP")
    top = result["matches"][0]
    assert (top["kind"], top["id"], top["score"]) == ("app", "SSOP", 1.0)
    assert top["detail"]["application"] == "ssop"


def test_resolve_entity_finds_a_cluster_by_alias() -> None:
    matches = queries.resolve_entity("s1a", kind_hint="cluster")["matches"]
    assert matches[0]["id"] == "acm-spoke-1a"
    assert matches[0]["score"] == queries.SCORE_ALIAS
    assert "auth" not in matches[0]["detail"]


def test_resolve_entity_finds_lobs_and_namespaces() -> None:
    lob = queries.resolve_entity("Payments", kind_hint="lob")["matches"][0]
    assert (lob["kind"], lob["id"]) == ("lob", "Payments")
    assert set(lob["detail"]["apps"]) == {"PAY", "RFND"}

    namespace = queries.resolve_entity("logistics-dev", kind_hint="namespace")["matches"][0]
    assert namespace["detail"]["clusters"] == ["acm-spoke-1b"]


def test_resolve_entity_survives_a_typo_and_ranks_it_below_a_real_match() -> None:
    result = queries.resolve_entity("paymnets-api")
    top = result["matches"][0]
    assert (top["kind"], top["id"]) == ("app", "PAY")
    # A fuzzy hit must never look like an exact one, or the agent will stop
    # asking the user to confirm.
    assert 0 < top["score"] <= queries.FUZZY_SCALE
    assert result["suggestion"]


def test_resolve_entity_admits_when_nothing_matches() -> None:
    result = queries.resolve_entity("zzz-no-such-thing")
    assert result["matches"] == []
    assert "nothing in the registry matched" in result["suggestion"]


def test_resolve_entity_respects_the_kind_hint() -> None:
    kinds = {m["kind"] for m in queries.resolve_entity("payments", kind_hint="app")["matches"]}
    assert kinds == {"app"}


# ---------------------------------------------------------------------------
# placements
# ---------------------------------------------------------------------------


def test_find_placements_filters_narrow_and_and_together() -> None:
    everything = queries.find_placements()
    by_app = queries.find_placements(app_id="PAY")
    assert 0 < by_app["count"] < everything["count"]
    assert {p["app_id"] for p in by_app["placements"]} == {"PAY"}

    both = queries.find_placements(app_id="PAY", cluster="acm-spoke-1a")
    assert both["count"] == 1
    assert both["placements"][0]["namespace"] == "payments-prod"

    assert queries.find_placements(environment="prod")["count"] < everything["count"]
    assert queries.find_placements(lob="Logistics")["count"] == 1
    assert queries.find_placements(app_id="PAY", cluster="no-such-cluster")["count"] == 0


def test_find_placements_is_case_insensitive_but_never_fuzzy() -> None:
    assert queries.find_placements(app_id="pay")["count"] == \
        queries.find_placements(app_id="PAY")["count"]
    assert queries.find_placements(app_id="PA")["count"] == 0


def test_placement_documents_carry_the_designed_shape() -> None:
    row = queries.find_placements(app_id="SSOP")["placements"][0]
    assert set(row) == {"app_id", "application", "app_label", "cluster", "namespace",
                        "environment", "lob"}


def test_list_apps_on_cluster_groups_by_application() -> None:
    result = queries.list_apps_on_cluster("acm-spoke-1a")
    assert result["cluster"] == "acm-spoke-1a"
    assert result["count"] == len(result["apps"])
    payments = next(a for a in result["apps"] if a["app_id"] == "PAY")
    assert payments["namespaces"] == ["payments-prod"]
    assert payments["lob"] == "Payments"
    assert "payments-prod" in result["namespaces"]
    assert "Payments" in result["lobs"]


def test_list_apps_on_cluster_honours_the_environment_filter() -> None:
    assert queries.list_apps_on_cluster("acm-spoke-1a", environment="nonprod")["count"] == 0
    assert queries.list_apps_on_cluster("acm-spoke-1a", environment="prod")["count"] > 0


# ---------------------------------------------------------------------------
# blast radius and LOBs
# ---------------------------------------------------------------------------


BLAST_KEYS = {"scope", "placement_count", "apps", "clusters", "namespaces", "lobs",
              "environments", "summary"}


def test_blast_radius_for_a_cluster() -> None:
    result = queries.blast_radius(cluster="acm-spoke-2a")
    assert set(result) == BLAST_KEYS
    assert result["scope"] == {"cluster": "acm-spoke-2a"}
    assert "PAY" in {a["app_id"] for a in result["apps"]}
    assert result["clusters"] == ["acm-spoke-2a"]
    assert "payments-prod" in result["namespaces"]
    assert "Payments" in result["lobs"]
    assert "acm-spoke-2a" in result["summary"]
    payments = next(a for a in result["apps"] if a["app_id"] == "PAY")
    assert payments["owner_groups"] == ["payments-eng"]
    assert payments["criticality"] == "high"


def test_blast_radius_for_a_line_of_business_spans_clusters() -> None:
    result = queries.blast_radius(lob="Retail")
    assert {a["app_id"] for a in result["apps"]} == {"CHKT", "CTLG", "LOYL", "PRCG"}
    assert len(result["clusters"]) > 1
    assert result["lobs"] == ["Retail"]


def test_blast_radius_of_an_empty_scope_is_honest_not_empty() -> None:
    result = queries.blast_radius(namespace="no-such-namespace")
    assert result["placement_count"] == 0
    assert result["apps"] == []
    assert "no placements" in result["summary"]


def test_blast_radius_refuses_a_scopeless_question() -> None:
    """Answering 'what breaks if nothing breaks' with the whole fleet would be
    exactly the confident wrong answer FR-MCP-2 forbids."""
    with pytest.raises(RegistryQueryError):
        queries.blast_radius()


def test_list_lobs_counts_apps_and_clusters() -> None:
    result = queries.list_lobs()
    by_lob = {row["lob"]: row for row in result["lobs"]}
    assert result["count"] == len(result["lobs"])
    assert by_lob["Payments"]["app_count"] == 2
    assert by_lob["Platform"]["apps"] == ["SSOP"]
    assert by_lob["Platform"]["cluster_count"] == 2


# ---------------------------------------------------------------------------
# get_app
# ---------------------------------------------------------------------------


def test_get_app_returns_the_registry_entry_with_its_placements() -> None:
    app = queries.get_app("SSOP")
    assert app["found"] is True
    assert app["application"] == "ssop"
    assert app["owner_groups"] == ["platform-sre"]
    assert app["runbook_url"].endswith("/ssop")
    assert set(app["clusters"]) == {"acm-hub-1", "acm-hub-2"}
    assert set(app["namespaces"]) == {"ssop-dev", "ssop-prod"}


def test_get_app_accepts_a_name_or_a_pod_label() -> None:
    assert queries.get_app("payments-api")["app_id"] == "PAY"
    assert queries.get_app("PAYMENTS-API")["app_id"] == "PAY"


def test_get_app_says_so_when_it_does_not_know() -> None:
    assert queries.get_app("nope")["found"] is False


# ---------------------------------------------------------------------------
# OpenShift OAuth: username/password -> bearer token
# ---------------------------------------------------------------------------


API_URL = "https://api.ocp.example.internal:6443"
AUTHORIZE = "https://oauth-openshift.apps.ocp.example.internal/oauth/authorize"


class FakeOAuthCluster:
    """A cluster that challenges for credentials, as httpx MockTransport.

    Mirrors the real flow: metadata discovery, then a 302 whose Location
    FRAGMENT carries the token. It hands out a fresh token on every exchange
    and rejects every token but the latest, so a stale token produces a real
    401 rather than a simulated one.
    """

    def __init__(self, unauthorized_calls: int = 0) -> None:
        self.exchanges = 0
        self.api_calls = 0
        self.seen_authorization: list[str] = []
        self.current_token = ""
        self._unauthorized_calls = unauthorized_calls
        self.expires_in = 86400

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json={
                "issuer": "https://oauth-openshift.apps.ocp.example.internal",
                "authorization_endpoint": AUTHORIZE,
                "token_endpoint": AUTHORIZE.replace("/authorize", "/token"),
            })
        if str(request.url).startswith(AUTHORIZE):
            self.exchanges += 1
            self.seen_authorization.append(request.headers.get("authorization", ""))
            assert request.headers.get("x-csrf-token") == "1"
            assert request.url.params.get("client_id") == "openshift-challenging-client"
            assert request.url.params.get("response_type") == "token"
            self.current_token = f"sha256~token-{self.exchanges}"
            return httpx.Response(302, headers={"location": (
                f"https://oauth-openshift.apps.ocp.example.internal/oauth/token/implicit"
                f"#access_token={self.current_token}&expires_in={self.expires_in}"
                f"&token_type=Bearer"
            )})
        self.api_calls += 1
        if self._unauthorized_calls > 0:
            self._unauthorized_calls -= 1
            return httpx.Response(401, json={"message": "Unauthorized"})
        expected = f"Bearer {self.current_token}"
        if request.headers.get("authorization") != expected:
            return httpx.Response(401, json={"message": "Unauthorized"})
        return httpx.Response(200, json={"items": [{"metadata": {"name": "cp-1"}}]})


@pytest.fixture(autouse=True)
def _clean_token_cache() -> Any:
    clear_token_cache()
    yield
    clear_token_cache()


def test_oauth_exchange_discovers_the_endpoint_and_parses_the_fragment() -> None:
    cluster = FakeOAuthCluster()
    token, expires_in = exchange_basic_for_token(
        API_URL, "operator", "hunter2", transport=cluster.transport())
    assert token == "sha256~token-1"
    assert expires_in == 86400
    assert cluster.seen_authorization[0].startswith("Basic ")


def test_oauth_exchange_reports_a_refused_challenge() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(200, json={"authorization_endpoint": AUTHORIZE})
        return httpx.Response(401, json={"message": "Unauthorized"})

    with pytest.raises(OAuthExchangeError):
        exchange_basic_for_token(
            API_URL, "operator", "wrong", transport=httpx.MockTransport(handle))


def test_oauth_token_is_cached_until_it_expires() -> None:
    cluster = FakeOAuthCluster()
    transport = cluster.transport()
    first = basic_token(API_URL, "operator", "hunter2", transport=transport)
    second = basic_token(API_URL, "operator", "hunter2", transport=transport)
    assert first == second
    assert cluster.exchanges == 1, "the second call must come from the cache"

    # Expire the cache entry the way time would, and the next call re-exchanges.
    kube._TOKEN_CACHE[(API_URL, "operator")] = (first, time.time() - 1)
    third = basic_token(API_URL, "operator", "hunter2", transport=transport)
    assert cluster.exchanges == 2
    assert third != first


def test_oauth_token_can_be_forced_past_the_cache() -> None:
    cluster = FakeOAuthCluster()
    transport = cluster.transport()
    basic_token(API_URL, "operator", "hunter2", transport=transport)
    basic_token(API_URL, "operator", "hunter2", transport=transport, force=True)
    assert cluster.exchanges == 2


# ---------------------------------------------------------------------------
# KubeClient built from a cluster record's auth block
# ---------------------------------------------------------------------------


def basic_record() -> dict[str, Any]:
    return {
        "name": "ocp-prod-1",
        "api_url": API_URL,
        "auth": {"type": "basic", "username": "operator", "password": "hunter2",
                 "insecure_skip_tls_verify": True},
    }


def test_kube_client_authenticates_a_basic_record_end_to_end() -> None:
    cluster = FakeOAuthCluster()
    client = KubeClient(basic_record(), transport=cluster.transport())
    assert client.get("/api/v1/nodes")["items"][0]["metadata"]["name"] == "cp-1"
    assert cluster.exchanges == 1


def test_kube_client_re_exchanges_on_a_401_and_retries_once() -> None:
    """A revoked token must self-heal, not fail the call."""
    cluster = FakeOAuthCluster(unauthorized_calls=1)
    client = KubeClient(basic_record(), transport=cluster.transport())
    assert client.get("/api/v1/nodes")["items"]
    assert cluster.exchanges == 2, "the 401 must trigger exactly one re-exchange"
    assert cluster.api_calls == 2


def test_kube_client_accepts_a_token_record() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sha256~preissued"
        return httpx.Response(200, json={"gitVersion": "v1.33.7"})

    client = KubeClient(
        {"name": "ocp-prod-2", "api_url": API_URL,
         "auth": {"type": "token", "token": "sha256~preissued",
                  "insecure_skip_tls_verify": True}},
        transport=httpx.MockTransport(handle),
    )
    assert client.get("/version")["gitVersion"] == "v1.33.7"


def test_kube_client_rejects_an_auth_type_it_does_not_understand() -> None:
    with pytest.raises(kube.KubeConfigError):
        KubeClient({"name": "x", "api_url": API_URL, "auth": {"type": "carrier-pigeon"}})


def test_kube_client_never_puts_credentials_in_its_headers() -> None:
    cluster = FakeOAuthCluster()
    client = KubeClient(basic_record(), transport=cluster.transport())
    rendered = str(dict(client._client.headers))
    assert "hunter2" not in rendered
    assert "operator" not in rendered
