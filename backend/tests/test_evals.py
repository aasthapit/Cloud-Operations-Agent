"""The eval harness's own smoke test (NFR-QE-1).

The harness is a piece of software like any other, and a broken one fails
quietly: it reports green because it never ran the scenario. So CI runs one
small suite through the PUBLIC entry point - the same ``run_evals`` the CLI
calls, in-process rather than as a subprocess - and asserts the shape of what
comes back and that the scenario really passed.

In-process on purpose. A subprocess would hide the failure mode this test
exists for (an import error, a boot that hangs, an empty scorecard) behind an
exit code, and it would cost a second interpreter start for no extra fidelity.
"""

from __future__ import annotations

import json
from pathlib import Path

from cloudops.evals.cli import SUITES_DIR, run_evals


async def test_smoke_suite_passes_in_fake_mode(tmp_path: Path) -> None:
    """One scenario, booted and scored, with the scorecard on disk."""
    scorecard = await run_evals(suites=["smoke"], mode="fake", judge_mode="off", out=tmp_path)

    assert scorecard.mode == "fake"
    assert scorecard.judge == "off"
    totals = scorecard.totals()
    assert totals == {
        "suites": 1, "cases": 1, "cases_passed": 1, "cases_failed": 0,
        "cases_skipped": 0, "turns": 1, "metrics": totals["metrics"], "metrics_failed": 0,
    }
    assert totals["metrics"] >= 5, "a case that scores almost nothing is not a smoke test"

    [case] = scorecard.cases
    assert case.id == "resolves-and-reports"
    assert case.passed and not case.error
    [turn] = case.turns
    assert turn.outcome == "resolved"
    # The deterministic phases gathered the evidence, so the analyst had no
    # reason to reach for a tool - and the check engine plainly did.
    assert turn.analyst_tool_calls == []
    assert any(name.startswith("ocp__") for name in turn.tool_calls)
    assert any(name.startswith("reg__") for name in turn.tool_calls)


async def test_scorecard_and_report_land_on_disk(tmp_path: Path) -> None:
    """Both outputs are written, and the machine one round-trips as JSON."""
    await run_evals(suites=["smoke"], mode="fake", judge_mode="off", out=tmp_path)

    payload = json.loads((tmp_path / "scorecard.json").read_text())
    assert payload["pass"] is True
    assert payload["mode"] == "fake"
    [suite] = payload["suites"]
    assert suite["suite"] == "smoke"
    metrics = {m["metric"] for c in suite["cases"] for t in c["turns"] for m in t["metrics"]}
    # The invariant that is never declared in a scenario and always scored.
    assert "protocol.no_model_fences" in metrics
    assert {"outcome", "context.application", "attestation.acm-spoke-1a"} <= metrics

    report = (tmp_path / "report.md").read_text()
    assert "# Cloud Operations Agent - evaluation report" in report
    assert "resolves-and-reports" in report


def test_every_committed_suite_parses() -> None:
    """A suite file that does not parse is a scenario that silently never ran."""
    from cloudops.evals.suite import discover_suites, load_suite

    paths = discover_suites(SUITES_DIR)
    assert len(paths) >= 4
    for path in paths:
        suite = load_suite(path)
        assert suite.cases, f"{path.name} declares no cases"
        for case in suite.cases:
            assert case.turns, f"{suite.suite}/{case.id} declares no turns"
            assert case.persona.sub or case.id, "a case needs an identity to run as"
