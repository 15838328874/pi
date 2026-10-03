"""Probe: 记忆语义去重阈值 _DUP_SIMILARITY 到底该设多少——用真实 embedding 实测。

回答的问题是「0.92 这个 cosine 阈值合不合适」，用测量代替拍脑袋：对一批
「语义相同、措辞不同（该判重）」与「语义相近但不同（不该判重）」的记忆短句，
用线上 embedding 模型算出两两 cosine，打印排序结果 + 不同阈值下的混淆矩阵。

结论刻进 ROADMAP §3「记忆去重/驱逐硬化」：qwen3.7-text-embedding 上 0.92 是
「零误杀、漏判 ~30%」的保守点——负样本最高 ~0.91 已贴到阈值，余量仅 ~0.01，
换 embedding 模型必须重跑本工具重标定。

Run: PYTHONPATH=src .venv/bin/python tools/probe_memory_threshold.py
"""

from __future__ import annotations

import asyncio
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pi.llm.embedding import EmbeddingClient  # noqa: E402


def _read_env_file(path: Path) -> dict[str, str]:
    """解析 KEY=VALUE 的 env 文件（跳过注释/空行，去成对引号）。"""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


# 正样本：语义相同、措辞差异递增（应该判重）
POSITIVE = [
    ("密码是 abc123", "密码为 abc123"),
    ("项目代号是 Orion", "这个项目的代号叫 Orion"),
    ("部署在火山引擎 ECS", "部署在火山引擎的 ECS 上"),
    ("用户喜欢 Rust 和钓鱼", "用户喜欢 Rust，爱好钓鱼"),
    ("数据库用 MySQL 主从", "数据库采用 MySQL 主从架构"),
    ("用户偏好中文回答", "回答请用中文"),
    ("每周五发布版本", "版本固定在每周五发"),
    ("不要用全局变量", "避免使用全局变量"),
    ("生产库在阿里云杭州", "生产环境的数据库部署在阿里云杭州区"),
    ("接口超时设 30 秒", "所有接口的超时时间配置为 30 秒"),
]

# 负样本：语义相近但不同、相似度递增（不该判重，越靠后越接近误杀边界）
NEGATIVE = [
    ("用户喜欢钓鱼", "数据库用 MySQL"),
    ("项目 A 的代号是 Orion", "项目 B 的代号是 Atlas"),
    ("用户喜欢 Rust", "用户喜欢 Go"),
    ("数据库用 MySQL 主从", "数据库用 PostgreSQL 主从"),
    ("部署在火山引擎 ECS", "部署在阿里云 ECS"),
    ("用户偏好中文回答", "用户偏好英文回答"),
    ("项目 A 用 MySQL", "项目 B 用 MySQL"),
    ("接口超时设 30 秒", "接口超时设 60 秒"),
    ("每周五发布版本", "每周一发布版本"),
    ("用户 A 的生日是 1 月", "用户 B 的生日是 1 月"),
]


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


async def _embed_batched(client: EmbeddingClient, texts: list[str], batch: int = 6) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for i in range(0, len(texts), batch):
        chunk = texts[i : i + batch]
        result = await client.embed(chunk)
        for t, v in zip(chunk, result.vectors):
            out[t] = v
    return out


async def main() -> int:
    env = _read_env_file(ROOT / ".env.local")
    for key in ("PI_EMBEDDING_URL", "PI_EMBEDDING_API_KEY", "PI_EMBEDDING_MODEL"):
        if not env.get(key) and not os.environ.get(key):
            print(f"missing {key} (check .env.local or env)", file=sys.stderr)
            return 1
    client = EmbeddingClient(
        env.get("PI_EMBEDDING_URL") or os.environ["PI_EMBEDDING_URL"],
        env.get("PI_EMBEDDING_API_KEY") or os.environ["PI_EMBEDDING_API_KEY"],
        env.get("PI_EMBEDDING_MODEL") or os.environ["PI_EMBEDDING_MODEL"],
    )

    texts = list(dict.fromkeys(t for pair in POSITIVE + NEGATIVE for t in pair))
    vec = await _embed_batched(client, texts)
    pos = [_cos(vec[a], vec[b]) for a, b in POSITIVE]
    neg = [_cos(vec[a], vec[b]) for a, b in NEGATIVE]

    print(f"model={client.model}  {len(POSITIVE)}正/{len(NEGATIVE)}负\n")
    print("== 正样本（该判重）sorted ==")
    for (a, b), c in sorted(zip(POSITIVE, pos), key=lambda x: x[1]):
        print(f"  {c:.4f}  [{'DUP' if c >= 0.92 else 'LEAK'}]  {a!r} vs {b!r}")
    print("== 负样本（不该判重）sorted ==")
    for (a, b), c in sorted(zip(NEGATIVE, neg), key=lambda x: x[1]):
        print(f"  {c:.4f}  [{'KILL' if c >= 0.92 else 'OK'}]  {a!r} vs {b!r}")

    print(f"\n正: min={min(pos):.4f} med={sorted(pos)[len(pos)//2]:.4f} max={max(pos):.4f}")
    print(f"负: min={min(neg):.4f} med={sorted(neg)[len(neg)//2]:.4f} max={max(neg):.4f}")
    for thr in (0.80, 0.85, 0.88, 0.90, 0.92, 0.95):
        tp = sum(1 for c in pos if c >= thr)
        fp = sum(1 for c in neg if c >= thr)
        print(f"thr={thr}: 正抓{tp}/{len(pos)} 漏{len(pos)-tp} | 负误杀{fp}/{len(neg)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
