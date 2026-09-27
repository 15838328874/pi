"""Eval data model: tasks, run results, verdicts, reports (JSON-serializable).

A task is DECLARATIVE DATA, not code — versioned in git, sourced from production
trajectories, and expanded over time. This mirrors SWE-bench (task = repo + issue
+ tests) and LangSmith/Braintrust (datasets as data).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class EnvSpec(BaseModel):
    files: dict[str, str] = Field(default_factory=dict)  # {relpath: content}
    setup: list[str] = Field(default_factory=list)  # shell commands before the run


class ScorerSpec(BaseModel):
    type: str  # file | command | tests | judge

    # file scorer: {relpath: regex} — every file must exist and match
    files: dict[str, str] | None = None

    # command scorer: run a shell command, check exit code / output
    command: str | None = None
    expect_exit: int = 0
    expect_contains: str | None = None

    # tests scorer (SWE-bench style): run these pytest node ids
    test_paths: list[str] | None = None

    # judge scorer (LLM-as-judge)
    rubric: str | None = None


class Task(BaseModel):
    id: str
    prompt: str
    category: str = "happy_path"  # happy_path | edge_case | adversarial
    tags: list[str] = Field(default_factory=list)
    env: EnvSpec = Field(default_factory=EnvSpec)
    scorer: ScorerSpec
    timeout: int = 600
    model: str | None = None  # optional per-task model override


class RunResult(BaseModel):
    task_id: str
    trajectory: dict
    usage_input: int = 0
    usage_output: int = 0
    turns: int = 0
    latency_ms: int = 0
    workspace: str = ""
    error: str | None = None


class Verdict(BaseModel):
    task_id: str
    passed: bool
    score: float = 0.0
    evidence: str = ""
    scorer: str = ""
    # Additive, consumed only by reward extraction (partial credit): per-case
    # detail for the tests scorer. None when the scorer has no such detail.
    # ``score``/``passed`` semantics are unchanged by these fields.
    passed_count: int | None = None
    total_count: int | None = None


class TaskReport(BaseModel):
    task_id: str
    category: str
    tags: list[str]
    passed: bool
    score: float
    usage_input: int
    usage_output: int
    turns: int
    latency_ms: int
    evidence: str
    error: str | None = None


class EvalReport(BaseModel):
    total: int
    passed: int
    pass_rate: float
    per_category: dict = Field(default_factory=dict)  # category -> {total, passed, pass_rate}
    tasks: list[TaskReport] = Field(default_factory=list)
