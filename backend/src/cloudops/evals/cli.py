"""The eval entry point: run suites, score them, write the scorecard.

``python -m cloudops.evals [--suite <name>] [--mode fake|live] [--judge auto|off]
[--out <dir>]``

Two modes, one code path:

  fake  the analyst is ``FakeLlm``. Hermetic, deterministic, seconds - no
        Ollama, no Mongo, no cluster, no dev port. Only deterministic metrics
        are scored, because the narrative is a fixed sentence. This is the CI
        gate.
  live  the analyst is the model in config/models.yaml. Everything the fake
        mode asserts still holds, plus the scenario's ``live_only`` narrative
        expectations and the LLM judges.

Exit code is nonzero when any case fails, so this drops into CI as-is.

``run_evals`` is the importable form of the same thing, which is what the
pytest smoke calls: it exercises the harness in-process rather than shelling
out, so a broken harness fails the suite instead of failing silently.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

import structlog

from cloudops.common.config import load_yaml
from cloudops.common.logging import setup_logging
from cloudops.evals.judge import Judge, JudgeUnavailable, judge_config
from cloudops.evals.report import CaseResult, Scorecard, SuiteResult, TurnResult, write_outputs
from cloudops.evals.runner import EvalStack, eval_stack, run_case
from cloudops.evals.scorers import Metric, score_turn
from cloudops.evals.suite import Case, Mode, Suite, discover_suites, load_suite
from cloudops.evals.world import stack_key
from cloudops.testkit import REPO_ROOT

log = structlog.get_logger("cloudops.evals")

SUITES_DIR = REPO_ROOT / "backend" / "evals" / "suites"
JUDGES_DIR = REPO_ROOT / "backend" / "evals" / "judges"
DEFAULT_OUT = REPO_ROOT / "backend" / "evals" / "out"


@lru_cache(maxsize=1)
def _known_tools() -> tuple[str, ...]:
    """Every tool name the gateway can legitimately expose, from the committed
    servers.yaml allowlists. Handed to the judges so "invented tool name" can
    only ever mean a name outside this roster - a real tool the narrative
    mentions without having called it is commentary, not invention."""
    servers = load_yaml(REPO_ROOT / "config" / "gateway" / "servers.yaml") or {}
    return tuple(sorted(
        f"{s['prefix']}__{t}"
        for s in servers.get("servers", []) for t in s.get("allow_tools", [])
    ))


async def _judge_turn(
    judge: Judge, thresholds: dict[str, float], question: str, record: object
) -> list[Metric]:
    """Score one narrative against every judge whose threshold is declared."""
    from cloudops.evals.capture import TurnRecord

    assert isinstance(record, TurnRecord)
    if not (record.analyst_text or "").strip():
        # No analyst narrative this turn (runtime copy only): nothing to grade.
        return []
    metrics: list[Metric] = []
    for name, threshold in thresholds.items():
        verdict = await judge.score(
            name, question=question, narrative=record.analyst_text or record.narrative,
            evidence={**record.evidence(), "known_tools": list(_known_tools())},
        )
        metrics.append(Metric(
            metric=f"judge.{name}",
            passed=verdict.score >= threshold,
            detail=f"score {verdict.score:.2f} against threshold {threshold:.2f}",
            kind="judge",
            score=verdict.score,
            threshold=threshold,
            evidence=verdict.evidence(),
        ))
    return metrics


async def _score_case(
    case: Case, suite: Suite, mode: Mode, judge: Judge | None, stack: EvalStack
) -> CaseResult:
    """One scenario driven through a booted stack and scored. A scenario that
    blows up is a failed case, not a crashed run: the others still have
    something to say."""
    result = CaseResult(id=case.id, description=case.description)
    try:
        records = await run_case(stack, case)
    except Exception as exc:  # noqa: BLE001 - one bad scenario must not end the run
        log.exception("evals.case_failed", case=case.id)
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    for turn_spec, record in zip(case.turns, records, strict=True):
        metrics = score_turn(turn_spec, record, mode)
        # Judges grade the ANALYST's answer. A clarify or onboarding turn has
        # no analyst answer - its visible text is runtime-authored copy the
        # deterministic scorers already assert - so judging it would grade the
        # product's question-asking design instead of the model.
        judgeable = (turn_spec.expect.outcome or "resolved") == "resolved"
        if judge is not None and judgeable:
            thresholds = turn_spec.expect.live_only.judge.thresholds(suite.threshold_for(case))
            metrics += await _judge_turn(judge, thresholds, turn_spec.user, record)
        result.turns.append(TurnResult(
            index=record.index, user=record.user, outcome=record.outcome(),
            narrative=record.narrative, duration_s=record.duration_s,
            tool_calls=[c.tool for c in record.tool_calls],
            analyst_tool_calls=[c.tool for c in record.analyst_tool_calls],
            metrics=metrics,
        ))
    return result


async def run_evals(
    *,
    suites: list[str] | None = None,
    mode: Mode = "fake",
    judge_mode: str = "auto",
    out: Path | None = None,
    suites_dir: Path | None = None,
    judges_dir: Path | None = None,
) -> Scorecard:
    """Run the selected suites and return the scorecard (also written to disk)."""
    root = suites_dir or SUITES_DIR
    paths = discover_suites(root, suites)
    if not paths:
        raise FileNotFoundError(f"no suite files under {root}")

    judge: Judge | None = None
    if mode == "live" and judge_mode == "auto":
        try:
            judge = Judge(judge_config(), judges_dir or JUDGES_DIR)
        except JudgeUnavailable as exc:
            log.warning("evals.judge_disabled", reason=str(exc))
    scorecard = Scorecard(mode=mode, judge="on" if judge is not None else "off")

    with tempfile.TemporaryDirectory(prefix="cloudops-evals-") as tmp:
        workdir = Path(tmp)
        for path in paths:
            suite = load_suite(path)
            log.info("evals.suite", suite=suite.suite, cases=len(suite.cases), mode=mode)
            scorecard.suites.append(
                await _run_suite(suite, mode, judge, workdir))

    scorecard_path, report_path = write_outputs(scorecard, out or DEFAULT_OUT)
    log.info("evals.done", scorecard=str(scorecard_path), report=str(report_path),
             **{k: v for k, v in scorecard.totals().items()})
    return scorecard


async def _run_suite(suite: Suite, mode: Mode, judge: Judge | None, workdir: Path) -> SuiteResult:
    """One suite, booting a stack per distinct world rather than per case.

    Cases are kept in declaration order and consecutive ones that describe the
    same world, registry and inference target share a boot (see
    ``world.stack_key``). Standing up five services costs seconds, and a suite
    that deliberately holds its fleet still while varying the conversation
    would otherwise spend most of its run time in uvicorn.
    """
    result = SuiteResult(suite=suite.suite, description=suite.description)
    groups: list[tuple[str, list[Case]]] = []
    for case in suite.cases:
        if not case.runs_in(mode):
            result.cases.append(CaseResult(
                id=case.id, description=case.description,
                skipped=f"declared for {', '.join(case.modes)} mode only"))
            continue
        key = stack_key(case, mode)
        if groups and groups[-1][0] == key:
            groups[-1][1].append(case)
        else:
            groups.append((key, [case]))

    for key, cases in groups:
        case_dir = workdir / f"{suite.suite}-{key}"
        case_dir.mkdir(parents=True, exist_ok=True)
        try:
            async with eval_stack(cases[0], mode, case_dir) as stack:
                for case in cases:
                    result.cases.append(await _score_case(case, suite, mode, judge, stack))
        except Exception as exc:  # noqa: BLE001 - a bad boot fails its cases, not the run
            log.exception("evals.stack_failed", suite=suite.suite, key=key)
            scored = {c.id for c in result.cases}
            for case in cases:
                if case.id not in scored:
                    result.cases.append(CaseResult(
                        id=case.id, description=case.description,
                        error=f"stack did not boot: {type(exc).__name__}: {exc}"))
    # Declaration order, not group order, is what a reader expects to see.
    order = {case.id: i for i, case in enumerate(suite.cases)}
    result.cases.sort(key=lambda c: order.get(c.id, 0))
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cloudops.evals",
        description="Scenario-driven evaluation of the Cloud Operations Agent.",
    )
    parser.add_argument(
        "--suite", action="append", dest="suites", metavar="NAME",
        help="suite file stem under backend/evals/suites (repeatable; default: all)")
    parser.add_argument("--mode", choices=("fake", "live"), default="fake",
                        help="fake: hermetic FakeLlm (the CI gate). live: the configured model")
    parser.add_argument("--judge", choices=("auto", "off"), default="auto",
                        help="LLM-judged narrative metrics; only ever run in live mode")
    parser.add_argument("--out", type=Path, default=None,
                        help=f"output directory (default: {DEFAULT_OUT})")
    parser.add_argument("--suites-dir", type=Path, default=None,
                        help=f"where suite files live (default: {SUITES_DIR})")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # A run boots five services per world and every one of them logs at info.
    # The scorecard is the output; the log is the fallback, so it is quiet
    # unless the operator asks (CLOUDOPS_LOG_LEVEL=info).
    os.environ.setdefault("CLOUDOPS_LOG_LEVEL", "warning")
    setup_logging("cloudops.evals")
    scorecard = asyncio.run(run_evals(
        suites=args.suites, mode=args.mode, judge_mode=args.judge,
        out=args.out, suites_dir=args.suites_dir,
    ))
    totals = scorecard.totals()
    print(
        f"{totals['cases_passed']}/{totals['cases']} cases passed "
        f"({totals['cases_skipped']} skipped, {totals['metrics_failed']} "
        f"of {totals['metrics']} metrics failed)",
        file=sys.stderr,
    )
    for case in scorecard.cases:
        if case.passed:
            continue
        if case.error:
            print(f"  FAIL {case.id}: {case.error}", file=sys.stderr)
        for index, metric in case.failures():
            print(f"  FAIL {case.id} turn {index} {metric.metric}: {metric.detail}",
                  file=sys.stderr)
    return 0 if scorecard.passed else 1
