"""Kubernetes transport for the live backends.

Role: turn one kubeconfig context into a read-only HTTPS client against that
cluster's API server, plus the two derived access paths the live backends
need:

- ``get`` / ``get_text`` for the Kubernetes API itself
- ``prom`` for the cluster's in-cluster Prometheus, reached through the API
  server's SERVICE PROXY. Going through the proxy means live mode needs no
  port-forward, no NodePort, and no second set of credentials: the same
  kubeconfig identity that reads the API also reads Prometheus.

Seams: this module knows nothing about fleet naming or tool shapes. The fleet
registry (cloudops.mcp_servers.live_fleet) maps cluster names to contexts;
the backends own the shapes.

Credentials are read from the kubeconfig on disk only (KUBECONFIG, else
~/.kube/config) and never logged (FR-MCP-7, NFR-LOG-3). Only GET requests are
ever issued (NFR-SEC-1).
"""

from __future__ import annotations

import atexit
import base64
import contextlib
import os
import ssl
import tempfile
from pathlib import Path
from typing import Any

import httpx

from cloudops.common.config import load_yaml

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


class KubeClient:
    """Read-only client for one kubeconfig context."""

    def __init__(self, context: str, timeout_s: float = 20.0) -> None:
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

        # Since httpx 0.28 TLS material is supplied as one SSLContext. Passing the client
        # certificate any other way is silently ignored, which shows up as an
        # authenticated-as-anonymous 403 rather than a TLS error, so build the
        # context here and keep both halves in one place.
        ssl_context = ssl.create_default_context(cafile=ca)
        if cluster.get("insecure-skip-tls-verify"):
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        if cert_file and key_file:
            ssl_context.load_cert_chain(cert_file, key_file)

        self._client = httpx.Client(
            base_url=self.server, verify=ssl_context, headers=headers, timeout=timeout_s,
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

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        resp = self._client.get(path, params={k: v for k, v in params.items() if v is not None})
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    def get_text(self, path: str, **params: Any) -> str:
        resp = self._client.get(path, params=params, headers={"Accept": "text/plain"})
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
