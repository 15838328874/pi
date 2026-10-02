"""Build the RAG golden set from the REAL corpus (M4, 对接文档 §6 step 1+5).

铁律: 先建评测, 再调检索. M3 already measured the retriever on a 9-chunk
single-doc corpus and produced a counter-intuitive result (hybrid RRF 0.833 <
vector_only 0.867). Before tuning ANY knob, that needs a corpus big and varied
enough to be meaningful, and a golden set with >=50 cases across all three
categories (§6 step 1).

Design decisions that matter (each one prevents a specific way the eval lies):

1. **Questions come from a real LLM, not from me.** A hand-written golden set
   encodes the author's assumptions about what should be easy. qwen3.8-max
   reads each chunk and writes the questions.

2. **Every case carries a verbatim ``answer_excerpt``, and the excerpt is
   VERIFIED to exist in the chunk before the case is kept.** An unverifiable
   gold is worse than no gold: it silently caps Recall@k below 1.0 forever and
   every tuning decision gets made against noise.

3. **Cases are tagged ``lexical_leak`` when the question copies long verbatim
   spans from its chunk.** Those are the cases BM25 wins for free, and they are
   exactly what made M3's hybrid look bad/good for the wrong reason. Slicing
   metrics by this tag is what turns "BM25 is noisy" from a guess into a
   measurement.

4. **adversarial = a real gold that shares surface terms with a distractor**,
   not an unanswerable question. The harness requires gold_chunk_keys (a case
   with no gold cannot be scored), and the interesting failure mode here is
   fusion picking the lexically-similar WRONG chunk - which is precisely the
   RRF vs vector tension under investigation.

5. **The 351 CSV chunks are ingested but never questioned.** They are
   realistic distractor mass (avg 14 distinct tokens/chunk vs 115 for prose),
   and they are the suspected cause of BM25's avgdl distortion. Including them
   in the index while excluding them from the gold is the experiment, not an
   oversight.

6. **gold_chunk_keys are any-of COMPLETE at build time (v2 lesson from
   rag-036).** If the same verbatim excerpt appears in several chunks of the
   SAME doc, ALL of them are gold - a retrieval that surfaces any one of them
   is correct, and scoring it as a miss would be a labeling defect, not a
   retrieval bug. ``rebind_golden`` re-derives this on every A/B run anyway;
   doing it here too means the saved golden set is honest on its own.

7. **Questions must be unambiguous within the corpus (v2 lesson from
   rag-034).** A question whose answer is not unique across the corpus
   ("示例程序类名" when the doc has five example classes) makes the gold
   arbitrary: any of several chunks is defensible, so a "miss" measures the
   labeler's coin flip, not the retriever. The prompt now requires the
   question to carry enough context (which guideline / which condition / which
   section) that exactly one chunk answers it.

Run:  python tools/build_golden_set.py [--corpus v1|v2]
      --corpus v1   original 6-doc corpus (390 chunks, 60 cases)  [default]
      --corpus v2   13 medical-guideline PDFs, prose-dense & heterogeneous -
                    the SECOND-corpus replication that decides whether M4's
                    "BM25 adds nothing" conclusion generalizes or was
                    over-fitted to v1's 351 homogeneous CSV rows.
Out:  evals/tasks/rag/corpus_v1.json  (or corpus_v2.json)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

import pi  # noqa: F401,E402  (loads ./.env)

from pi.rag.config import ChunkingConfig, EmbeddingConfig, RagConfig  # noqa: E402
from pi.rag.defaults.bm25 import MemoryBM25Index, tokenize  # noqa: E402
from pi.rag.defaults.http_embedder import HttpEmbedder  # noqa: E402
from pi.rag.defaults.milvus_vector import MilvusRagVectorStore  # noqa: E402
from pi.rag.defaults.mysql_store import MysqlChunkStore  # noqa: E402
from pi.rag.eval.harness import (  # noqa: E402
    MAX_ANY_OF_GOLD,
    GoldenQA,
    GoldenSet,
    chunk_key,
    normalize_ws,
)
from pi.rag.ingest import IngestPipeline  # noqa: E402
from pi.rag.types import IngestStatus  # noqa: E402

# ---------------------------------------------------------------------------
# Corpus profiles. Each profile is fully self-contained: its own doc list, its
# own eval user (so two corpora can coexist in MySQL/Milvus without ACL
# collisions), its own Milvus collection, its own golden-set filename.
# ---------------------------------------------------------------------------

# doc_key -> (filename, kind). "prose" chunks get questions; "data"/"tabular"
# chunks are distractor mass only (see module docstring #5).
CORPUS_V1: list[tuple[str, str, str]] = [
    ("rag-eval", "RAG评估.md", "prose"),
    ("html-decode", "html-tags-decode.html", "prose"),
    ("array-docx", "数组.docx", "prose"),
    ("broker-pdf", "甬兴证券-AI行业点评报告：海外科技巨头持续发力AI，龙头公司中报业绩亮眼.pdf", "prose"),
    ("sales-xlsx", "销售数据统计.xlsx", "tabular"),
    ("claims-csv", "训练数据.csv", "data"),
]

# v2: 13 text-native medical-guideline PDFs (338 pages, ~422k chars, all
# prose-dense, multi-topic, full of exact terms/numbers/codes to look up).
# This is the heterogeneous second corpus the M4 verdict gate (b) demands:
# v1's conclusion "BM25 unique coverage = 0/60" is suspect because 351 of its
# 390 chunks are homogeneous CSV rows. A prose-only corpus is where a lexical
# channel SHOULD earn its keep - if it still adds nothing here, the conclusion
# generalizes; if it starts winning, v1 was over-fitted.
#
# NOTE (scope, recorded in meta): parse_pdf emits no BLOCK_HEADING, so every
# v2 chunk has an EMPTY title_path. That is honest v1 parser behavior for
# PDFs without a heading layer, and it makes v2 a clean "pure-prose vector vs
# lexical" test. The title_path-symmetry fix (M4 edge_case regression) is
# validated separately on v1, which does have headings (docx/md).
CORPUS_V2: list[tuple[str, str, str]] = [
    ("med-obesity-2024", "《肥胖症诊疗指南（2024版）》.pdf", "prose"),
    ("med-obesity-diet-2024", "成人肥胖食养指南2024.pdf", "prose"),
    ("med-diabetes-diet-2023", "成人糖尿病食养指南（2023年版）.pdf", "prose"),
    ("med-insomnia-2023", "中国成人失眠诊断与治疗指南（2023版）.pdf", "prose"),
    ("med-lipid-2024", "中国血脂管理指南(基层版2024年).pdf", "prose"),
    ("med-obesity-consensus", "中国居民肥胖防治专家共识 (1).pdf", "prose"),
    ("med-weight-flow-2021", "4. 超重或肥胖人群体重管理流程的专家共识2021.pdf", "prose"),
    ("med-weight-principle-2024", "体重管理指导原则（2024年版）.pdf", "prose"),
    ("med-grassroots-2025", "国家基层肥胖症综合管理技术指南（2025）.pdf", "prose"),
    ("med-pcos", "多囊中国诊疗指南.pdf", "prose"),
    ("med-weight-knowledge-2024", "居民体重管理核心知识(2024年版)释义.pdf", "prose"),
    ("med-activity-strategy", "成人减重和防止体重反弹的体力活动干预策略.pdf", "prose"),
    ("med-weight-standard", "超重或肥胖人群体重管理专家共识及团体标准.pdf", "prose"),
]

_PROFILES: dict[str, dict] = {
    "v1": {
        "corpus": CORPUS_V1,
        "eval_user": 992001,  # dedicated, documented; distinct from itest throwaway ranges
        "collection": "pi_rag_chunks_itest",
        "golden_name": "rag-corpus-v1",
        "golden_file": "corpus_v1.json",
        # v1 prose docs are small (34 chunks); question every one.
        "max_chunks_per_doc": 0,  # 0 = no cap
        "questions_per_chunk": 2,
    },
    "v2": {
        "corpus": CORPUS_V2,
        "eval_user": 992002,  # distinct user -> v1 (992001) stays intact in MySQL/Milvus
        "collection": "pi_rag_chunks_itest_v2",  # distinct collection -> no vector collision
        "golden_name": "rag-corpus-v2",
        "golden_file": "corpus_v2.json",
        # ~586 chunks total; questioning all of them at 2 q/chunk is ~1172 LLM
        # calls (slow + costly) and would over-weight the 3 big PDFs (79/70/64
        # pages). Cap per doc and sample evenly across each doc's page range so
        # every guideline contributes questions from beginning/middle/end.
        "max_chunks_per_doc": 14,
        "questions_per_chunk": 2,
    },
}

# Back-compat module-level defaults: ab_rag.py / mine_badcases.py do
# `from build_golden_set import CORPUS, EVAL_USER`. They keep working against
# v1 unless they opt into select_corpus() with an explicit --corpus.
CORPUS = CORPUS_V1
EVAL_USER = _PROFILES["v1"]["eval_user"]
_ITESS_COLLECTION = _PROFILES["v1"]["collection"]


def select_corpus(name: str) -> dict:
    """Return the full profile dict for a corpus name ('v1'|'v2')."""
    if name not in _PROFILES:
        raise SystemExit(f"unknown corpus '{name}'; choose from {sorted(_PROFILES)}")
    return _PROFILES[name]


def _corpus_dir() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "待测试文档"
        if cand.is_dir():
            return cand
    raise SystemExit("待测试文档 corpus not found")


# ---------------------------------------------------------------------------
# LLM question generation (real model; a golden set I write by hand would
# encode my own assumptions about what retrieval should find easy)
# ---------------------------------------------------------------------------

_SYSTEM = (
    "你是检索评测集的出题人。给你一段企业知识库文档片段，你要出中文问答题，"
    "用于评测 RAG 检索质量（不是评测生成质量）。\n"
    "硬性要求：\n"
    "1. answer_excerpt 必须是从给定片段里**逐字复制**的连续原文，10~40 字，"
    "不得改写、不得翻译、不得拼接、不得加省略号。\n"
    "2. question 必须是自然的用户提问，**不要照抄原文句子**，用同义改写或口语化表达。\n"
    "3. 问题的答案必须真的在这段片段里，不能靠常识回答。\n"
    "4. **question 必须自带足够限定语境**（点明是哪份指南/哪个疾病/哪个条件下/"
    "哪个具体指标），使答案在整份语料里**唯一确定**。禁止问'XX是什么''XX的类名'"
    "这种多个片段都能答、gold 只能武断选一个的泛问题——那种题的'未命中'衡量的是"
    "出题人的随意，不是检索质量。\n"
    "5. 只输出 JSON，不要任何解释文字、不要 markdown 代码块。\n"
)

_PROMPT = """片段（标题路径 + 正文）:
标题路径: {title_path}
正文:
{text}

