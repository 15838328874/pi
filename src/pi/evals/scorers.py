"""Scorers: turn a RunResult into a pass/fail Verdict.

Deterministic scorers (file / command / tests) are preferred — they are
objective and reproducible. The LLM judge is the fallback for open-ended tasks
where no ground truth exists; it needs a structured rubric and calibration (see
LangSmith's guidance on LLM-as-judge).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from pi.evals.schema import RunResult, Task, Verdict
from pi.llm.base import TextDelta
from pi.llm.registry import DEFAULT_MODEL, resolve_chain
from pi.models import Message, Role, TextBlock


async def score(
    task: Task,
    result: RunResult,
    *,
    judge_provider=None,
    judge_model: str | None = None,
    runner=None,
) -> Verdict:
    """Turn a RunResult into a Verdict.

    ``runner`` (optional, additive): a CommandRunner used to execute the
    command/tests scorers' shells - the rollout pipeline passes the sandbox so
    that model-written code is never executed on the host. None = host shell
    (the original dev-tool behavior).
    """
    scorer = task.scorer
    if scorer.type == "file":
        return _score_file(task, result)
    if scorer.type == "command":
        return await _score_command(task, result, runner)
    if scorer.type == "tests":
        return await _score_tests(task, result, runner)
    if scorer.type == "judge":
        return await _score_judge(task, result, judge_provider, judge_model)
    return Verdict(
        task_id=task.id,
        passed=False,
        evidence=f"unknown scorer type {scorer.type!r}",
        scorer=scorer.type,
    )


def _score_file(task: Task, result: RunResult) -> Verdict:
    ws = Path(result.workspace)
    spec = task.scorer.files or {}
    failures: list[str] = []
    for relpath, pattern in spec.items():
        p = ws / relpath
        if not p.exists():
            failures.append(f"{relpath}: missing")
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if not re.search(pattern, text, re.DOTALL):
            failures.append(f"{relpath}: no match for /{pattern}/")
    passed = not failures
    evidence = "; ".join(failures) if failures else f"{len(spec)} file(s) matched"
    return Verdict(
        task_id=task.id,
        passed=passed,
        score=1.0 if passed else 0.0,
        evidence=evidence,
        scorer="file",
    )


async def _run_shell(cmd: str, cwd: str, runner=None) -> tuple[int, str]:
    """Run a scorer shell command. runner=None: host subprocess (dev tool);
    runner given: inside the sandbox (rollout pipeline)."""
    if runner is None:
        proc = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=300
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    result = await runner.run(cmd, cwd, timeout=300)
    return result.exit_code, result.output


async def _score_command(task: Task, result: RunResult, runner=None) -> Verdict:
    spec = task.scorer
    try:
        code, out = await _run_shell(spec.command or "", result.workspace, runner)
    except Exception as exc:  # noqa: BLE001 - subprocess unavailable -> fail
        return Verdict(
            task_id=task.id, passed=False, evidence=f"command failed: {exc}", scorer="command"
        )
    ok_exit = code == spec.expect_exit
    ok_contains = spec.expect_contains is None or spec.expect_contains in out
    passed = ok_exit and ok_contains
    evidence = f"exit={code}"
    if not ok_contains:
        evidence += f" (missing {spec.expect_contains!r} in output)"
    return Verdict(
        task_id=task.id, passed=passed, score=1.0 if passed else 0.0,
        evidence=evidence, scorer="command",
    )


async def _score_tests(task: Task, result: RunResult, runner=None) -> Verdict:
    spec = task.scorer
    paths = " ".join(spec.test_paths or [])
    cmd = f"python -m pytest -q {paths}".strip()
    try:
        code, out = await _run_shell(cmd, result.workspace, runner)
    except Exception as exc:  # noqa: BLE001
        return Verdict(
            task_id=task.id, passed=False, evidence=f"pytest failed: {exc}", scorer="tests"
        )
    passed_count, total_count = _parse_pytest_summary(out)
    return Verdict(
        task_id=task.id,
        passed=code == 0,
        score=1.0 if code == 0 else 0.0,
        evidence=f"pytest exit={code} ({passed_count or '?'}/{total_count or '?'})"
        if passed_count is not None
        else f"pytest exit={code}",
        scorer="tests",
        passed_count=passed_count,
        total_count=total_count,
    )


_PYTEST_RE = re.compile(r"(\d+)\s+passed")
_PYTEST_FAIL_RE = re.compile(r"(\d+)\s+(?:failed|error|errors)")


def _parse_pytest_summary(out: str) -> tuple[int | None, int | None]:
    """pytest -q summary line ('3 passed, 1 failed in 0.12s') -> (passed, total).

    Returns (None, None) when the summary cannot be parsed (e.g. no tests ran,
    or output truncated)."""
    m = _PYTEST_RE.search(out)
    if m is None:
        return None, None
    passed = int(m.group(1))
    failed = sum(int(x) for x in _PYTEST_FAIL_RE.findall(out))
    return passed, passed + failed


async def _score_judge(
    task: Task, result: RunResult, judge_provider, judge_model: str | None
) -> Verdict:
    provider = judge_provider or resolve_chain(judge_model or task.model or DEFAULT_MODEL)
    rubric = task.scorer.rubric or "The agent should complete the task correctly."
    prompt = (
        "You are an evaluator. Judge whether the agent completed the task.\n\n"
        f"TASK:\n{task.prompt}\n\n"
        f"RUBRIC:\n{rubric}\n\n"
        f"AGENT FINAL OUTPUT:\n{_final_text(result)}\n\n"
        'Reply with exactly one word "PASS" or "FAIL", then a one-line reason.'
    )
    messages = [Message(role=Role.user, blocks=[TextBlock(text=prompt)])]
    text = ""
    async for ev in provider.stream("You are an evaluation judge.", messages, []):
        if isinstance(ev, TextDelta):
            text += ev.text
    passed = "PASS" in text.upper()
    return Verdict(
        task_id=task.id,
        passed=passed,
        score=1.0 if passed else 0.0,
        evidence=text.strip()[:400],
        scorer="judge",
    )


def _final_text(result: RunResult) -> str:
    for e in reversed(result.trajectory.get("events", [])):
        if e.get("type") == "LlmCall":
            return e.get("text", "") or "(no text)"
    return "(no output)"
