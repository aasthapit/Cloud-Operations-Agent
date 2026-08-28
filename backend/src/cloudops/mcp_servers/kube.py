"""Kubernetes transport for the live backends.

Role: turn ONE CLUSTER RECORD from the fleet registry into a read-only HTTPS
client against that cluster's API server, plus the two derived access paths
the live backends need:

- ``get`` / ``get_text`` for the Kubernetes API itself
- ``prom`` for the cluster's in-cluster Prometheus, reached through the API
  server's SERVICE PROXY. Going through the proxy means live mode needs no
  port-forward, no NodePort, and no second set of credentials: the same
  identity that reads the API also reads Prometheus.

Three ways a record can authenticate (docs/design/LIVE-CUTOVER.md, "MongoDB"):

- ``{"type": "kubeconfig", "context": ...}`` - the local kind dev fleet.
  Credentials stay in the kubeconfig on disk (KUBECONFIG, else
  ~/.kube/config) and never enter the registry.
- ``{"type": "token", "token": ...}`` - a service-account or personal bearer
  token held in the registry.
- ``{"type": "basic", "username": ..., "password": ...}`` - what a real
  fleet's operators actually have. OpenShift does not accept basic auth on
  the API server, so the credentials are exchanged for a bearer token
  through the cluster's own OAuth server: discover
  ``/.well-known/oauth-authorization-server``, then run the
  ``openshift-challenging-client`` implicit flow, which answers a 302 whose
  Location FRAGMENT carries the token. Tokens are cached per (api_url, user)
  until they expire and re-exchanged on a 401, so a rotated password or a
  revoked token self-heals on the next call instead of failing forever.

Seams: this module knows nothing about fleet naming or tool shapes. The fleet
registry (cloudops.registry) owns cluster records; the backends own the
shapes.

Credentials are never logged and never appear in a tool result (FR-MCP-7,
NFR-LOG-3); the exchange below logs the cluster and the outcome, never the
password or the token. Only GET requests are ever issued (NFR-SEC-1).
"""

from __future__ import annotations

import atexit
import base64
import contextlib
import os
import ssl
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import structlog

from cloudops.common.config import load_yaml

log = structlog.get_logger("cloudops.kube")

# The single sentence every OpenShift-only tool answers with when it is run
# against a vanilla Kubernetes cluster. Kept here so the wording is identical
# across tools and greppable from tests.
VANILLA_K8S_REASON = "not applicable: vanilla Kubernetes cluster (no OpenShift APIs)"


def not_applicable(**neutral: Any) -> dict[str, Any]:
    """An OpenShift-only tool result for a cluster that has no such API.

    The contract (see README, 'Live mode against a local kind fleet'): the
    result keeps the mock shape with health-neutral values, so no attestation
    rule triggers and the check lands as a plain pass, and carries
    ``applicable: false`` plus a reason so a reader (or a future battery rule)
    can tell "nothing to report" apart from "everything is fine".
    """
    return {"applicable": False, "not_applicable_reason": VANILLA_K8S_REASON, **neutral}


class KubeConfigError(RuntimeError):
    """The kubeconfig cannot satisfy the requested context."""


def _kubeconfig_path() -> Path:
    """KUBECONFIG (first entry) or ~/.kube/config. Environment only: live-mode
    credentials never come from the committed config plane (FR-CFG-4)."""
    raw = os.environ.get("KUBECONFIG", "")
    for candidate in [p for p in raw.split(os.pathsep) if p] or [str(Path.home() / ".kube" / "config")]:
        path = Path(candidate).expanduser()
        if path.is_file():
            return path
    raise KubeConfigError("no kubeconfig found (set KUBECONFIG or create ~/.kube/config)")


_TEMP_FILES: list[str] = []


def _materialize(data_b64: str | None, path: str | None, suffix: str) -> str | None:
    """PEM data from a kubeconfig, as a file path httpx can use.

    httpx (like OpenSSL) wants client certs and CA bundles as files, but
    kubeconfig carries them inline. Inline data is written to a private
    temporary file that is unlinked at process exit.
    """
    if path:
        return str(Path(path).expanduser())
    if not data_b64:
        return None
    fd, name = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as fh:
        fh.write(base64.b64decode(data_b64))
    os.chmod(name, 0o600)
    _TEMP_FILES.append(name)
    return name


