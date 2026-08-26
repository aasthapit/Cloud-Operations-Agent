"""The triage agent service (ADK), exposed over A2A.

Layout:
  models.py         typed check/report structures (the card payload contracts)
  protocol.py       the fenced-JSON envelope carrying typed payloads to the BFF
  gateway_client.py MCP client used by the deterministic check engine
  checks.py         the config-driven check engine (batteries -> reports)
  context.py        user context resolution (claims -> apps -> placements)
  prompts.py        hot-reloading instruction assembly
  model_factory.py  LiteLLM model construction (Ollama via OpenAI-compat)
  analyst.py        the LlmAgent used for narrative + interactive phases
  orchestrator.py   the deterministic root agent enforcing the triage gates
  a2a_app.py        wiring: config, telemetry, to_a2a()
"""
