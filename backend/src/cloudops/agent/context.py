"""User context resolution (FR-CTX-1..8): claims -> applications -> placements.

Pure decision logic plus gateway lookups; no LLM. The orchestrator calls
resolve() every turn and acts on the outcome:

  Onboarding   no claims, or no registered apps and none named (FR-CTX-5, FR-ID-4)
  Clarify      exactly one question with enumerated options (FR-CTX-4)
  Resolved     application + verified placements, or explicit cluster scope

Placement is ALWAYS verified through obs__find_app_placements (Prometheus
series in live mode), never assumed from the registry (FR-CTX-2).

Application ambiguity always asks; environment ambiguity does not have to.
FR-CTX-8 gives the environment a configured default (policy["default_environment"],
carried from the hot agent tuning by the orchestrator): when the application is
resolved, the environment is unstated, and the app runs in the default, the turn
resolves there and marks the context environment_assumed instead of spending the
user's one question on it. Setting the policy to None restores strict asking.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import structlog

from cloudops.agent.gateway_client import GatewayClient
from cloudops.agent.models import AppInstance, ResolvedContext

log = structlog.get_logger("cloudops.context")

_ATTEST_RE = re.compile(r"\battest\s+([A-Za-z0-9._-]+)", re.I)
_ENV_HINTS = {
    "prod": "prod", "production": "prod",
    "nonprod": "nonprod", "non-prod": "nonprod", "dev": "nonprod",
    "staging": "nonprod", "qa": "nonprod", "uat": "nonprod",
}


@dataclass
class Onboarding:
    message: str


@dataclass
class Clarify:
    question: str
    options: list[str]
    kind: str  # "application" | "environment"


@dataclass
class Resolved:
    context: ResolvedContext


Outcome = Onboarding | Clarify | Resolved


@dataclass
class Claims:
    sub: str = ""
    name: str = ""
    email: str = ""
    groups: list[str] = field(default_factory=list)

    @classmethod
    def from_metadata(cls, meta: dict[str, Any]) -> Claims:
        return cls(
            sub=str(meta.get("sub", "") or ""),
            name=str(meta.get("name", "") or ""),
            email=str(meta.get("email", "") or ""),
            groups=[str(g) for g in (meta.get("groups") or [])],
        )


def candidate_apps(claims: Claims, registry: dict[str, Any]) -> list[dict[str, Any]]:
    """Applications whose owner_groups intersect the user's groups."""
    groups = set(claims.groups)
    return [
        a for a in registry.get("applications", [])
        if groups.intersection(a.get("owner_groups", []))
    ]


def _named_app(text: str, registry: dict[str, Any]) -> dict[str, Any] | None:
    """An application explicitly named in the user's message (any app in the
    org registry, not just the user's own: visibility beats ceremony, F3)."""
    lowered = text.lower()
    hits = [a for a in registry.get("applications", []) if a["application"].lower() in lowered]
    # Longest name wins so 'payments-api' beats a hypothetical 'payments'.
    return max(hits, key=lambda a: len(a["application"]), default=None)


def _env_hint(text: str) -> str | None:
    lowered = text.lower()
    for token, env in _ENV_HINTS.items():
        if re.search(rf"\b{re.escape(token)}\b", lowered):
            return env
    return None


def _pick_option(text: str, options: list[str]) -> str | None:
    """Interpret a clarification reply: a number, an exact option, or a
    unique substring match.

    Exact equality MUST win before substring matching: 'prod-east-1' is a
    substring of 'nonprod-east-1', so substring-only matching declares a
    perfectly precise reply ambiguous and loops the clarification forever.
    """
    stripped = text.strip().strip(".,;:!?").lower()
    m = re.match(r"^(\d+)\b", stripped)
    if m and 1 <= int(m.group(1)) <= len(options):
        return options[int(m.group(1)) - 1]
    exact = [o for o in options if o.lower() == stripped]
    if exact:
        return exact[0]
    # An option spoken inside a longer reply ("checkout, prod") counts, but
    # only on name boundaries: 'prod-east-1' must not match inside
    # 'nonprod-east-1', so hyphens and dots are name characters, not breaks.
    contained = [
        o for o in options
        if re.search(rf"(?<![a-z0-9._-]){re.escape(o.lower())}(?![a-z0-9._-])", stripped)
    ]
    if len(contained) == 1:
        return contained[0]
    prefix = [o for o in options if o.lower().startswith(stripped)]
    if len(prefix) == 1:
        return prefix[0]
    matches = [o for o in options if stripped in o.lower()]
    return matches[0] if len(matches) == 1 else None


