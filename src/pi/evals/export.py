"""Export RolloutSamples as training JSONL (SFT / RLVR / judge).

- sft.jsonl: reward == 1.0 samples only (both verifiable and judge-passed),
  one high-quality trajectory per line, system message included - cold start
  data that teaches the agent the tool-call format.
- rlvr.jsonl: VERIFIABLE samples only (reward from the environment) - this is
  the RLVR stream; judge scores must never leak into it.
- rlvr_judge.jsonl: judge-scored samples, kept separately for SFT filtering /
  future preference data.

Field names follow the canonical veRL/TRL shapes but the frameworks' dataset
interfaces change between versions - check the target version's docs before
training.
"""

from __future__ import annotations

import json
from pathlib import Path

from pi.evals.rollout import RolloutSample
from pi.prompt import SYSTEM_PROMPT


def export_sft(samples: list[RolloutSample], path: Path) -> int:
    """reward == 1.0 samples as OpenAI chat trajectories with the system prompt."""
    lines = 0
    with path.open("w", encoding="utf-8") as f:
        for s in samples:
            if s.reward < 1.0:
                continue
            messages = [{"role": "system", "content": SYSTEM_PROMPT}, *s.messages]
            f.write(json.dumps({"messages": messages}, ensure_ascii=False) + "\n")
            lines += 1
    return lines


def export_rlvr(samples: list[RolloutSample], path: Path) -> int:
    """All VERIFIABLE samples with their precomputed reward (offline reward)."""
    lines = 0
    with path.open("w", encoding="utf-8") as f:
        for s in samples:
            if s.reward_meta.get("source") != "verifiable":
                continue
            f.write(json.dumps(_rlvr_line(s), ensure_ascii=False) + "\n")
            lines += 1
    return lines


def export_rlvr_judge(samples: list[RolloutSample], path: Path) -> int:
    """Judge-scored samples only - never mix these into the RLVR stream."""
    lines = 0
    with path.open("w", encoding="utf-8") as f:
        for s in samples:
            if s.reward_meta.get("source") != "judge":
                continue
            f.write(json.dumps(_rlvr_line(s), ensure_ascii=False) + "\n")
            lines += 1
    return lines


def _rlvr_line(s: RolloutSample) -> dict:
    return {
        "prompt": s.prompt,
        "reward": s.reward,
        "trajectory": s.messages,
        "metadata": {
            "task_id": s.task_id,
            "reward_meta": s.reward_meta,
            "usage": {"input_tokens": s.usage_input, "output_tokens": s.usage_output},
            "latency_ms": s.latency_ms,
            "error": s.error,
        },
    }
