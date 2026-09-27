"""Unified reward extraction for the RL data flywheel.

RLVR discipline: verifiable rewards (tests/command/file) come purely from the
environment; judge scores are a different trust class and must never mix into
the RLVR stream - they are tagged with source="judge" and exported to a
separate file.

Partial credit: for the tests scorer, reward = passed_count / total_count when
the detail is available (additive Verdict fields) - GRPO's within-group
comparison then sees a gradient (0.6 vs 0.0) instead of all-or-nothing zeros.
"""

from __future__ import annotations

from pi.evals.schema import Verdict


def extract_reward(verdict: Verdict, *, partial_credit: bool = True) -> tuple[float, dict]:
    """Verdict -> (reward, reward_meta). reward is 0~1.

    reward_meta carries ``source`` ("verifiable" | "judge") so downstream
    export can route the sample to the right file.
    """
    if verdict.scorer == "judge":
        return (
            round(verdict.score, 4),
            {"source": "judge", "evidence": verdict.evidence[:200]},
        )

    if partial_credit and verdict.passed_count is not None and verdict.total_count:
        reward = round(verdict.passed_count / verdict.total_count, 4)
    else:
        reward = 1.0 if verdict.passed else 0.0
    return (
        reward,
        {
            "source": "verifiable",
            "passed": verdict.passed,
            "passed_count": verdict.passed_count,
            "total_count": verdict.total_count,
            "evidence": verdict.evidence[:200],
        },
    )
