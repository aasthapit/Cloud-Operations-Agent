"""Scenario-driven evaluation of the Cloud Operations Agent.

The pytest suite asks whether the code does what it was written to do. This
package asks a different question: does the AGENT behave, across a
conversation, the way the product says it should. It boots the real stack per
scenario, sends real turns through the real code path, captures every emitted
event, and scores the result - deterministically always, and with LLM judges
when a real model wrote the narrative.

Entry point: ``python -m cloudops.evals`` (see ``cli``).
"""
