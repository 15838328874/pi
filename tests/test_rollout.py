"""RL data flywheel tests: rollout, reward extraction, filtering, export.

All offline: FakeProvider + temp workspaces, no real model, no sandbox, no
training framework.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from pi.evals.export import export_rlvr, export_rlvr_judge, export_sft
from pi.evals.filter import filter_samples
from pi.evals.reward import extract_reward
from pi.evals.rollout import RolloutSample, rollout
from pi.evals.schema import EnvSpec, ScorerSpec, Task, Verdict
from pi.evals.scorers import _parse_pytest_summary, score
from pi.llm.fake import FakeProvider
from pi.models import TextBlock, ToolCallBlock


def _task(tmp_path: Path, task_id: str = "t1") -> Task:
    return Task(
        id=task_id,
        prompt="在 out.txt 写入 HELLO_WORLD",
        env=EnvSpec(files={}),
        scorer=ScorerSpec(type="file", files={"out.txt": "HELLO_WORLD"}),
        timeout=30,
    )


def _scripted_provider():
    return FakeProvider(
        responses=[
            [
                ToolCallBlock(
                    id="t1",
                    name="write",
                    arguments=json.dumps({"path": "out.txt", "content": "HELLO_WORLD"}),
                )
            ],
            [TextBlock(text="done")],
        ]
    )


class TestRollout:
    def test_rollout_produces_n_samples_with_reward_and_messages(self, tmp_path: Path):
        task = _task(tmp_path)

        async def main():
            samples = await rollout(
                [task],
                model="fake/demo",
                n_samples=3,
                concurrency=2,
                provider=lambda: _scripted_provider(),  # factory: fresh per sample
            )
            return samples

        samples = asyncio.run(main())
        assert len(samples) == 3
        for s in samples:
            assert s.task_id == "t1"
            assert s.reward == 1.0
            assert s.reward_meta["source"] == "verifiable"
            assert s.trajectory  # full P1 log captured
            roles = [m["role"] for m in s.messages]
            assert roles == ["user", "assistant", "tool", "assistant"]
            assert s.messages[1]["tool_calls"][0]["function"]["name"] == "write"
            assert json.loads(s.messages[1]["tool_calls"][0]["function"]["arguments"]) == {
                "path": "out.txt",
                "content": "HELLO_WORLD",
            }
            assert s.messages[2]["tool_call_id"] == "t1"
            assert "out.txt" in s.messages[2]["content"]  # write tool's confirmation

    def test_failed_task_gets_zero_reward(self, tmp_path: Path):
        task = _task(tmp_path)

        async def main():
            samples = await rollout([task], model="fake/demo", n_samples=1)
            return samples

        # provider that does nothing useful -> file missing -> fail
        samples = asyncio.run(main())
        assert samples[0].reward == 0.0
        assert samples[0].reward_meta["passed"] is False


class TestRewardExtraction:
    def test_tests_partial_credit(self):
        v = Verdict(task_id="t", passed=False, scorer="tests",
                    passed_count=3, total_count=5)
        reward, meta = extract_reward(v)
        assert reward == 0.6
        assert meta["source"] == "verifiable"

    def test_tests_binary_fallback_without_counts(self):
        v = Verdict(task_id="t", passed=True, scorer="tests")
        assert extract_reward(v)[0] == 1.0
        v = Verdict(task_id="t", passed=False, scorer="tests")
        assert extract_reward(v)[0] == 0.0

    def test_partial_credit_disabled_is_binary(self):
        v = Verdict(task_id="t", passed=False, scorer="tests",
                    passed_count=3, total_count=5)
        assert extract_reward(v, partial_credit=False)[0] == 0.0

    def test_judge_reward_tagged(self):
        v = Verdict(task_id="t", passed=True, score=0.7, scorer="judge")
        reward, meta = extract_reward(v)
        assert reward == 0.7
        assert meta["source"] == "judge"

    def test_zero_total_is_binary_fallback(self):
        v = Verdict(task_id="t", passed=False, scorer="tests",
                    passed_count=0, total_count=0)
        assert extract_reward(v)[0] == 0.0


class _FakeRunner:
    def __init__(self):
        self.calls: list[tuple[str, str, int]] = []

    async def run(self, command, cwd, timeout):
        self.calls.append((command, str(cwd), timeout))
        return SimpleNamespace(exit_code=0, output="ok")


class TestScorerRunner:
    def test_command_scorer_uses_runner(self, tmp_path: Path):
        task = Task(
            id="c1", prompt="p",
            scorer=ScorerSpec(type="command", command="make check"),
        )
        result = SimpleNamespace(workspace=str(tmp_path))

        async def main():
            runner = _FakeRunner()
            v = await score(task, result, runner=runner)
            return v, runner

        v, runner = asyncio.run(main())
        assert v.passed
        assert runner.calls[0][0] == "make check"

    def test_pytest_summary_parsing(self):
        assert _parse_pytest_summary("3 passed, 1 failed in 0.12s") == (3, 4)
        assert _parse_pytest_summary("5 passed in 0.01s") == (5, 5)
        assert _parse_pytest_summary("1 passed, 1 error in 1.0s") == (1, 2)
        assert _parse_pytest_summary("no tests ran in 0.00s") == (None, None)


def _sample(task_id="t1", reward=0.0, source="verifiable", n_calls=1, n_errors=0,
            prompt="p", content="x") -> RolloutSample:
    events: list[dict] = [{"type": "RunStarted", "prompt": prompt}]
    events += [
        {"type": "ToolCall", "call_id": f"c{i}", "result": "r", "is_error": i < n_errors}
        for i in range(n_calls)
    ]
    events.append({"type": "LlmCall", "turn": 1, "text": content, "tool_calls": []})
    return RolloutSample(
        task_id=task_id,
        prompt=prompt,
        trajectory={"run_id": "volatile", "started_at": 0, "events": events},
        messages=[{"role": "user", "content": prompt}],
        reward=reward,
        reward_meta={"source": source},
        usage_input=1,
        usage_output=1,
        latency_ms=10,
    )


class TestFilter:
    def test_dedupe_ignores_volatile_fields(self):
        s1, s2 = _sample(), _sample()
        assert len(filter_samples([s1, s2])) == 1

    def test_noise_dropped_and_recovery_kept(self):
        wrong_no_tools = _sample(reward=0.0, n_calls=0, content="w1")
        all_errored = _sample(reward=0.0, n_calls=2, n_errors=2, content="w2")
        pass_no_tools = _sample(reward=1.0, n_calls=0, content="w3")
        recovery = _sample(reward=1.0, n_calls=3, n_errors=1, content="recovered")
        kept = filter_samples([wrong_no_tools, all_errored, pass_no_tools, recovery])
        ids = {id(s) for s in kept}
        assert id(pass_no_tools) in ids and id(recovery) in ids
        assert id(wrong_no_tools) not in ids and id(all_errored) not in ids

    def test_rejection_sampling_top_k(self):
        samples = [
            _sample(task_id="t", reward=0.2, content="a"),
            _sample(task_id="t", reward=0.9, content="b"),
            _sample(task_id="t", reward=0.5, content="c"),
            _sample(task_id="t", reward=0.8, content="d"),
            _sample(task_id="t", reward=0.7, content="e"),
        ]
        kept = filter_samples(samples, top_k=2, max_per_task=4)
        assert len(kept) == 2  # top_k limits non-recovery samples
        assert sorted((s.reward for s in kept), reverse=True) == [0.9, 0.8]

    def test_recovery_samples_survive_top_k_and_cap_bites(self):
        samples = [
            _sample(task_id="t", reward=1.0, n_calls=2, n_errors=1, content="recovery"),
            _sample(task_id="t", reward=0.9, content="hi"),
            _sample(task_id="t", reward=0.8, content="hi2"),
            _sample(task_id="t", reward=0.7, content="hi3"),
        ]
        # recovery is kept outside the top_k budget; cap then trims the tail
        kept = filter_samples(samples, top_k=3, max_per_task=2)
        assert len(kept) == 2
        rewards = [s.reward for s in kept]
        assert 1.0 in rewards  # recovery survives
        assert 0.9 in rewards  # and the best non-recovery sample


class TestExport:
    def test_sft_export_high_quality_only_with_system(self, tmp_path: Path):
        good = _sample(reward=1.0)
        bad = _sample(reward=0.0, content="other")
        out = tmp_path / "sft.jsonl"
        assert export_sft([good, bad], out) == 1
        line = json.loads(out.read_text(encoding="utf-8").strip())
        assert line["messages"][0]["role"] == "system"
        assert line["messages"][1]["role"] == "user"

    def test_rlvr_and_judge_files_are_disjoint(self, tmp_path: Path):
        ver = _sample(task_id="a", reward=1.0, source="verifiable")
        ver2 = _sample(task_id="b", reward=0.4, source="verifiable", content="y")
        judge = _sample(task_id="c", reward=0.8, source="judge")
        assert export_rlvr([ver, ver2, judge], tmp_path / "rlvr.jsonl") == 2
        assert export_rlvr_judge([ver, ver2, judge], tmp_path / "judge.jsonl") == 1
        line = json.loads((tmp_path / "rlvr.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert line["prompt"] == "p"
        assert line["reward"] == 1.0
        assert line["trajectory"][0]["role"] == "user"
        assert line["metadata"]["task_id"] == "a"
