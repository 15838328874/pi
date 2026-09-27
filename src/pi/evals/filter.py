"""Rollout filtering: dedupe, noise removal, rejection sampling.

Order (each stage keeps the "most valuable" subset for RLVR/SFT):

1. Dedupe on (task_id, normalized trajectory hash). The raw trajectory dict
   contains run_id (random) and started_at (timestamp), so the hash must be
   taken over a normalized projection or nothing ever dedupes.
2. Noise removal: samples with no tool calls AND reward < 1 carry no learning
   signal (a bare wrong answer); samples whose tool calls ALL errored AND
   reward < 1 are total failures. Zero-tool samples that PASS are kept - the
   model should also learn when tools are unnecessary.
3. Rejection sampling per task: recovery samples (reward == 1.0 despite
   mid-trajectory tool errors) are the most valuable - kept unconditionally;
   then the top_k by reward; then a per-task cap so easy tasks cannot drown
   hard ones.
"""

from __future__ import annotations

import hashlib
import json
import logging

from pi.evals.rollout import RolloutSample

log = logging.getLogger("pi.evals.filter")

# trajectory keys that are per-run volatile, not part of the semantic content
_VOLATILE_TOP = ("run_id", "started_at")
_VOLATILE_EVENT = ("latency_ms",)


def filter_samples(
    samples: list[RolloutSample],
    *,
    top_k: int = 2,
    max_per_task: int = 4,
) -> list[RolloutSample]:
    """Dedupe -> drop noise -> per-task rejection sampling with a cap."""
    samples = _dedupe(samples)
    samples = [s for s in samples if not _is_noise(s)]

    by_task: dict[str, list[RolloutSample]] = {}
    for s in samples:
        by_task.setdefault(s.task_id, []).append(s)

    kept: list[RolloutSample] = []
    for task_id, group in sorted(by_task.items()):
        recovery = [s for s in group if s.reward >= 1.0 and _tool_errors(s) > 0]
        rest = sorted(
            (s for s in group if s not in recovery),
            key=lambda s: s.reward,
            reverse=True,
        )
        chosen = recovery + rest[:top_k]
        kept.extend(chosen[:max_per_task])
        if len(chosen) > max_per_task:
            log.debug("task %s: capped %d -> %d", task_id, len(chosen), max_per_task)
    return kept


def _dedupe(samples: list[RolloutSample]) -> list[RolloutSample]:
    seen: set[tuple[str, str]] = set()
    out: list[RolloutSample] = []
    for s in samples:
        key = (s.task_id, _stable_hash(s.trajectory))
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _stable_hash(traj: dict) -> str:
    """Hash of the trajectory's semantic content (volatile fields stripped)."""
    stable = {k: v for k, v in traj.items() if k not in _VOLATILE_TOP}
    stable["events"] = [
        {k: v for k, v in e.items() if k not in _VOLATILE_EVENT}
        for e in traj.get("events", [])
    ]
    payload = json.dumps(stable, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(payload).hexdigest()


def _tool_stats(sample: RolloutSample) -> tuple[int, int]:
    calls = [
        e for e in sample.trajectory.get("events", []) if e.get("type") == "ToolCall"
    ]
    errors = sum(1 for e in calls if e.get("is_error"))
    return len(calls), errors


def _tool_errors(sample: RolloutSample) -> int:
    return _tool_stats(sample)[1]


def _is_noise(sample: RolloutSample) -> bool:
    """True when the sample carries no learning signal worth keeping."""
    if sample.reward >= 1.0:
        return False  # correct answers are never noise (incl. zero-tool passes)
    n_calls, errors = _tool_stats(sample)
    if n_calls == 0:
        return True  # bare wrong answer
    if errors >= 1 and errors == n_calls:
        return True  # every tool call failed, no recovery attempt
    return False
