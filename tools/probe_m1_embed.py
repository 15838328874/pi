"""M1 -> real-embedder end-to-end smoke (uses the REAL DashScope endpoint).

Proves M1 output is consumable downstream, with REAL corpus + REAL embeddings
(no mocks, no fakes):
  parse real file -> chunk -> feed chunk.embed_text to the REAL embedder
  -> dim consistent, usage billed, contextual prefix really reached the
     embedded text, and a topically-related chunk out-scores an unrelated one.

Throwaway diagnostic. Creds come from pi-dev/.env (user-filled, never printed).
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pi  # noqa: F401,E402  (triggers .env load from cwd, like rag_check_env)
from pi.rag.chunker import Chunker  # noqa: E402
from pi.rag.config import ChunkingConfig, RagConfig  # noqa: E402
from pi.rag.defaults.http_embedder import HttpEmbedder  # noqa: E402
from pi.rag.parser import parse_file  # noqa: E402

CORPUS = Path(r"C:\Users\朱文宝\Desktop\pi版本\pi-rag\待测试文档")


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


async def main() -> None:
    cfg = RagConfig.from_env()
    emb = HttpEmbedder(
        url=cfg.embedding.url,
        api_key=cfg.embedding.api_key,
        model=cfg.embedding.model,
        batch_size=cfg.embedding.batch_size,
        timeout=cfg.embedding.timeout_s,
    )

    # real corpus -> real chunks
    res = parse_file(CORPUS / "RAG评估.md")
    drafts = Chunker(ChunkingConfig(max_chars=400)).chunk(res)
    print(f"parsed {res.title!r}: {len(drafts)} chunks")
    assert drafts, "no chunks produced"

    # 1) contextual prefix present in embed_text, absent from cited text
    with_prefix = [d for d in drafts if d.embed_text != d.text and d.title_path]
    print(f"chunks whose embed_text carries a title_path prefix: {len(with_prefix)}")
    assert with_prefix, "contextual prefix never reached embed_text"
    s = with_prefix[0]
    assert s.embed_text.startswith(s.title_path)
    assert not s.text.startswith(s.title_path)

    # 2) REAL embedding over the first few chunks
    texts = [d.embed_text[:1500] for d in drafts[:4]]
    out = await emb.embed(texts)
    dim = len(out.vectors[0])
    print(f"real embed: {len(out.vectors)} vectors, dim={dim}, usage_tokens={out.usage_tokens}")
    assert len(out.vectors) == len(texts)
    assert out.usage_tokens > 0, "real call must bill tokens"
    assert all(len(v) == dim for v in out.vectors), "vector dims inconsistent"

    # 3) semantic sanity vs an unrelated corpus file
    unrelated = parse_file(CORPUS / "html-tags-decode.html")
    udrafts = Chunker(ChunkingConfig(max_chars=400)).chunk(unrelated)
    uvec = (await emb.embed([udrafts[0].embed_text[:1500]])).vectors[0]

    q = "RAG 检索质量的评估指标 context relevancy faithfulness"
    qv = (await emb.embed_query(q)).vectors[0]
    rag_chunk = next(d for d in drafts if "评估" in d.text)
    rv = (await emb.embed([rag_chunk.embed_text[:1500]])).vectors[0]

    s_rag, s_unrel = _cos(qv, rv), _cos(qv, uvec)
    print(f"cos(query, RAG评估 chunk)   = {s_rag:.4f}")
    print(f"cos(query, HTML标签 chunk)  = {s_unrel:.4f}")
    assert s_rag > s_unrel, "real embedder did not rank the related chunk higher"

    print("\nM1 -> real embedder smoke: PASS")


if __name__ == "__main__":
    asyncio.run(main())
