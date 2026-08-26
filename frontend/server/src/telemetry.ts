/**
 * OpenTelemetry for the web tier (NFR-OBS-1/4).
 * Always installs a tracer provider so spans exist and trace ids can be
 * logged; exports OTLP only when OTEL_EXPORTER_OTLP_ENDPOINT is set (the
 * deploy/ compose file provides a collector + Jaeger). W3C traceparent is
 * injected on the BFF -> agent hop in a2a.ts, joining the browser turn to
 * the backend trace.
 */
import { context, propagation, trace } from "@opentelemetry/api";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { Resource } from "@opentelemetry/resources";
import { BatchSpanProcessor, NodeTracerProvider } from "@opentelemetry/sdk-trace-node";
import { ATTR_SERVICE_NAME } from "@opentelemetry/semantic-conventions";

export function setupTelemetry(): void {
  const endpoint = (process.env.OTEL_EXPORTER_OTLP_ENDPOINT ?? "").trim();
  const provider = new NodeTracerProvider({
    resource: new Resource({ [ATTR_SERVICE_NAME]: "cloudops.bff" }),
  });
  if (endpoint) {
    provider.addSpanProcessor(new BatchSpanProcessor(new OTLPTraceExporter()));
  }
  provider.register();
}

export const tracer = () => trace.getTracer("cloudops.bff");

/** Headers carrying the current trace context (for the A2A hop). */
export function injectTraceHeaders(headers: Record<string, string>): Record<string, string> {
  propagation.inject(context.active(), headers);
  return headers;
}
