"""RAG evaluation harness - golden set, retrieval metrics, A/B reports.

铁律：先建评测，再调检索。Everything a tuning decision needs must be a
number this module can produce:

- GoldenQA: one eval case = query + gold chunk keys + expected answer facts.
  Compatible-in-spirit with pi.evals.schema.Task (id/category/tags) so cases
  can be exported to evals/tasks/rag/*.json for `pi-py eval run` (M5).
- Retrieval metrics: Recall@k / MRR / HitRate - computed from ranked
  chunk_key lists, no LLM needed (deterministic, cheap, CI-safe).
- ABReport: same golden set through N retrieval configs, side-by-side table.

chunk_key vs chunk_id: golden sets are authored BEFORE/AFTER ingest and must
survive re-ingest (SQL ids change). So cases reference stable
``doc_key#seq`` keys; the runner maps RetrievedChunk -> key via the store.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Golden set
# ---------------------------------------------------------------------------

CATEGORIES = ("happy_path", "edge_case", "adversarial")  # mirrors pi.evals Task.category


def chunk_key(doc_key: str, seq: int) -> str:
    """Stable, ingest-surviving identifier of a chunk."""
    return f"{doc_key}#{seq}"


@dataclass
class GoldenQA:
    id: str
    query: str
    user_id: int  # ACL is part of the case: which user's corpus to search
    gold_chunk_keys: list[str]  # any-of semantics for hit; all-of for recall
    category: str = "happy_path"
    tags: list[str] = field(default_factory=list)
    ground_truth: str = ""  # expected answer facts (generation-side judge, M4+)
    notes: str = ""
    # Verbatim snippet(s) copied out of the gold chunk(s). This is the DURABLE
    # ground truth: gold_chunk_keys are ``doc_key#seq``, and seq is a pure
    # function of the chunking config - the moment M4 A/Bs a different
    # max_chars, every key shifts and a key-only golden set silently turns into
    # garbage (all cases score 0, which LOOKS like a retrieval regression).
    # With an excerpt, ``rebind_golden`` recomputes the keys from the text that
    # actually matters.
    answer_excerpts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "query": self.query,
            "user_id": self.user_id,
            "gold_chunk_keys": self.gold_chunk_keys,
            "category": self.category,
            "tags": self.tags,
            "ground_truth": self.ground_truth,
            "notes": self.notes,
            "answer_excerpts": self.answer_excerpts,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GoldenQA":
        if d.get("category") not in CATEGORIES:
            raise ValueError(f"case {d.get('id')}: category must be one of {CATEGORIES}")
        if not d.get("gold_chunk_keys"):
            raise ValueError(f"case {d.get('id')}: gold_chunk_keys required (evals need a gold)")
        return cls(
            id=str(d["id"]),
            query=str(d["query"]),
            user_id=int(d.get("user_id", 1)),
            gold_chunk_keys=[str(k) for k in d["gold_chunk_keys"]],
            category=d.get("category", "happy_path"),
            tags=list(d.get("tags", [])),
            ground_truth=str(d.get("ground_truth", "")),
            notes=str(d.get("notes", "")),
            answer_excerpts=[str(x) for x in d.get("answer_excerpts", [])],
        )


@dataclass
class GoldenSet:
    name: str
    cases: list[GoldenQA] = field(default_factory=list)

    def by_category(self) -> dict[str, list[GoldenQA]]:
        out: dict[str, list[GoldenQA]] = {c: [] for c in CATEGORIES}
        for case in self.cases:
            out[case.category].append(case)
        return out

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(
                {"name": self.name, "cases": [c.to_dict() for c in self.cases]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> "GoldenSet":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        cases = [GoldenQA.from_dict(c) for c in data["cases"]]
        ids = [c.id for c in cases]
        if len(ids) != len(set(ids)):
            raise ValueError(f"golden set {path}: duplicate case ids")
        return cls(name=data.get("name", Path(path).stem), cases=cases)


# ---------------------------------------------------------------------------
# Re-anchoring: keep a golden set valid across chunking changes
# ---------------------------------------------------------------------------


def normalize_ws(text: str) -> str:
    """Whitespace-insensitive comparison form for excerpt matching.

    Public because it IS part of the gold-anchoring contract: whoever authors
    or validates an ``answer_excerpt`` (tools/build_golden_set.py, a future
    bad-case rewriter) must compare text exactly the way ``rebind_golden``
    does, or the two will disagree about what counts as a match.

    Chunkers reflow whitespace (joining blocks, stripping markup, normalizing
    newlines), so an excerpt captured under one chunking run must still match
    under another. ONLY whitespace is normalized - never case or punctuation:
    loose matching here would let a wrong chunk become gold and silently inflate
    every metric, which is worse than an eval that fails loudly.
    """
    return " ".join((text or "").split())


@dataclass
class RebindReport:
    """What happened when a golden set was re-anchored to new chunking."""

    total: int = 0
    rebound: int = 0  # keys moved, but the excerpt was found -> repaired
    unchanged: int = 0  # keys identical (this chunk did not move)
    unresolved: int = 0  # excerpt matched NO chunk -> case dropped
    ambiguous: int = 0  # excerpt matched chunks in >1 doc -> case dropped
    no_excerpt: int = 0  # legacy case with keys only -> kept, but at risk
    template_repeat: int = 0  # excerpt matched > MAX_ANY_OF_GOLD chunks -> dropped
    dropped_ids: list[str] = field(default_factory=list)
    ambiguous_ids: list[str] = field(default_factory=list)
    template_repeat_ids: list[str] = field(default_factory=list)

    def markdown(self) -> str:
        lines = [
            "## Golden set rebind",
            "",
            f"- cases in: {self.total}",
            f"- unchanged: {self.unchanged}",
            f"- rebound (keys moved, excerpt found): {self.rebound}",
            f"- ambiguous (excerpt in >1 doc): {self.ambiguous}"
            + (f" -> {self.ambiguous_ids[:10]}" if self.ambiguous_ids else ""),
            f"- unresolved (excerpt gone): {self.unresolved}"
            + (f" -> {self.dropped_ids[:10]}" if self.dropped_ids else ""),
            f"- template-repeat (excerpt recurs in > {MAX_ANY_OF_GOLD} chunks): "
            f"{self.template_repeat}"
            + (f" -> {self.template_repeat_ids[:10]}" if self.template_repeat_ids else ""),
            f"- legacy key-only (NOT rebindable): {self.no_excerpt}",
        ]
        return "\n".join(lines)


def rebind_golden(golden: "GoldenSet", chunks: list) -> tuple["GoldenSet", RebindReport]:
    """Re-derive every case's gold_chunk_keys from its answer_excerpts.

    Why this exists: M4 A/Bs chunking parameters, but a golden set pinned to
    ``doc_key#seq`` is only valid for the exact chunking that produced it.
    Rebinding keeps the DURABLE part (the answer text) and recomputes the
    VOLATILE part (the keys), so tuning chunk size cannot masquerade as a
    retrieval regression.

    Contract (deliberately conservative - a wrong gold poisons every metric):
      - cases without excerpts keep their keys and are counted separately; they
        are exactly the ones at risk of going stale.
      - an excerpt matching chunks in MORE THAN ONE doc is ambiguous: it cannot
        prove which chunk is gold, so the case is DROPPED, not guessed.
      - an excerpt matching nothing means the text is gone (a parser/chunker
        change dropped it) -> dropped loudly, never kept as a silent key-only.
    """
    rep = RebindReport(total=len(golden.cases))
    by_doc: dict[str, list] = {}
    for c in chunks:
        by_doc.setdefault(c.doc_key, []).append(c)

    new_cases: list[GoldenQA] = []
    for case in golden.cases:
        if not case.answer_excerpts:
            new_cases.append(case)
            rep.no_excerpt += 1
            continue

        matched: dict[str, set[int]] = {}  # doc_key -> {seq}
        for ex in case.answer_excerpts:
            needle = normalize_ws(ex)
            if not needle:
                continue
            for doc_key, doc_chunks in by_doc.items():
                for c in doc_chunks:
                    # Index the SAME text the lexical channel sees
                    # (title_path + body): an excerpt may legitimately come
                    # from a heading, and rebinding must find it there too.
                    if needle in normalize_ws(c.text_to_index()):
                        matched.setdefault(doc_key, set()).add(int(c.seq))

        if not matched:
            rep.unresolved += 1
            rep.dropped_ids.append(case.id)
            continue
        if len(matched) > 1:
            rep.ambiguous += 1
            rep.ambiguous_ids.append(case.id)
            continue

        doc_key, seqs = next(iter(matched.items()))
        if len(seqs) > MAX_ANY_OF_GOLD:
            # Same guard as build_golden_set._expand_any_of_gold: an excerpt
            # matching this many chunks of one doc is a recurring template, not
            # an answer. Scoring it would be meaningless (hit@k = 1 always), so
            # drop it LOUDLY rather than rebuild a gold set nothing can satisfy.
            rep.template_repeat += 1
            rep.template_repeat_ids.append(case.id)
            continue
        keys = sorted(chunk_key(doc_key, s) for s in seqs)
        if keys == sorted(case.gold_chunk_keys):
            rep.unchanged += 1
        else:
            rep.rebound += 1
        new_cases.append(
            GoldenQA(
                id=case.id, query=case.query, user_id=case.user_id,
                gold_chunk_keys=keys, category=case.category, tags=case.tags,
                ground_truth=case.ground_truth, notes=case.notes,
                answer_excerpts=case.answer_excerpts,
            )
        )

    return GoldenSet(name=golden.name, cases=new_cases), rep


# ---------------------------------------------------------------------------
# Metrics (retrieval side, deterministic)
# ---------------------------------------------------------------------------


def recall_at_k(ranked_keys: list[str], gold_keys: set[str], k: int) -> float:
    """ALL-OF fraction: |gold found in top-k| / |gold|. SECONDARY metric.

    The PRIMARY metric is ``hit_at_k`` (any-of): a RAG pipeline only needs one
    answer-bearing chunk, so "did we find it" beats "did we find all of it".
    Use this one for coverage-completeness questions (multi-part answers) with
    the meaning below in mind - it is NOT what the headline number should be.

    Deliberately all-of, NOT any-of (any-of is ``hit_at_k``). Hand-computed in
    ``test_recall_at_k_hand_computed``. Two consequences (see
    ``evals/reports/badcase_v2_lost4_diagnosis.md``):

    1. A single-key gold yields ``1.0 or 0.0`` (that is what the old docstring
       was trying to say). A MULTI-key gold yields ``found / N``.
    2. Therefore an over-expanded any-of gold (``_expand_any_of_gold`` adding
       dozens of "equivalent" chunks in a structurally-repetitive doc) caps
       recall@k at ``k / N`` and structurally under-reports it - rag-017 had 54
       gold keys, so recall@5 could never exceed 0.093. Keep gold small; the
       metric cannot credit more than k keys.
    """
    if not gold_keys:
        return 0.0
    found = sum(1 for key in ranked_keys[:k] if key in gold_keys)
    return found / len(gold_keys)


def hit_at_k(ranked_keys: list[str], gold_keys: set[str], k: int) -> bool:
    """Binary: at least one gold key in top-k.

    THIS IS THE PRIMARY RAG METRIC (any-of semantics). A RAG pipeline needs only
    ONE retrieved chunk that contains the answer in order to answer the
    question, so "did we find it" is the question that matters. Unlike
    ``recall_at_k`` (all-of), it is IMMUNE to gold-set size: a 1-key gold and a
    54-key gold both score 1.0 on a hit, so an over-expanded any-of gold cannot
    deflate it. See ``evals/reports/badcase_v2_lost4_diagnosis.md``.

    ``recall_at_k`` remains as the SECONDARY metric: coverage completeness
    ("did we find ALL the relevant chunks"), which matters for multi-part
    answers but is structurally capped at ``k / |gold|`` when ``|gold| > k``.
    """
    return any(key in gold_keys for key in ranked_keys[:k])


def reciprocal_rank(ranked_keys: list[str], gold_keys: set[str]) -> float:
    """1/rank of the FIRST gold hit, else 0."""
    for i, key in enumerate(ranked_keys, start=1):
        if key in gold_keys:
            return 1.0 / i
    return 0.0


@dataclass
class CaseScore:
    case_id: str
    category: str
    ranked_keys: list[str]
    recall_at: dict[int, float]  # k -> recall
    hit_at: dict[int, bool]
    rr: float
    error: str | None = None  # retrieval raised / degraded unexpectedly
    # Carried through from GoldenQA so reports can slice by tag, not just by
    # category. 归因 needs this: the interesting cut for hybrid-vs-vector is
    # "does BM25 help or hurt on cases whose question copies the passage?"
    # (tag lexical_leak) - and that is orthogonal to happy_path/edge_case.
    tags: list[str] = field(default_factory=list)


@dataclass
class EvalReport:
    """Aggregate over one golden set x one retrieval config."""

    config_name: str
    total: int = 0
    failed: int = 0  # cases where retrieval errored
    metrics: dict[str, float] = field(default_factory=dict)  # hit@k, mrr, recall@k (primary first)
    per_category: dict[str, dict[str, float]] = field(default_factory=dict)
    per_tag: dict[str, dict[str, float]] = field(default_factory=dict)
    cases: list[CaseScore] = field(default_factory=list)

    def markdown(self) -> str:
        lines = [
            f"## Eval report: {self.config_name}",
            "",
            f"- cases: {self.total} (retrieval errors: {self.failed})",
            "- primary metric: hit@k (any-of: one gold chunk is enough to answer;"
            " immune to gold-set size)",
        ]
        for name, val in self.metrics.items():
            lines.append(f"- {name}: **{val:.3f}**")
        if self.per_category:
            lines += ["", "| category | n | hit@5 | recall@5 | mrr |", "|---|---|---|---|---|"]
            for cat, m in self.per_category.items():
                lines.append(
                    f"| {cat} | {int(m.get('n', 0))} | {m.get('hit@5', 0):.3f} "
                    f"| {m.get('recall@5', 0):.3f} | {m.get('mrr', 0):.3f} |"
                )
        if self.per_tag:
            lines += ["", "| tag | n | hit@5 | recall@1 | recall@5 | mrr |",
                      "|---|---|---|---|---|---|"]
            for tag, m in self.per_tag.items():
                lines.append(
                    f"| {tag} | {int(m.get('n', 0))} | {m.get('hit@5', 0):.3f} "
                    f"| {m.get('recall@1', 0):.3f} | {m.get('recall@5', 0):.3f} "
                    f"| {m.get('mrr', 0):.3f} |"
                )
        bad = [c for c in self.cases if not c.hit_at.get(5, False)]
        if bad:
            lines += ["", "### bad cases (hit@5 = 0)"]
            for c in bad[:20]:
                err = f" [error: {c.error}]" if c.error else ""
                tags = f" tags={c.tags}" if c.tags else ""
                lines.append(
                    f"- `{c.case_id}` ({c.category}){tags}{err}: top5={c.ranked_keys[:5]}"
                )
        return "\n".join(lines)


KS = (1, 3, 5)

# The largest gold set a top-k can ever satisfy. Above this, an "any-of gold" is
# really a template sentence recurring across one document - a recipe footnote
# printed under all 54 recipes - and the case cannot discriminate (almost any
# top-k contains one of them, so hit@k = 1 for every config) while its all-of
# recall ceiling is k/N. Both the generator (build_golden_set._expand_any_of_gold)
# and the rebind path refuse to expand such a case and DROP it instead.
# See evals/reports/badcase_v2_lost4_diagnosis.md.
MAX_ANY_OF_GOLD = max(KS)


def aggregate(config_name: str, case_scores: list[CaseScore]) -> EvalReport:
    rep = EvalReport(config_name=config_name, total=len(case_scores))
    rep.failed = sum(1 for c in case_scores if c.error)
    ok = [c for c in case_scores if not c.error]

    def mean(vals: list[float]) -> float:
        return sum(vals) / len(vals) if vals else 0.0

    # PRIMARY metrics first (hit@k any-of, then mrr), secondary all-of recall
    # last. The order is not cosmetic: ``ab_markdown`` renders metrics in dict
    # order, so this decides what leads every A/B table the project publishes.
    for k in KS:
        rep.metrics[f"hit@{k}"] = mean([1.0 if c.hit_at[k] else 0.0 for c in ok])
    rep.metrics["mrr"] = mean([c.rr for c in ok])
    for k in KS:
        rep.metrics[f"recall@{k}"] = mean([c.recall_at[k] for c in ok])

    by_cat: dict[str, list[CaseScore]] = {}
    for c in ok:
        by_cat.setdefault(c.category, []).append(c)
    for cat, cases in by_cat.items():
        rep.per_category[cat] = {
            "n": float(len(cases)),
            "recall@5": mean([c.recall_at[5] for c in cases]),
            "mrr": mean([c.rr for c in cases]),
            "hit@5": mean([1.0 if c.hit_at[5] else 0.0 for c in cases]),
        }

    # Per-TAG slices. Categories say what KIND of question it is; tags say what
    # PROPERTY of the case we are testing a hypothesis about. M4's question
    # ("does BM25 hurt when the corpus is dominated by short tabular rows?")
    # is a tag question (lexical_leak), not a category question.
    by_tag: dict[str, list[CaseScore]] = {}
    for c in ok:
        for t in c.tags:
            by_tag.setdefault(t, []).append(c)
    for tag, cases in sorted(by_tag.items()):
        rep.per_tag[tag] = {
            "n": float(len(cases)),
            "recall@1": mean([c.recall_at[1] for c in cases]),
            "recall@5": mean([c.recall_at[5] for c in cases]),
            "mrr": mean([c.rr for c in cases]),
            "hit@5": mean([1.0 if c.hit_at[5] else 0.0 for c in cases]),
        }

    rep.cases = case_scores
    return rep


def ab_markdown(reports: list[EvalReport]) -> str:
    """Side-by-side A/B table (纯向量 vs 混合 vs 混合+rerank 一眼看出谁好)."""
    if not reports:
        return "(no reports)"
    metric_names = list(reports[0].metrics.keys())
    lines = ["| metric | " + " | ".join(r.config_name for r in reports) + " |",
             "|---" * (len(reports) + 1) + "|"]
    for name in metric_names:
        vals = [r.metrics.get(name, 0.0) for r in reports]
        best = max(vals)
        cells = []
        for v in vals:
            cells.append(f"**{v:.3f}**" if v == best and len(vals) > 1 else f"{v:.3f}")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def case_diff(base: EvalReport, other: EvalReport, k: int = 5) -> dict[str, list[CaseScore]]:
    """Which cases MOVED between two configs, and in which direction.

    Aggregate A/B tables answer "which config is better on average" - they
    cannot answer "why". 归因 needs the per-case delta: fusion that wins on 8
    cases and loses on 8 shows a flat mean while being two different bugs.

    Returns {"gained": [...], "lost": [...], "same": [...]} where gained means
    ``other`` ranks the gold strictly better than ``base`` at top-k (by
    reciprocal rank, which is finer-grained than a binary hit). Cases present
    in only one report are ignored (different golden sets are not comparable).
    """
    base_by_id = {c.case_id: c for c in base.cases}
    out: dict[str, list[CaseScore]] = {"gained": [], "lost": [], "same": []}
    for oc in other.cases:
        bc = base_by_id.get(oc.case_id)
        if bc is None:
            continue
        if oc.rr > bc.rr:
            out["gained"].append(oc)
        elif oc.rr < bc.rr:
            out["lost"].append(oc)
        else:
            out["same"].append(oc)
    return out


def diff_markdown(base: EvalReport, other: EvalReport, k: int = 5,
                  limit: int = 25) -> str:
    """Render case_diff as a readable attribution table."""
    d = case_diff(base, other, k)
    lines = [
        f"## Case diff: {base.config_name} -> {other.config_name}",
        "",
        f"- gained (gold ranked better): **{len(d['gained'])}**",
        f"- lost (gold ranked worse): **{len(d['lost'])}**",
        f"- unchanged: {len(d['same'])}",
        "",
        f"net = {len(d['gained']) - len(d['lost']):+d}",
    ]
    base_by_id = {c.case_id: c for c in base.cases}
    for label, key in (("LOST", "lost"), ("GAINED", "gained")):
        rows = d[key][:limit]
        if not rows:
            continue
        lines += ["", f"### {label} cases (top {len(rows)})", "",
                  "| case | tags | rr before -> after | top-3 after |",
                  "|---|---|---|---|"]
        for c in rows:
            b = base_by_id.get(c.case_id)
            before = f"{b.rr:.2f}" if b else "?"
            top3 = ", ".join(c.ranked_keys[:3]) or "(empty)"
            lines.append(f"| `{c.case_id}` | {','.join(c.tags) or '-'} "
                         f"| {before} -> {c.rr:.2f} | {top3} |")
    return "\n".join(lines)