@atexit.register
def _cleanup_temp_files() -> None:
    for name in _TEMP_FILES:
        with contextlib.suppress(OSError):
            os.unlink(name)


def _materialize_pem(pem: str | None) -> str | None:
    """Inline PEM text from a cluster record, as a file path httpx can use."""
    if not pem or not pem.strip():
        return None
    fd, name = tempfile.mkstemp(suffix=".crt")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(pem)
    os.chmod(name, 0o600)
    _TEMP_FILES.append(name)
    return name


def _ssl_context(ca_file: str | None, insecure: bool) -> ssl.SSLContext:
    """One TLS context for a cluster, honouring an explicit CA or none at all.

    Since httpx 0.28 TLS material is supplied as one SSLContext. Passing a
    client certificate any other way is silently ignored, which shows up as
    an authenticated-as-anonymous 403 rather than a TLS error, so building
    the context in one place keeps both halves together.
    """
    context = ssl.create_default_context(cafile=ca_file)
    if insecure:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


# ---------------------------------------------------------------------------
# OpenShift OAuth: username/password -> bearer token
# ---------------------------------------------------------------------------

# The client id OpenShift reserves for credential challenges. It is a public,
# documented constant, not a secret.
CHALLENGING_CLIENT = "openshift-challenging-client"

# Re-exchange this many seconds before the token actually expires, so a call
# in flight cannot land on the far side of the expiry.
TOKEN_EXPIRY_MARGIN_S = 60.0

# (api_url, username) -> (token, expires_at epoch seconds).
_TOKEN_CACHE: dict[tuple[str, str], tuple[str, float]] = {}


class OAuthExchangeError(RuntimeError):
    """The cluster's OAuth server would not exchange these credentials."""


def _access_token_from_location(location: str) -> tuple[str, float]:
    """Pull access_token and expires_in out of an implicit-flow redirect.

    The implicit flow returns them in the URL FRAGMENT, not the query string,
    which is why this cannot simply read response params.
    """
    fragment = urlparse(location).fragment
    params = parse_qs(fragment)
    token = (params.get("access_token") or [""])[0]
    if not token:
        error = (params.get("error_description") or params.get("error") or ["no access_token"])[0]
        raise OAuthExchangeError(f"OAuth redirect carried no access token: {error}")
    try:
        expires_in = float((params.get("expires_in") or ["3600"])[0])
    except ValueError:
        expires_in = 3600.0
    return token, expires_in


def exchange_basic_for_token(
    api_url: str,
    username: str,
    password: str,
    *,
    ssl_context: ssl.SSLContext | None = None,
    timeout_s: float = 20.0,
    transport: httpx.BaseTransport | None = None,
) -> tuple[str, float]:
    """Trade a username and password for a bearer token and its lifetime.

    Two hops, both GETs:

    1. ``{api_url}/.well-known/oauth-authorization-server`` names the
       cluster's ``authorization_endpoint``. It is discovered rather than
       assumed because the OAuth route is not always on the API host.
    2. ``{authorization_endpoint}?client_id=openshift-challenging-client&
       response_type=token`` with HTTP Basic credentials and an
       ``X-CSRF-Token`` header, which is what makes OpenShift answer the
       challenge instead of redirecting to a login page. The 302's Location
       fragment carries the token.

    ``transport`` exists so tests can drive the whole flow through an
    httpx.MockTransport; production callers leave it unset.
    """
    http = httpx.Client(
        transport=transport,
        verify=ssl_context or ssl.create_default_context(),
        timeout=timeout_s,
        follow_redirects=False,
    )
    try:
        discovery = http.get(
            f"{api_url.rstrip('/')}/.well-known/oauth-authorization-server",
            headers={"Accept": "application/json"},
        )
        discovery.raise_for_status()
        endpoint = str(discovery.json().get("authorization_endpoint") or "")
        if not endpoint:
            raise OAuthExchangeError(
                "the cluster's OAuth metadata has no authorization_endpoint")

        response = http.get(
            endpoint,
            params={"client_id": CHALLENGING_CLIENT, "response_type": "token"},
            headers={"X-CSRF-Token": "1", "Accept": "application/json"},
            auth=(username, password),
            follow_redirects=False,
        )
    finally:
        http.close()

    location = response.headers.get("location", "")
    if response.status_code not in (301, 302, 303, 307, 308) or not location:
        raise OAuthExchangeError(
            f"OAuth challenge returned {response.status_code} with no redirect; "
            "check the username and password in the cluster record"
        )
    return _access_token_from_location(location)


