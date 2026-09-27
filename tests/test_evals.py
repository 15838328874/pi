"""Tests for the eval harness (P4): runner, scorers, report, loader."""

from __future__ import annotations

import asyncio
import json

from pi.evals.load import load_task_set
from pi.evals.report import diff, summarize
from pi.evals.runner import run_task
from pi.evals.schema import (
    EnvSpec,
    RunResult,
    ScorerSpec,
    Task,
    TaskReport,
)
from pi.evals.scorers import score
from pi.llm.fake import FakeProvider
from pi.models import TextBlock, ToolCallBlock


def _write_task() -> Task:
    return Task(
        id="write-hello",
        prompt="create hello.txt with 'hello world'",
        category="happy_path",
        env=EnvSpec(),
        scorer=ScorerSpec(type="file", files={"hello.txt": "hello world"}),
    )


def test_file_scorer_end_to_end(tmp_path):
    provider = FakeProvider(
        model="demo",
        responses=[
            [
                ToolCallBlock(
                    id="t1",
                    name="write",
                    arguments=json.dumps({"path": "hello.txt", "content": "hello world"}),
                )
            ],
            [TextBlock(text="done")],
        ],
    )
    task = _write_task()

    async def main():
        result = await run_task(task, provider=provider, workspace=tmp_path)
        verdict = await score(task, result)
        return result, verdict

    result, verdict = asyncio.run(main())
    assert verdict.passed
    assert verdict.scorer == "file"
    assert result.usage_output == 2  # two LLM turns
    # P1 trajectory was captured by the runner
    assert result.trajectory["events"][0]["type"] == "RunStarted"
    assert result.trajectory["events"][-1]["type"] == "RunFinished"


def test_file_scorer_reports_failure(tmp_path):
    provider = FakeProvider(model="demo", responses=[[TextBlock(text="did nothing")]])
    task = _write_task()

    async def main():
        result = await run_task(task, provider=provider, workspace=tmp_path)
        return await score(task, result)

    verdict = asyncio.run(main())
    assert not verdict.passed
    assert "missing" in verdict.evidence


def test_judge_scorer(tmp_path):
    task = Task(
        id="open-task",
        prompt="summarize the codebase",
        scorer=ScorerSpec(type="judge", rubric="must mention key points"),
    )
    judge = FakeProvider(model="judge", responses=[[TextBlock(text="PASS - good summary")]])
    result = RunResult(
        task_id="open-task",
        trajectory={"events": [{"type": "LlmCall", "text": "the summary"}]},
        workspace=str(tmp_path),
    )

    async def main():
        return await score(task, result, judge_provider=judge)

    verdict = asyncio.run(main())
    assert verdict.passed
    assert verdict.scorer == "judge"


def test_report_summarize_and_diff():
    a = TaskReport(
        task_id="a", category="happy_path", tags=[], passed=True, score=1.0,
        usage_input=1, usage_output=1, turns=1, latency_ms=1, evidence="ok",
    )
    b = TaskReport(
        task_id="b", category="happy_path", tags=[], passed=False, score=0.0,
        usage_input=1, usage_output=1, turns=1, latency_ms=1, evidence="bad",
    )
    report = summarize([a, b])
    assert report.total == 2
    assert report.passed == 1
    assert report.pass_rate == 0.5
    assert report.per_category["happy_path"]["pass_rate"] == 0.5

    d = diff(report, report, "model-a", "model-b")
    assert d["delta"] == 0.0


def test_load_task_set(tmp_path):
    (tmp_path / "t1.json").write_text(
        json.dumps(
            {
                "id": "t1",
                "prompt": "do it",
                "scorer": {"type": "file", "files": {"a.txt": "x"}},
            }
        ),
        encoding="utf-8",
    )
    tasks = load_task_set(tmp_path)
    assert len(tasks) == 1
    assert tasks[0].id == "t1"
    assert tasks[0].scorer.type == "file"
