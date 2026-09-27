"""pi-py eval CLI: run / diff."""

from __future__ import annotations

import asyncio
import json

from pi.evals.load import load_task_set
from pi.evals.report import diff, format_report, make_task_report, summarize
from pi.evals.runner import run_task
from pi.evals.schema import EvalReport
from pi.evals.scorers import score


async def _run_set(tasks, model=None) -> EvalReport:
    reports = []
    for task in tasks:
        result = await run_task(task, model=model)
        verdict = await score(task, result)
        reports.append(make_task_report(task, result, verdict))
    return summarize(reports)


def cmd_eval(args) -> int:
    if args.eval_command == "run":
        tasks = load_task_set(args.tasks)
        report = asyncio.run(_run_set(tasks, model=args.model))
        print(format_report(report))
        return 0
    if args.eval_command == "diff":
        tasks = load_task_set(args.tasks)
        a = asyncio.run(_run_set(tasks, model=args.model_a))
        b = asyncio.run(_run_set(tasks, model=args.model_b))
        print(json.dumps(diff(a, b, args.model_a, args.model_b), ensure_ascii=False, indent=2))
        return 0
    if args.eval_command == "rollout":
        return asyncio.run(_cmd_rollout(args))
    print("usage: pi-py eval {run,diff,rollout} ...")
    return 1


async def _cmd_rollout(args) -> int:
    from pathlib import Path

    from pi.evals.export import export_rlvr, export_rlvr_judge, export_sft
    from pi.evals.filter import filter_samples
    from pi.evals.rollout import rollout

    tasks = load_task_set(args.tasks)
    samples = await rollout(
        tasks,
        model=args.model,
        n_samples=args.n,
        concurrency=args.concurrency,
        sandbox=args.sandbox,
        judge_model=args.judge_model,
        partial_credit=not args.no_partial_credit,
    )
    total = len(samples)
    if not args.no_filter:
        samples = filter_samples(samples)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    n_sft = export_sft(samples, out_dir / "sft.jsonl")
    n_rlvr = export_rlvr(samples, out_dir / "rlvr.jsonl")
    n_judge = export_rlvr_judge(samples, out_dir / "rlvr_judge.jsonl")

    avg = sum(s.reward for s in samples) / len(samples) if samples else 0.0
    print(
        json.dumps(
            {
                "rolled_out": total,
                "kept": len(samples),
                "avg_reward": round(avg, 4),
                "files": {
                    "sft.jsonl": n_sft,
                    "rlvr.jsonl": n_rlvr,
                    "rlvr_judge.jsonl": n_judge,
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0
