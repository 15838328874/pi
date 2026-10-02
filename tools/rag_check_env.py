"""RAG 配置自检：验证 .env 里 embedding/rerank 端点真实可用。

用法（在 pi-dev/ 目录下）：
    .venv/Scripts/python.exe tools/rag_check_env.py

检查项：
  1. PI_EMBEDDING_URL / API_KEY / MODEL 已配置且 key 不是占位符/被打码的坏值
     （AI 写 key 会被安全机制打成 "sk-ws…bY4" 这种 9 字符坏值，这里能识别出来）
  2. embedding 端点真实调用一次：打印向量维度、usage tokens、耗时
  3. rerank 端点（若配置）真实调用一次：打印排序结果与 usage
  4. Milvus（若配置 PI_MILVUS_URI/PI_RAG_MILVUS_URI）ping 一次

不打印任何密钥内容；只打印 key 长度与前缀用于人工核对。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pi  # noqa: F401  (triggers .env load from cwd)

from pi.rag.config import RagConfig
from pi.rag.defaults.http_embedder import EmbeddingError, HttpEmbedder
from pi.rag.defaults.http_reranker import HttpReranker, RerankError
from pi.rag.types import RetrievedChunk


def _key_report(name: str, value: str) -> bool:
    """Validate a key looks like a real key, without printing it."""
    ok = True
    problems = []
    if not value:
        problems.append("未配置")
        ok = False
    else:
        if "…" in value or "..." in value:
            problems.append("含省略号——是被打码的坏值，请手工重新粘贴完整 key")
            ok = False
        if value.startswith("PASTE_"):
            problems.append("还是占位符，请填入真实 key")
            ok = False
        if len(value) < 20:
            problems.append(f"长度只有 {len(value)}，疑似不完整")
            ok = False
    status = "OK " if ok else "BAD"
    shown = f"{value[:6]}...({len(value)} chars)" if value and ok else "(见问题)"
    print(f"[{status}] {name} = {shown}")
    for p in problems:
        print(f"       !! {p}")
    return ok


async def main() -> int:
    cfg = RagConfig.from_env()
    print("=" * 60)
    print("① 配置检查")
    print("=" * 60)
    key_ok = _key_report("PI_EMBEDDING_API_KEY", cfg.embedding.api_key)
    print(f"       PI_EMBEDDING_URL   = {cfg.embedding.url or '(未配置)'}")
    print(f"       PI_EMBEDDING_MODEL = {cfg.embedding.model or '(未配置)'}")
    print(f"       style(推断/显式)   = {cfg.embedding.style}")
    print(f"       vector_enabled     = {cfg.vector_enabled()}")
    if cfg.rerank_url:
        print(f"       PI_RAG_RERANK_URL  = {cfg.rerank_url}")
        print(f"       PI_RAG_RERANK_MODEL= {cfg.rerank_model or '(未配置)'}")
    if not cfg.vector_enabled():
        print("\nembedding 未配置完整，检索将只走 BM25 词法兜底（fail-safe，不报错）。")
        if not key_ok:
            return 1
        return 0

    print()
    print("=" * 60)
    print("② embedding 端点真实调用")
    print("=" * 60)
    emb = HttpEmbedder(
        cfg.embedding.url, cfg.embedding.api_key, cfg.embedding.model,
        timeout=cfg.embedding.timeout_s, batch_size=cfg.embedding.batch_size,
        style=cfg.embedding.style,
    )
    t0 = time.perf_counter()
    try:
        res = await emb.embed(["企业知识库混合检索测试", "The quick brown fox"])
        ms = int((time.perf_counter() - t0) * 1000)
        print(f"[OK ] 2 条文本 -> {len(res.vectors)} 个向量, dim={len(res.vectors[0])}, "
              f"usage_tokens={res.usage_tokens}, {ms}ms")
    except EmbeddingError as exc:
        print(f"[BAD] embedding 调用失败: {exc}")
        return 1

    if cfg.rerank_url:
        print()
        print("=" * 60)
        print("③ rerank 端点真实调用")
        print("=" * 60)
        rr = HttpReranker(cfg.rerank_url, cfg.rerank_api_key, cfg.rerank_model)
        chunks = [
            RetrievedChunk(chunk_id=1, doc_key="a", text="文本排序模型广泛用于搜索引擎和推荐系统", score=0.9),
            RetrievedChunk(chunk_id=2, doc_key="b", text="量子计算是计算科学的一个前沿领域", score=0.8),
            RetrievedChunk(chunk_id=3, doc_key="c", text="预训练语言模型的发展给文本排序模型带来了新的进展", score=0.7),
        ]
        t0 = time.perf_counter()
        try:
            out = await rr.rerank("什么是文本排序模型", chunks)
            ms = int((time.perf_counter() - t0) * 1000)
            order = [(c.chunk_id, round(c.score, 4)) for c in out]
            print(f"[OK ] rerank 排序: {order}, usage_tokens={rr.last_usage.usage_tokens if rr.last_usage else 0}, {ms}ms")
            top = out[0].chunk_id if out else None
            if top in (1, 3):
                print("      语义合理：排序模型相关文档排到了前面")
            else:
                print("      !! 警告：最不相关的文档排第一，请人工确认 rerank 模型是否正常")
        except RerankError as exc:
            print(f"[BAD] rerank 调用失败（不阻塞，检索会跳过 rerank）: {exc}")

    milvus_uri = cfg.milvus_uri
    if milvus_uri:
        print()
        print("=" * 60)
        print("④ Milvus 连通性")
        print("=" * 60)
        try:
            from pi.server.vectorstore import MilvusStore

            store = MilvusStore(milvus_uri)
            alive = await store.ping()
            await store.close()
            print(f"[{'OK ' if alive else 'BAD'}] ping {milvus_uri} -> {alive}")
        except Exception as exc:  # noqa: BLE001
            print(f"[BAD] Milvus ping 异常: {type(exc).__name__}: {exc}")
    else:
        print("\n(i) PI_MILVUS_URI / PI_RAG_MILVUS_URI 未配置：向量检索将用 InMemory 兜底。")

    print("\n全部检查完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