请出 {n} 道题，JSON 数组格式:
[{{"question": "...", "answer_excerpt": "逐字原文片段", "kind": "happy_path"}}]

kind 取值规则（{n} 道题尽量分布在不同 kind）:
- "happy_path": 常规提问，答案是片段的主要内容。
- "edge_case": 问题的关键词**只出现在标题路径里**（正文没有该词），用来考察检索是否能命中章节标题。若标题路径为空或无独特术语，则改用"针对片段中某个具体数值/专有名词/英文术语/编号"的提问（答案唯一、需要精确定位）。
- "adversarial": 提问时**故意使用与内容相关但片段里并不突出的表述**，或用一个容易与其他文档混淆的角度提问，但答案确实只在本片段里。

再次强调：每道题的 question 都要带上限定语境（如"根据《XX指南》，…""在XX情况下，…"），确保答案唯一。
"""


async def _llm_questions(client: httpx.AsyncClient, base: str, key: str, model: str,
                         title_path: str, text: str, n: int) -> list[dict]:
    prompt = _PROMPT.format(title_path=title_path or "(无)", text=text[:2400], n=n)
    resp = await client.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "temperature": 0.4,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
        },
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"] or ""
    return _extract_json_array(content)


def _extract_json_array(content: str) -> list[dict]:
    """Tolerate a model that wraps JSON in prose or a ```json fence."""
    s = content.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        data = json.loads(s)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        pass
    # last resort: first balanced [...] block
    start = s.find("[")
    if start < 0:
        return []
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "[":
            depth += 1
        elif s[i] == "]":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(s[start : i + 1])
                    return data if isinstance(data, list) else []
                except json.JSONDecodeError:
                    return []
    return []


# ---------------------------------------------------------------------------
# Gold validation - the part that keeps the eval honest
# ---------------------------------------------------------------------------


def _longest_common_span(a: str, b: str, min_len: int = 6) -> int:
    """Length of the longest shared verbatim substring >= min_len, else 0.

    Used to detect lexical_leak: if the question copies a long span from the
    chunk, BM25 matches it trivially and the case says nothing about whether
    fusion helps. Cheap O(len(a)*len(b)) is fine at this scale (short strings).
    """
    best = 0
    na = normalize_ws(a)
    for size in range(min(len(na), len(normalize_ws(b))), min_len - 1, -1):
        if size <= best:
            break
        for i in range(0, len(na) - size + 1):
            if na[i : i + size] in normalize_ws(b):
                best = size
                break
        if best:
            break
    return best


def _validate_case(raw: dict, chunk, kind_hint: str) -> tuple[GoldenQA | None, str]:
    """Turn one LLM item into a verified GoldenQA, or reject it with a reason.

    Rejection is the default: an unverifiable gold caps Recall@k below 1.0
    forever and every later tuning decision gets made against that noise.
    """
    q = str(raw.get("question") or "").strip()
    excerpt = str(raw.get("answer_excerpt") or "").strip()
    kind = str(raw.get("kind") or kind_hint).strip()
    if kind not in ("happy_path", "edge_case", "adversarial"):
        kind = "happy_path"
    if len(q) < 6:
        return None, "question too short"
    if not excerpt:
        return None, "no answer_excerpt"

    # THE core check: the excerpt must be verbatim in the chunk's indexed text.
    indexed = normalize_ws(chunk.text_to_index())
    needle = normalize_ws(excerpt)
    if not needle or needle not in indexed:
        return None, "excerpt not verbatim in chunk"
    if len(needle) < 8:
        return None, "excerpt too short to be a reliable anchor"

    # A question that is itself a verbatim copy of the chunk teaches nothing
    # about retrieval quality - BM25 wins by construction.
    qnorm = normalize_ws(q)
    if qnorm in indexed:
        return None, "question is a verbatim substring of the chunk"

    leak_span = _longest_common_span(q, chunk.text_to_index())
    qtoks = set(tokenize(q))
    ctoks = set(tokenize(chunk.text_to_index()))
    overlap = len(qtoks & ctoks) / max(1, len(qtoks))
    tags = [kind]
    if leak_span >= 10 or overlap >= 0.85:
        tags.append("lexical_leak")

    return GoldenQA(
        id="",  # assigned by the caller (needs a global counter)
        query=q,
        user_id=EVAL_USER,  # patched to the profile's user by the caller
        gold_chunk_keys=[chunk_key(chunk.doc_key, chunk.seq)],
        category=kind,
        tags=tags,
        ground_truth=excerpt,
        notes=f"leak_span={leak_span} tok_overlap={overlap:.2f}",
        answer_excerpts=[excerpt],
    ), ""


def _expand_any_of_gold(cases: list[GoldenQA], all_chunks: list) -> tuple[int, int]:
    """Make gold_chunk_keys any-of COMPLETE within the same doc (lesson #6).

    If a case's verbatim excerpt also appears in OTHER chunks of the SAME doc,
    those chunks are equally correct gold: a retrieval surfacing any of them
    answered the question. Leaving them out turns a correct retrieval into a
    scored miss - a labeling defect (rag-036), not a retrieval bug.

    Cross-doc matches are NOT expanded here: the same excerpt in two different
    guidelines is genuinely ambiguous about which doc the question meant, and
    ``rebind_golden`` deliberately drops those. We only complete within a doc,
    which is always safe (the question already names the doc via its context).

    Widely-recurring excerpts are a different animal and are NOT expanded. If
    the excerpt matches more than ``MAX_ANY_OF_GOLD`` siblings it is not an
    answer but a template sentence - a recipe footnote printed under all 54
    recipes, say. Such a case is REMOVED from ``cases`` (filtered in place,
    counted, and returned so the caller can log it) rather than given a gold
    set that no ranking can satisfy.

    Returns ``(n_expanded, n_removed)``. ``cases`` is filtered in place.
    """
    by_doc: dict[str, list] = {}
    for c in all_chunks:
        by_doc.setdefault(c.doc_key, []).append(c)

    grew = 0
    removed = 0
    kept: list[GoldenQA] = []
    for case in cases:
        if not case.answer_excerpts:
            kept.append(case)
            continue
        # the doc this case was generated from
        src_doc = case.gold_chunk_keys[0].split("#")[0]
        siblings = by_doc.get(src_doc, [])
        if len(siblings) <= 1:
            kept.append(case)
            continue
        keys = set(case.gold_chunk_keys)
        for ex in case.answer_excerpts:
            needle = normalize_ws(ex)
            if not needle:
                continue
            for c in siblings:
                k = chunk_key(c.doc_key, c.seq)
                if k in keys:
                    continue
                if needle in normalize_ws(c.text_to_index()):
                    keys.add(k)
        if len(keys) > MAX_ANY_OF_GOLD:
            removed += 1
            continue  # template-repeat: cannot discriminate -> drop, not expand
        if len(keys) > len(case.gold_chunk_keys):
            case.gold_chunk_keys = sorted(keys)
            grew += 1
        kept.append(case)
    cases[:] = kept
    return grew, removed


def _sample_evenly(chunks: list, cap: int) -> list:
    """Evenly spaced, ENDPOINT-INCLUSIVE sample of a doc's chunks (cap=0 -> all).

    Even spacing matters: the 3 big guidelines (79/70/64 pages) would otherwise
    contribute questions only from their first pages if we sampled a prefix,
    and a golden set that only asks about document openings measures nothing
    about deep retrieval. Endpoint-inclusive (linspace over indices) so the
    sample also covers each document's CONCLUSION, not just 0..(n-cap).
    """
    n = len(chunks)
    if cap <= 0 or n <= cap:
        return chunks
    if cap == 1:
        return [chunks[0]]
    idxs = [round(i * (n - 1) / (cap - 1)) for i in range(cap)]
    # round() can collide on small n; de-dup while preserving order/spread
    seen: set[int] = set()
    out = []
    for i in idxs:
        if i not in seen:
            seen.add(i)
            out.append(chunks[i])
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="v1", choices=sorted(_PROFILES),
                    help="which corpus profile to build the golden set for")
    args = ap.parse_args()
    prof = select_corpus(args.corpus)
    corpus_list: list[tuple[str, str, str]] = prof["corpus"]
    eval_user: int = prof["eval_user"]
    collection: str = prof["collection"]
    n_per_chunk: int = prof["questions_per_chunk"]
    cap_per_doc: int = prof["max_chunks_per_doc"]

    corpus = _corpus_dir()
    db_url = os.environ.get(
        "PI_ITEST_DATABASE_URL",
        "mysql+aiomysql://pi:pi_py_local@127.0.0.1:3306/pi_py_test",
    )
    milvus_uri = os.environ.get("PI_ITEST_MILVUS_URI", "http://127.0.0.1:19531")
    llm_base = (os.environ.get("OPENAI_BASE_URL") or "").rstrip("/")
    llm_key = os.environ.get("OPENAI_API_KEY") or ""
    llm_model = (os.environ.get("PI_MODEL") or "").split("/")[-1]
    if not (llm_base and llm_key and llm_model):
        raise SystemExit("OPENAI_BASE_URL / OPENAI_API_KEY / PI_MODEL required")

    emb_cfg = EmbeddingConfig(
        url=os.environ["PI_EMBEDDING_URL"],
        api_key=os.environ["PI_EMBEDDING_API_KEY"],
        model=os.environ["PI_EMBEDDING_MODEL"],
        
    )
    cfg = RagConfig(chunking=ChunkingConfig(max_chars=800, min_chars=100), embedding=emb_cfg)

    store = MysqlChunkStore(db_url, create_schema=True)
    vec = MilvusRagVectorStore(uri=milvus_uri, collection=collection)
    emb = HttpEmbedder(url=emb_cfg.url, api_key=emb_cfg.api_key,
                       model=emb_cfg.model)
    lex = MemoryBM25Index(store)
    pipe = IngestPipeline(store=store, embedder=emb, vector_store=vec,
                          config=cfg, lexical_index=lex)

    print(f"=== corpus profile: {args.corpus} "
          f"({len(corpus_list)} docs, user {eval_user}, collection {collection}) ===")
    t_start = time.time()
    try:
        assert await vec.ping(), "local Milvus unreachable"
        await vec.drop()
        for key, _, _ in corpus_list:
            await store.delete_doc(eval_user, key)

        # --- 1) ingest the WHOLE corpus (prose + tabular + data) ------------
        print("=== ingest (real embedding, local MySQL + Milvus) ===")
        usage_total = 0
        per_doc: dict[str, int] = {}
        for key, fname, kind in corpus_list:
            p = corpus / fname
            if not p.is_file():
                print(f"  SKIP missing {fname}")
                continue
            t0 = time.time()
            out = await pipe.ingest_file(p, user_id=eval_user, doc_key=key)
            per_doc[key] = out.chunks_stored
            usage_total += out.usage_tokens
            print(f"  {key:24s} {kind:8s} {out.status:18s} chunks={out.chunks_stored:4d} "
                  f"idx={out.chunks_indexed:4d} tok={out.usage_tokens:6d} {time.time()-t0:5.1f}s"
                  + (f"  <- {out.reason[:50]}" if out.reason else ""))
            if out.status == IngestStatus.NEEDS_HEAVY_PARSER.value:
                print(f"      (quality gate: scanned/complex -> not indexed, correct v1 behavior)")
        print(f"  TOTAL chunks={sum(per_doc.values())} embed_tokens={usage_total}")

        all_chunks = await store.list_chunks_for_user(eval_user)
        prose_keys = {k for k, _, kind in corpus_list if kind == "prose"}
        prose = [c for c in all_chunks if c.doc_key in prose_keys]
        print(f"\n=== corpus shape: {len(all_chunks)} chunks total, "
              f"{len(prose)} prose (questionable), "
              f"{len(all_chunks)-len(prose)} distractor ===")

        # --- 1b) per-doc even sampling (cost control for big corpora) -------
        if cap_per_doc > 0:
            by_doc: dict[str, list] = {}
            for c in prose:
                by_doc.setdefault(c.doc_key, []).append(c)
            questioned: list = []
            for dk in sorted(by_doc):
                questioned.extend(_sample_evenly(by_doc[dk], cap_per_doc))
            print(f"=== sampling: {len(prose)} prose chunks -> {len(questioned)} questioned "
                  f"(cap {cap_per_doc}/doc, evenly spaced) ===")
        else:
            questioned = prose

        # --- 2) generate + VERIFY questions with the real LLM --------------
        cases: list[GoldenQA] = []
        rejected: dict[str, int] = {}
        print(f"\n=== question generation ({llm_model}, {n_per_chunk}/chunk over "
              f"{len(questioned)} chunks) ===")
        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
            sem = asyncio.Semaphore(4)

            async def one(chunk) -> list[tuple[GoldenQA | None, str]]:
                async with sem:
                    try:
                        raw = await _llm_questions(
                            client, llm_base, llm_key, llm_model,
                            chunk.title_path, chunk.text_to_index(), n_per_chunk,
                        )
                    except Exception as exc:  # noqa: BLE001 - one chunk must not kill the build
                        return [(None, f"llm_error:{type(exc).__name__}")]
                    return [_validate_case(r, chunk, "happy_path") for r in raw]

            results = await asyncio.gather(*(one(c) for c in questioned))

        for pairs in results:
            for case, reason in pairs:
                if case is None:
                    key = reason.split(":")[0]
                    rejected[key] = rejected.get(key, 0) + 1
                else:
                    case.user_id = eval_user  # _validate_case used the module default
                    cases.append(case)

        # --- 3) de-dup + any-of gold expansion + assign stable ids ----------
        seen: set[str] = set()
        uniq: list[GoldenQA] = []
        for c in cases:
            sig = normalize_ws(c.query)[:60]
            if sig in seen:
                rejected["duplicate"] = rejected.get("duplicate", 0) + 1
                continue
            seen.add(sig)
            uniq.append(c)
        expanded, template_repeat = _expand_any_of_gold(uniq, all_chunks)
        for i, c in enumerate(uniq, start=1):
            c.id = f"rag-{i:03d}"

        by_cat: dict[str, int] = {}
        leaky = 0
        multi_gold = 0
        for c in uniq:
            by_cat[c.category] = by_cat.get(c.category, 0) + 1
            if "lexical_leak" in c.tags:
                leaky += 1
            if len(c.gold_chunk_keys) > 1:
                multi_gold += 1
        by_doc_cases: dict[str, int] = {}
        for c in uniq:
            dk = c.gold_chunk_keys[0].split("#")[0]
            by_doc_cases[dk] = by_doc_cases.get(dk, 0) + 1

        print(f"\n=== golden set ===")
        print(f"  cases: {len(uniq)} (rejected: {sum(rejected.values())} -> {rejected})")
        print(f"  by category: {by_cat}")
        print(f"  by doc: {by_doc_cases}")
        print(f"  lexical_leak tagged: {leaky} "
              f"({100*leaky/max(1,len(uniq)):.0f}% - these are BM25's free wins)")
        print(f"  any-of gold expanded: {expanded} cases now accept >1 chunk "
              f"({multi_gold} multi-gold total)")

        if len(uniq) < 50:
            print(f"\n  !! only {len(uniq)} cases; 对接文档 §6 wants >=50.")
            print(f"     Re-run with a larger n_per_chunk or more prose docs.")

        # --- 4) persist ----------------------------------------------------
        out_path = ROOT / "evals" / "tasks" / "rag" / prof["golden_file"]
        gs = GoldenSet(name=prof["golden_name"], cases=uniq)
        gs.save(out_path)
        print(f"\n  saved -> {out_path.relative_to(ROOT)}")

        # record the corpus shape next to the golden set: without it nobody can
        # tell later whether a metric change came from the retriever or from a
        # different corpus.
        meta = {
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "corpus_profile": args.corpus,
            "eval_user_id": eval_user,
            "milvus_collection": collection,
            "llm_model": llm_model,
            "embedding_model": emb_cfg.model,
            "chunking": {"max_chars": cfg.chunking.max_chars,
                         "min_chars": cfg.chunking.min_chars,
                         "overlap_chars": cfg.chunking.overlap_chars,
                         "contextual_prefix": cfg.chunking.contextual_prefix},
            "chunks_total": len(all_chunks),
            "chunks_by_doc": per_doc,
            "embed_tokens": usage_total,
            "questioned_chunks": len(questioned),
            "max_chunks_per_doc": cap_per_doc,
            "questions_per_chunk": n_per_chunk,
            "cases": len(uniq),
            "cases_by_category": by_cat,
            "cases_by_doc": by_doc_cases,
            "lexical_leak_cases": leaky,
            "any_of_gold_expanded": expanded,
            "multi_gold_cases": multi_gold,
            "template_repeat_dropped": template_repeat,
            "rejected": rejected,
            "notes": (
                "chunks_total includes distractor-only docs that are indexed but "
                "never questioned. gold_chunk_keys are valid ONLY for the chunking "
                "config above - use rebind_golden() after changing it. "
                + ("v2 scope: all docs are text-native PDFs; parse_pdf emits no "
                   "BLOCK_HEADING so every chunk's title_path is EMPTY - this is a "
                   "pure-prose vector-vs-lexical test, the title_path-symmetry fix "
                   "is validated on v1 (which has docx/md headings)."
                   if args.corpus == "v2" else
                   "v1 includes 351 homogeneous CSV rows as distractor mass; the "
                   "M4 'BM25 adds nothing' conclusion is suspected over-fit to them "
                   "- corpus v2 (medical PDFs) is the replication.")
            ),
        }
        meta_path = out_path.with_name(prof["golden_file"].replace(".json", ".meta.json"))
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  saved -> {meta_path.relative_to(ROOT)}")

        print(f"\n=== done in {time.time()-t_start:.0f}s ===")
    finally:
        # zero residue: the A/B runner re-ingests (and rebinds) itself
        for key, _, _ in corpus_list:
            await store.delete_doc(eval_user, key)
        await vec.drop()
        await vec.close()
        await store.dispose()


if __name__ == "__main__":
    asyncio.run(main())