async def resolve(
    claims: Claims,
    user_text: str,
    registry: dict[str, Any],
    client: GatewayClient,
    prior: ResolvedContext | None,
    pending: dict[str, Any] | None,
    policy: dict[str, Any] | None = None,
) -> tuple[Outcome, dict[str, Any] | None]:
    """One resolution step. Returns (outcome, new_pending_clarification).

    `pending` is the clarification asked last turn ({kind, options,
    application?}); the user's reply is interpreted against it first.

    `policy` carries the hot-read resolution knobs; only
    {"default_environment": str | None} is read today (FR-CTX-8).
    """
    # -- unauthenticated: onboarding only, no checks (FR-ID-4) --------------
    if not claims.sub:
        return Onboarding(
            "I could not read a signed-in identity for this session, so I cannot "
            "resolve your applications or run checks. Sign in (in dev, pick an "
            "identity in the masthead) and ask again."
        ), None

    # -- explicit cluster scope: "attest <cluster>" (F6, FR-CTX-7) ----------
    m = _ATTEST_RE.search(user_text)
    if m:
        # The character class admits '.', so sentence-final punctuation rides
        # along ("attest prod-east-2." captured 'prod-east-2.') and turns an
        # exact name into a fuzzy multi-match. Strip it before resolving.
        query = m.group(1).strip(".,;:!?")
        lookup = await client.call("ocp__resolve_cluster", {"query": query})
        matches = lookup.get("matches", [])
        if len(matches) == 1:
            name = matches[0]["name"]
            return Resolved(ResolvedContext(
                scope="cluster", user_sub=claims.sub, user_name=claims.name,
                groups=claims.groups, clusters=[name],
                environment=matches[0].get("environment"),
            )), None
        if len(matches) > 1:
            options = [c["name"] for c in matches][:8]
            return Clarify(
                f"Several clusters match '{query}'. Which one?", options, "cluster"
            ), {"kind": "cluster", "options": options}
        return Onboarding(
            f"No cluster matched '{query}'. Try the full name, an alias, or "
            "ask me to list clusters for an environment or region."
        ), None

    # -- interpret a pending clarification reply ----------------------------
    chosen_app: dict[str, Any] | None = None
    chosen_env: str | None = _env_hint(user_text)
    if pending:
        picked = _pick_option(user_text, pending.get("options", []))
        if picked is None and pending.get("kind") == "cluster":
            # The reply may name a perfectly valid cluster that was not in
            # the truncated options list; the fleet resolver is the judge.
            lookup = await client.call(
                "ocp__resolve_cluster", {"query": user_text.strip().strip(".,;:!?")}
            )
            matches = lookup.get("matches", [])
            if len(matches) == 1:
                picked = matches[0]["name"]
        if picked is None:
            return Clarify(
                "I did not catch that; pick one of the options below.",
                pending.get("options", []), pending.get("kind", "application"),
            ), pending
        if pending.get("kind") == "application":
            chosen_app = next(
                (a for a in registry.get("applications", []) if a["application"] == picked), None
            )
        elif pending.get("kind") == "environment":
            chosen_env = picked
            chosen_app = next(
                (a for a in registry.get("applications", [])
                 if a["application"] == pending.get("application")), None
            )
        elif pending.get("kind") == "cluster":
            return Resolved(ResolvedContext(
                scope="cluster", user_sub=claims.sub, user_name=claims.name,
                groups=claims.groups, clusters=[picked],
            )), None

    # -- pick the application ------------------------------------------------
    outside = False
    if chosen_app is None:
        named = _named_app(user_text, registry)
        mine = candidate_apps(claims, registry)
        if named is not None:
            chosen_app = named
            outside = not any(a["application"] == named["application"] for a in mine)
        elif prior is not None and prior.application and prior.scope == "app":
            # Follow-up turn in an already-resolved thread: keep the context
            # unless the user changed it (FR-CTX-7 handled by naming/env hints).
            chosen_app = next(
                (a for a in registry.get("applications", [])
                 if a["application"] == prior.application), None
            )
            chosen_env = chosen_env or prior.environment
        elif len(mine) == 1:
            chosen_app = mine[0]
        elif len(mine) > 1:
            options = sorted(a["application"] for a in mine)
            return Clarify(
                f"You have {len(options)} registered applications. Which one first?",
                options, "application",
            ), {"kind": "application", "options": options}
        else:
            return Onboarding(
                "Your account has no registered applications yet, so I cannot resolve "
                "what to triage. Your team lead can register you in the application "
                "registry (owner-group mapping in fleet/applications.yaml). If you "
                "tell me the application's name, I can proceed now and note it is "
                "outside your registered set."
            ), None

    assert chosen_app is not None  # every earlier branch returned or assigned

    # -- verify placement fleet-wide (FR-CTX-2) ------------------------------
    app_label = chosen_app.get("app_label", chosen_app["application"])
    placements_result = await client.call("obs__find_app_placements", {"app_label": app_label})
    placements = placements_result.get("placements", [])
    if not placements:
        return Onboarding(
            f"The registry knows {chosen_app['application']}, but I found no running "
            "workloads for it anywhere in the fleet (kube_pod_labels returned no "
            "series). Check the app label or whether it is deployed."
        ), None

    envs = sorted({p["environment"] for p in placements})
    if chosen_env is None and prior is not None and prior.environment in envs:
        # FR-CTX-7: naming a different application mid-thread changes the
        # application, not the environment. Re-asking "prod or nonprod?" for
        # a thread that has been in prod for ten turns is an interrogation
        # the user already answered.
        chosen_env = prior.environment
    assumed = False
    if chosen_env is None and len(envs) > 1:
        # FR-CTX-8: most applications span environments, so asking here would
        # cost the F1 promise ("zero questions asked") its most common case.
        # The default answers it when the app actually runs there; when it does
        # not, or the policy is off, the question is still the only honest move.
        default_env = (policy or {}).get("default_environment")
        if default_env in envs:
            chosen_env, assumed = default_env, True
        else:
            return Clarify(
                f"{chosen_app['application']} runs in {len(envs)} environments. Which one?",
                envs, "environment",
            ), {"kind": "environment", "options": envs, "application": chosen_app["application"]}
    env = chosen_env if chosen_env in envs else (chosen_env or envs[0])
    scoped = [p for p in placements if p["environment"] == env] or placements

    instances = [
        AppInstance(cluster=p["cluster"], namespace=p["namespace"], environment=p["environment"])
        for p in scoped
    ]
    context = ResolvedContext(
        scope="app", user_sub=claims.sub, user_name=claims.name, groups=claims.groups,
        application=chosen_app["application"], app_label=app_label, environment=env,
        instances=instances, clusters=sorted({i.cluster for i in instances}),
        outside_registered_set=outside, environment_assumed=assumed,
    )
    log.info("context.resolved", application=context.application, environment=env,
             clusters=context.clusters, outside=outside, environment_assumed=assumed)
    return Resolved(context), None