def basic_token(
    api_url: str,
    username: str,
    password: str,
    *,
    force: bool = False,
    ssl_context: ssl.SSLContext | None = None,
    timeout_s: float = 20.0,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """A cached bearer token for these credentials.

    ``force=True`` bypasses (and replaces) the cache: it is the 401 path,
    where the cached token is still unexpired but the cluster has stopped
    honouring it.
    """
    key = (api_url, username)
    cached = _TOKEN_CACHE.get(key)
    if cached and not force and cached[1] > time.time():
        return cached[0]
    token, expires_in = exchange_basic_for_token(
        api_url, username, password, ssl_context=ssl_context, timeout_s=timeout_s,
        transport=transport)
    _TOKEN_CACHE[key] = (token, time.time() + max(expires_in - TOKEN_EXPIRY_MARGIN_S, 0.0))
    # Never log the token or the password: the cluster and the lifetime are
    # the only useful facts anyway.
    log.info("kube.oauth_exchanged", api_url=api_url, expires_in=expires_in, forced=force)
    return token


def clear_token_cache() -> None:
    """Drop every cached OAuth token (tests, and credential rotation)."""
    _TOKEN_CACHE.clear()


class KubeClient:
    """Read-only client for one cluster record from the fleet registry.

    The record is ``{"name", "api_url", "auth": {...}}`` as stored in the
    registry's `clusters` collection. Only ``auth`` and ``api_url`` are read
    here; everything else about a cluster is the registry's business.
    """

    def __init__(
        self,
        cluster: dict[str, Any] | str,
        timeout_s: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        # A bare string is still accepted as "the kubeconfig context", which
        # keeps ad-hoc callers (smoke scripts, a REPL) working without them
        # having to fabricate a registry record.
        record: dict[str, Any] = (
            {"name": cluster, "auth": {"type": "kubeconfig", "context": cluster}}
            if isinstance(cluster, str) else dict(cluster)
        )
        self.cluster = str(record.get("name") or "")
        self.timeout_s = timeout_s
        # Test seam only: an httpx.MockTransport lets the whole auth flow
        # (discovery, challenge, retry) run without a cluster.
        self._transport = transport
        auth = dict(record.get("auth") or {})
        kind = str(auth.get("type") or "kubeconfig")

        # Set by the basic path only; its presence is what enables the
        # re-exchange-on-401 retry below.
        self._basic: dict[str, Any] | None = None
        self.context: str | None = None

        if kind == "kubeconfig":
            self._init_kubeconfig(str(auth.get("context") or record.get("context") or ""), timeout_s)
        elif kind in ("token", "basic"):
            self._init_endpoint(record, auth, kind, timeout_s)
        else:
            raise KubeConfigError(
                f"cluster {self.cluster!r} has unsupported auth type {kind!r}; "
                "expected kubeconfig, token, or basic"
            )

    # -- construction paths --------------------------------------------------

    def _init_kubeconfig(self, context: str, timeout_s: float) -> None:
        if not context:
            raise KubeConfigError(
                f"cluster {self.cluster!r} uses kubeconfig auth but names no context")
        self.context = context
        config = load_yaml(_kubeconfig_path())
        ctx = self._named(config, "contexts", context, "context")
        cluster = self._named(config, "clusters", ctx["cluster"], "cluster")
        user = self._named(config, "users", ctx["user"], "user")

        self.server = str(cluster["server"]).rstrip("/")
        ca = _materialize(cluster.get("certificate-authority-data"),
                          cluster.get("certificate-authority"), ".crt")
        cert_file = _materialize(user.get("client-certificate-data"),
                                 user.get("client-certificate"), ".crt")
        key_file = _materialize(user.get("client-key-data"), user.get("client-key"), ".key")

        headers = {"Accept": "application/json"}
        token = user.get("token") or self._token_file(user)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if not (cert_file and key_file) and not token:
            raise KubeConfigError(
                f"context {context!r} has neither client certificates nor a token; "
                "exec-based credential plugins are not supported by the live backend"
            )

        ssl_context = _ssl_context(ca, bool(cluster.get("insecure-skip-tls-verify")))
        if cert_file and key_file:
            ssl_context.load_cert_chain(cert_file, key_file)
        self._client = httpx.Client(
            base_url=self.server, verify=ssl_context, headers=headers, timeout=timeout_s,
            transport=self._transport,
        )

    def _init_endpoint(
        self, record: dict[str, Any], auth: dict[str, Any], kind: str, timeout_s: float
    ) -> None:
        """Token or basic auth against an api_url named by the record."""
        api_url = str(auth.get("api_url") or record.get("api_url") or "").rstrip("/")
        if not api_url:
            raise KubeConfigError(
                f"cluster {self.cluster!r} uses {kind} auth but has no api_url")
        self.server = api_url
        ca_file = _materialize_pem(auth.get("ca") or record.get("ca"))
        insecure = bool(auth.get("insecure_skip_tls_verify") or record.get("insecure_skip_tls_verify"))
        self._ssl = _ssl_context(ca_file, insecure)

        if kind == "token":
            token = str(auth.get("token") or "")
            if not token:
                raise KubeConfigError(f"cluster {self.cluster!r} uses token auth but has no token")
        else:
            self._basic = {
                "api_url": api_url,
                "username": str(auth.get("username") or ""),
                "password": str(auth.get("password") or ""),
            }
            if not self._basic["username"] or not self._basic["password"]:
                raise KubeConfigError(
                    f"cluster {self.cluster!r} uses basic auth but has no username/password")
            token = self._exchange()

        self._client = httpx.Client(
            base_url=api_url, verify=self._ssl, timeout=timeout_s,
            transport=self._transport,
            headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
        )

    def _exchange(self, force: bool = False) -> str:
        assert self._basic is not None
        return basic_token(
            self._basic["api_url"], self._basic["username"], self._basic["password"],
            force=force, ssl_context=self._ssl, timeout_s=self.timeout_s,
            transport=self._transport,
        )

    @staticmethod
    def _token_file(user: dict[str, Any]) -> str | None:
        path = user.get("tokenFile")
        return Path(path).expanduser().read_text().strip() if path else None

    @staticmethod
    def _named(config: dict[str, Any], section: str, name: str, key: str) -> dict[str, Any]:
        for entry in config.get(section) or []:
            if entry.get("name") == name:
                return dict(entry.get(key) or {})
        raise KubeConfigError(f"kubeconfig has no {section[:-1]} named {name!r}")

    # -- Kubernetes API ------------------------------------------------------

    def _get(self, path: str, **kwargs: Any) -> httpx.Response:
        """One GET, with a single re-exchange-and-retry on 401.

        A 401 on a basic-auth cluster means the cached token was revoked or
        the cluster restarted its OAuth server; exchanging again is cheap and
        turns a hard failure into a transparent recovery. Every other status
        is left to the caller's raise_for_status.
        """
        response = self._client.get(path, **kwargs)
        if response.status_code == 401 and self._basic is not None:
            token = self._exchange(force=True)
            self._client.headers["Authorization"] = f"Bearer {token}"
            response = self._client.get(path, **kwargs)
        return response

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        resp = self._get(path, params={k: v for k, v in params.items() if v is not None})
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    def get_text(self, path: str, **params: Any) -> str:
        resp = self._get(path, params=params, headers={"Accept": "text/plain"})
        resp.raise_for_status()
        return resp.text

    def items(self, path: str, **params: Any) -> list[dict[str, Any]]:
        """List call returning just ``items`` (empty when the API group is absent)."""
        return list(self.get(path, **params).get("items") or [])

    # -- Prometheus through the API server's service proxy -------------------

    def prom(self, subpath: str, **params: Any) -> dict[str, Any]:
        """GET against the in-cluster Prometheus HTTP API.

        subpath is relative to Prometheus' own /api/v1, e.g. 'query' or
        'alerts'. Raises httpx.HTTPError when the cluster has no Prometheus,
        which callers turn into an honest 'metric unavailable', never a guess.
        """
        base = (
            "/api/v1/namespaces/monitoring/services/prometheus:9090/proxy/api/v1/"
        )
        return self.get(base + subpath.lstrip("/"), **params)

    def close(self) -> None:
        self._client.close()
