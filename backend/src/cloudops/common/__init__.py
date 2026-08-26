"""Shared infrastructure for every backend service.

Import seams (what the rest of the codebase is allowed to depend on):
  settings   - process configuration from environment variables
  config     - hot-reloadable YAML/Markdown configuration files
  logging    - structlog setup with trace correlation and redaction
  redact     - the single redaction layer (NFR-LOG-1)
  telemetry  - OpenTelemetry tracer setup (NFR-OBS-1..5)
"""
