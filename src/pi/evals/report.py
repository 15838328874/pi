"""Report: summarize task results, compare A/B configs."""

from __future__ import annotations

from pi.evals.schema import EvalReport, RunResult, Task, TaskReport, Verdict


def make_task_report(task: Task, result: RunResult, verdict: Verdict) -> TaskReport:
    return TaskReport(
        task_id=task.id,
        category=task.category,
        tags=task.tags,
        passed=verdict.passed,
        score=verdict.score,
        usage_input=result.usage_input,
        usage_output=result.usage_output,
        turns=result.turns,
        latency_ms=result.latency_ms,
        evidence=verdict.evidence,
        error=result.error,
    )


def summarize(reports: list[TaskReport]) -> EvalReport:
    total = len(reports)
    passed = sum(1 for r in reports if r.passed)
    per_category: dict = {}
    for r in reports:
        c = per_category.setdefault(r.category, {"total": 0, "passed": 0})
        c["total"] += 1
        if r.passed:
            c["passed"] += 1
    per_category = {
        k: {"total": v["total"], "passed": v["passed"], "pass_rate": v["passed"] / v["total"]}
        for k, v in per_category.items()
    }
    return EvalReport(
        total=total,
        passed=passed,
        pass_rate=passed / total if total else 0.0,
        per_category=per_category,
        tasks=reports,
    )


def diff(a: EvalReport, b: EvalReport, label_a: str, label_b: str) -> dict:
    return {
        label_a: {"passed": a.passed, "total": a.total, "pass_rate": round(a.pass_rate, 4)},
        label_b: {"passed": b.passed, "total": b.total, "pass_rate": round(b.pass_rate, 4)},
        "delta": round(b.pass_rate - a.pass_rate, 4),
    }


def format_report(report: EvalReport) -> str:
    lines = [f"pass: {report.passed}/{report.total} ({report.pass_rate:.1%})"]
    for cat, info in report.per_category.items():
        lines.append(f"  [{cat}] {info['passed']}/{info['total']}")
    for r in report.tasks:
        mark = "PASS" if r.passed else "FAIL"
        err = f" error={r.error}" if r.error else ""
        lines.append(
            f"  {mark}  {r.task_id}  ({r.category})  "
            f"in={r.usage_input} out={r.usage_output} turns={r.turns}{err}"
        )
        if not r.passed:
            lines.append(f"        {r.evidence[:160]}")
    return "\n".join(lines)
