"""OpenTelemetry setup shared by every backend service (NFR-OBS-1..5).

Design:
- Exporters are optional (NFR-OBS-4): with OTEL_EXPORTER_OTLP_ENDPOINT set,
  spans export via OTLP/HTTP (the compose file in deploy/ provides a
  collector + Jaeger); without it, spans still exist in-process so log
  lines keep their trace ids, and nothing is exported.
- Propagation: W3C traceparent via the global propagator. HTTP hops inject/
  extract explicitly with `inject_headers` / `extract_context` because the
  MCP transport hides its HTTP client; the agent's McpToolset header
  provider and the gateway's ASGI middleware are the two seams.
- The agent service calls setup_telemetry BEFORE ADK initializes, so ADK's
  built-in GenAI spans land on our provider (ADK's maybe_set_otel_providers
  is a no-op when a global provider already exists).

Gateway-to-domain-MCP hop: MCP v1 client sessions pin HTTP headers at
connect time, so per-call trace context rides the MCP request's _meta
instead (gateway sends traceparent + x-thread-id + x-user-sub as extra
meta fields; mcp_servers.shared._caller_context extracts them), keeping
one distributed trace per turn across all five services (NFR-OBS-1).
"""

from __future__ import annotations

import os

from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_initialized = False


def setup_telemetry(service_name: str) -> trace.Tracer:
    """Install a tracer provider for this process (idempotent)."""
    global _initialized
    if not _initialized:
        provider = TracerProvider(
            resource=Resource.create({"service.name": service_name, "service.namespace": "cloudops"})
        )
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
        if endpoint:
            # Imported lazily so the exporter package is only required when used.
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        _initialized = True
    return trace.get_tracer(service_name)


def inject_headers(headers: dict[str, str] | None = None) -> dict[str, str]:
    """Return headers carrying the current trace context (traceparent)."""
    carrier: dict[str, str] = dict(headers or {})
    propagate.inject(carrier)
    return carrier


def extract_context(headers: dict[str, str]) -> Context:
    """Build an OTel context from incoming HTTP headers (case-insensitive)."""
    lowered = {k.lower(): v for k, v in headers.items()}
    return propagate.extract(lowered)
