# 4 residual LOST cases on corpus_v2 — root-cause diagnosis

- mined: 2026-09-29, on the 81-case `lexical_leak`-excluded subset of
  `corpus_v2.json` (user 992002, 527 chunks / 13 docs)
- cases: `rag-038`, `rag-045`, `rag-094`, `rag-096` — the 4 that
  `hybrid_rrf60+rerank` ranked WORSE than `vector_only` in
  `ab_20260929_112152.md` (net still +30, gained 34)
- tools: `tools/mine_badcases.py --corpus v2` (per-case text + scores),
  `tools/_probe_rerank_saturation.py` (corpus-wide score discrimination),
  ad-hoc MySQL probes (chunk lengths, duplicates, title_path coverage)

## Headline

**None of the 4 is a retrieval or rerank bug.** Three are measurement
artifacts of the golden set / corpus; one is a corpus-quality artifact of PDF
table extraction. M4 gate (b) therefore stands: `hybrid+rerank` winning +30 net
is not being propped up by a broken tail.

Two hypotheses formed while reading `ab_20260929_112152.md` were **REFUTED by
measurement** before any code was touched — recorded here so nobody re-chases
them.

## Refuted hypothesis 1: "adjacent chunks are near-duplicates, so the cross-encoder can't tell them apart"

The A/B report's `top-3 after` columns for rag-094/096 looked like consecutive
`seq` values from one document, which suggested duplicated content.

Measured over the whole v2 corpus (canonicalized: whitespace/pipe/control chars
stripped, then exact-or-90%-containment match between `seq`-adjacent chunks):

- adjacent duplicate pairs: **3 / 514 (0.6%)**
- canonical duplicates anywhere in the corpus: **5 / 527 (0.9%)**

0.6% cannot explain 4 lost cases. **REFUTED.**

## Refuted hypothesis 2: "rerank scores saturate at +1.0000, so rerank degenerates into serving the RRF order while reporting maximum confidence"

`mine_badcases.py` prints rag-094's rerank top-5 as `+1.0000 / +1.0000 /
+1.0000 / +1.0000 / +1.0000` and rag-096's top-4 as all `+1.0000`. If that were
真 saturation, rerank would be silently inert on a large slice of the corpus —
a serious finding, and the only mechanism that explains losing a case
`vector_only` had at rank 1.

Measured at **full precision** across all 81 cases (`top1 - top5` spread):

| band (by top-1 score) | n | mean spread | gold NOT at rank 1 |
|---|---|---|---|
| [0.0, 0.5) | 0 | — | — |
| [0.5, 0.9) | 6 | 0.301022 | 2/6 |
| [0.9, 0.99) | 37 | 0.441129 | 2/37 |
| [0.99, 1.0001) | 38 | 0.278744 | 3/38 |

- exactly-tied top-5 (`spread == 0`): **0 / 81**
- overall top-5 spread: mean **0.3546**, median **0.3870**
- top-1 score: mean 0.9690, median 0.9850

The high-confidence band (38 cases, top-1 ≥ 0.99) still discriminates with a
mean spread of 0.279, and its gold-at-rank-1 rate (35/38) is statistically
indistinguishable from the low-confidence bands. **Rerank is not inert.
REFUTED.**

### Why the report looked saturated

`mine_badcases.py` formats scores with `:.4f`. rag-094's real values are
`top1 = 0.9999990068781563`, `spread = 1.48e-05` — five distinct scores that
all print as `+1.0000`. **The saturation was in the display format, not the
model.** Lesson recorded in ARCHITECTURE §21.6.2: never diagnose score
behaviour from a fixed-precision print; measure the spread.

## Per-case root causes

### rag-045 — golden-set defect (control-character garbage). Action: drop the case.

```
QUERY : 根据这份标题路径为空、正文由控制字符和竖线组成的企业知识库片段，其正文的完整字符序列是什么？
GOLD  : med-insomnia-2023#16   (len = 25 chars, printable ratio = 0.520)
GT    : '\x06 \x05 \x05 \x04 \x04 \x04 \x05 | \x06 \x03 \x06 \x08 \x04'
```

The gold chunk is 25 characters of PDF-extraction garbage — not text, not a
table, not anything a retrieval system should be expected to pinpoint. The
question asks for "the complete character sequence of the body", i.e. it asks
the system to reproduce a specific run of control bytes.

Rerank's behaviour is **correct**: it scores #20 / #19 / #18 / #21 / #17 at
0.905–0.939, because those chunks ARE the nearest neighbours of a control-char
blob in embedding space. `vector_only` had the gold at rank 5 (0.5476) —
essentially a coin flip among five interchangeable garbage chunks.

A second case, **rag-046**, has the same defect (`GT = '| \x06 \x03 \x06 \x08
\x04'`). A corpus-wide scan found **2 / 151** cases whose ground_truth is
dominated by control characters.

This is exactly the failure mode `mine_badcases.py`'s docstring warns about: "a
golden set that encodes a parsing defect will 'fail' forever and send you
chasing a retrieval bug that does not exist." Confirmed present, not
hypothetical.

### rag-038 — corpus defect: one PDF table extracted twice, in two different layouts. Action: none (corpus-side).

```
QUERY : 在这条1100~1200千卡菜谱原料记录里，与热量区间相邻的重量字段是什么？
GOLD  : med-grassroots-2025#15   (len = 38 chars, page 9)
GT    : '毛重 | 1 100~1 200 kcal'
```

Both `#15` and `#16` come from page 9 and contain the *same* appendix table:

- `#15` (38 chars): `'cal 原料 | 毛重 | 1 100~1 200 kcal 菜谱 | 原料'` — a
  pipe-delimited fragment
- `#16` (1958 chars): `'· · 612 · · 中华内科杂志 ... Chin J Intern Med, July
  2025, Vol.64, No.7 附录3 食谱举例 ... 1 200~1 300 kcal 1 100~1 200 kcal /
  菜谱 原料 毛重 菜谱 原料 毛重 / 早餐 蒸山药 ...'` — the same table in
  reading order, with the full surrounding context

pdfplumber emitted the table twice in two layouts. `#15` survived as a separate
chunk because table blocks are **atomic** in the chunker (`_ATOMIC_KINDS`, never
merged into paragraph flow), so `min_chars=100` runt-merging did not absorb it.

Rerank scoring `#16` at 0.884 over `#15` is **defensible**: #16 is the
1958-char chunk that actually contains the table plus its caption, page header
and neighbouring rows. `_expand_any_of_gold` did not add `#16` to the gold
because the GT's verbatim pipe form (`'毛重 | 1 100~1 200 kcal'`) does not
appear in #16's pipe-free layout.

Note this is not a systemic layout problem: only **1 / 527** chunks match the
journal-header/footer pattern.

### rag-094 — cross-encoder has no signal left once title_path is empty. Action: corpus-side (heading extraction), not retriever-side.

```
QUERY : 在总能量约1200kcal的秋季食谱1中，蛋白质、碳水化合物和脂肪分别占能量的多少？
GOLD  : med-obesity-diet-2024#71,#72,#81,#82,#86   (any-of)
GT    : '蛋白质20%，碳水化合物56%，脂肪24%'
full-precision rerank: top1 = 0.9999990068781563, spread = 1.4848e-05
                       -> lowest discrimination of all 81 cases; gold_rank = None
```

The gold labelling is **partly wrong** — `#71`/`#72`/`#81`/`#82` genuinely contain
秋季食谱1 (1200kcal) and its note reads `蛋白质20%，碳水化合物56%，脂肪24%`, but
`#86` is actually **冬季食谱3**: the same ratio string recurs under a different
section heading and the any-of expansion promoted it (see the measured
correction in the Follow-up section).

What fails is discrimination. This document is a recipe compendium: **every**
season × every recipe has a note in the identical template

> `注：1.本食谱提供能量约为 XXXkcal，其中蛋白质 Ng，碳水化合物 Ng 及脂肪 Ng；宏量营养素占总能量比约为：蛋白质A%，碳水化合物B%，脂肪C%。`

The query's distinguishing feature is *which* recipe ("秋季食谱1"). That
information lives in the **heading**. And in corpus v2:

```
title_path non-empty: 0 / 527      (pdfplumber yields no heading layer)
```

So the cross-encoder — which scores `text_to_score()` = `title_path + body`, the
M4 fix that repaired the v1 edge_case regression — receives an **empty**
title_path for every candidate. All it sees is template-identical body text. It
compresses 20 candidates into a 1.5e-05 spread and the order collapses to the
RRF input order, which put #48/#36/#60/#59/#35 on top.

This is the *same class* of bug as M4's `text_to_score()` asymmetry, but the
fix is not in the retriever: **the v2 corpus has no headings to propagate.**
It is a parser/heading-extraction limitation on PDF, and it bounds how much
rerank can ever help on heading-dependent queries in this corpus.

### rag-096 — `_expand_any_of_gold` over-expanded across sections. Action: real generator defect (see below).

```
QUERY : 在总能量约1600kcal的春季食谱3的营养注释中，脂肪供能占比是多少？
GOLD  : med-obesity-diet-2024#37,#38,#79,#80
GT    : '碳水化合物54%，脂肪27%'
```

Checked which section each gold chunk's GT string actually belongs to:

| chunk | season headers it contains | note kcal values present | GT is the note of |
|---|---|---|---|
| `#37` | 冬季食谱1 (1200), 冬季食谱2 (1400), 冬季食谱3 (1600) | 1200, 1400 | **冬季食谱1 (1200kcal)** |
| `#38` | same as #37 | 1200, 1400 | **冬季食谱1 (1200kcal)** |
| `#79` | 春季食谱3 (1600), 夏季食谱1 (1200), 夏季食谱2 (1400) | 1400, 1600, 1200 | 春季食谱3 (1600kcal) ✓ |
| `#80` | same as #79 | 1400, 1600, 1200 | 春季食谱3 (1600kcal) ✓ |

The query asks about **春季食谱3 @1600kcal**. `#79`/`#80` are correct gold.
`#37`/`#38` are **wrong gold**: the string `碳水化合物54%，脂肪27%` in them is
the note of **冬季食谱1 @1200kcal** — a *different* recipe that happens to have
an identical macronutrient ratio.

Rerank actually did the right thing: it put the true gold `#80` at rank 4 and
scattered `#31/#32/#43` (other recipe notes) above it. The measured `rr`
(1.00 → 0.25) is diluted by two keys that should never have been gold.

**Generator defect.** `_expand_any_of_gold` (lesson #6, added for rag-036)
assumes:

> if a case's verbatim excerpt also appears in OTHER chunks of the SAME doc,
> those chunks are equally correct gold

That assumption holds when the excerpt is distinctive. In a **structurally
repetitive** document (a recipe compendium where the same ratio sentence recurs
under different section headings) it silently promotes chunks from the wrong
section. The expansion has no notion of "which section was the question about".

This is the one genuinely actionable finding of the four.

## Verdict on M4 gate (b)

Gate (b) — does the v1 conclusion generalize to a second, heterogeneous corpus
— was already answered YES by `ab_20260929_112152.md` (hybrid+rerank net +30,
recall@5 0.663 → 0.895). The remaining question was whether the 4-case tail hid
a real regression.

It does not:

| case | root cause | whose fault | retrieval fixable? |
|---|---|---|---|
| rag-045 | control-char garbage gold (+rag-046 same) | golden-set generator | no — drop the case |
| rag-038 | one PDF table extracted twice, two layouts | corpus / pdfplumber | no — corpus-side |
| rag-094 | title_path empty in 527/527 → cross-encoder blind to "which recipe" **PLUS wrong-section gold #86** | corpus / PDF heading extraction + generator | no — parser-side (+generator) |
| rag-096 | `_expand_any_of_gold` promoted wrong-section chunks | golden-set generator | **yes — generator guard** |

So of the 4: **2 are golden-set defects, 2 are corpus defects, 0 are retriever
or rerank defects.** The +30 net is clean.

## Recommended actions (in priority order)

> ⚠️ **P1 below was REFUTED by measurement** and superseded by P0 — see the
> Follow-up section. Kept for the record of what was tried.

1. ~~**P1 — `_expand_any_of_gold` count threshold.**~~ **REFUTED.** Measured the
   excerpt-hit count across all 43 expanded cases: legit rag-094 (5 chunks) and
   wrong rag-096 (4 chunks) sit *adjacent* — no count threshold separates them.
   The distinguishing signal is not "how many", but "is the expansion textually
   identical to a sibling chunk (same section) vs merely ratio-similar (different
   section)". That is the P0 structural signal below.
2. **P2 — reject control-char chunks at golden-set generation time.** Add a
   quality gate in `_validate_case`: if the chunk's `text_to_index()` has a
   printable ratio below a threshold (rag-045's gold is 0.520), refuse to make
   it a gold anchor. Removes rag-045 and rag-046 and prevents recurrence.
3. **P3 — record the title_path limitation as a known corpus bound.** corpus v2
   has `title_path` empty in 527/527 chunks, so any heading-dependent query is
   unanswerable-by-construction for the rerank stage. This should be stated in
   the A/B report's verdict, not silently absorbed into the recall number, and
   it motivates PDF heading extraction as a separate parser work item.
4. **P3 — print scores at full precision in `mine_badcases.py`** — **DONE.**
   The `.4f` format created a false saturation signal; near-1.0 scores now
   print at full precision (see Follow-up).

None of these change retrieval behaviour, so **no new A/B is required** beyond
regenerating the golden set and confirming the metrics move in the expected
direction (fewer LOST, unchanged GAINED).

## Follow-up: measurements that corrected this report

This report was written from `mine_badcases.py` output and two hypotheses that
were later **measured and overturned**. The corrections are recorded here so
the diagnosis record is honest about what survived contact with the data.

**1. The "adjacent-duplicate" hypothesis (already refuted above) held.** The
corpus-wide duplicate scan (0.6% adjacent dup pairs) was run before the report
and stands.

**2. The "rerank saturation" hypothesis was refuted — that is what triggered
`_probe_rerank_saturation.py`.** Full-precision measurement over all 81 cases:
exactly-tied top-5 (`spread == 0`) = **0/81**; overall top-5 spread mean 0.3546;
the near-1.0 band (38 cases, top-1 ≥ 0.99) still discriminates (mean spread
0.279). The "+1.0000" wall in `mine_badcases.py` was the `:.4f` print format,
not the model. **Fixed**: `mine_badcases.py` now prints scores ≥ 0.9995 at full
precision.

**3. The "rag-094 gold is clean" assumption was wrong — it has the same
wrong-section defect as rag-096.** A per-key section-ownership check (which
section header immediately precedes the GT string inside each chunk) showed:

- `#71 #72 #81 #82` → 秋季食谱1 ✓, but `#86` → **冬季食谱3** ✗
- `#79 #80` → 春季食谱3 ✓, but `#37 #38` → **冬季食谱1** ✗

So rag-094 and rag-096 are **the same bug class**: `_expand_any_of_gold` in a
structurally-repetitive document promotes chunks whose *ratio-string matches*
but whose *section does not*. Both LOST cases are inflated by wrong-section
gold, and the measured rr drop (1.00 → 0.00 / 0.25) partly counts chunks that
should never have been gold.

**4. The P0 fix needs the section-level signal that v2 does not carry.** To
correctly expand (or to bound any-of gold) in a repetitive document you need to
know "does this sibling chunk belong to the same section as the source chunk".
That signal is exactly the **title_path / heading** — which is `''` in 527/527
v2 chunks (pdfplumber emits no heading layer). Two consequences:

- Fixing `_expand_any_of_gold` properly is **blocked on P3** (PDF heading
  extraction). Without headings, the best available guard is the **structural
  text-overlap signal**: same-section siblings are near-duplicates (overlap
  ≈ 1.0, e.g. rag-016/018/020 ≈ 0.98), wrong-section siblings are
  ratio-similar-but-not-identical (≈ 0.5–0.7, e.g. rag-097 0.695, rag-096
  0.638). Measured overlap spectrum:
  * same-section any-of expansions: token-overlap ≈ **0.98–1.00**
  * wrong-section any-of expansions: token-overlap ≈ **0.46–0.70**
  * but corpus-duplicate expansions (rag-017 = 54 keys): ≈ 0.28–0.47

  The overlap signal is real but tri-modal, and the corpus-duplicate mode
  (54-key cases) sits in the overlap valley. No single overlap threshold does
  all three jobs, which is why this is a **decision**, not a one-line fix.

**5. The bigger problem this surfaced: `recall_at_k` is all-of, not any-of.**
`test_recall_at_k_hand_computed` pins `recall_at_k` to `|gold ∩ top-k| / |gold|`
(all-of), while the docstring claimed "any-of gold sets -> 1.0 or 0.0" (which is
actually `hit_at_k`). Combined with over-expansion, this structurally
under-reports: on the set that actually gets scored (see the caveat below),
perfect retrieval reports recall@5 = **0.9708** (142 cases) / **0.9662**
(lexical_leak excluded, 81 cases) instead of 1.0, and a 54-key case caps
recall@5 at **0.093**.

> **Measurement caveat (added 2026-10-01, after a wrong intermediate number).**
> An earlier draft of this note quoted **0.9545**, computed by scoring the raw
> 151-case file. That is WRONG as a statement about the eval: `ab_rag` runs
> `rebind_golden()` before scoring, and rebind drops cross-doc-ambiguous cases,
> so the scored set was **142**, not 151. Recomputed on the scored set the
> ceiling is **0.9708** (142) / **0.9662** (81) - which is exactly what the
> original A/B reports had always been up against. Lesson: *"cases in the file"
> != "cases that get scored"* - compute any ceiling on the post-rebind set.

`recall_at_k`'s docstring was corrected to state the all-of semantics
explicitly; the metric itself is unchanged (all-of is the correct definition for
recall; the fix belongs in the *expander*, not the metric). Resolved end-to-end
in the section below.

## Resolution (2026-10-01): metric semantics fixed + golden set pruned

User signed off on the two-track fix. Both tracks are implemented and verified.

### Track 1 - any-of is now the PRIMARY metric

The business question a RAG pipeline answers is "was the answer **found**",
which is any-of: one retrieved chunk containing the answer is enough. all-of
recall answers a different (stricter) question - "were **all** relevant chunks
found" - and is structurally capped at `k / |gold|`. So:

- `hit_at_k` is documented as THE primary metric and is now listed first in
  `EvalReport.metrics` (dict order drives `ab_markdown`, so it leads every A/B
  table), in the per-category/per-tag tables, and in `ab_rag.py`'s verdict.
- `recall_at_k` is explicitly the SECONDARY coverage metric.
- `--min-hit5` added to `pi-py rag eval` as the primary CI gate; `--min-recall5`
  kept for the coverage gate.

Why this is not "moving the goalposts": the historical A/B *conclusions* were
already computed on `rr` (`case_diff` uses reciprocal rank, which is any-of by
construction) - so "rerank is the decisive gain" was never an all-of artifact.
Only the headline recall number was affected.

### Track 2 - prune the file, guard the behavior (they are NOT the same thing)

Measured across the 9 cases with gold > 5, the real defect is narrower than
"gold inflation": the excerpt is a **template sentence** that recurs verbatim
across N chunks of one compendium, so the case cannot discriminate (almost any
top-5 contains one -> hit@k = 1 for every config) while its all-of ceiling is
`k/N`.

| case | gold | excerpt | what OLD rebind did |
|---|---|---|---|
| rag-017 / rag-027 | 54 | `*为食谱中用到的食药物质` | dropped (`ambiguous`, cross-doc) |
| rag-019 | 52 | `2.*为食谱中用到的食药物质` | dropped (`ambiguous`, cross-doc) |
| rag-023 | 54 | `宏量营养素占总能量比为：蛋白质 15%～20%` | **kept and SCORED** |
| rag-097 | 24 | `全天总用量：植物油15g，盐＜5g` | **kept and SCORED** |
| rag-021 | 18 | `全天总用量：植物油 25g，盐 4g` | **kept and SCORED** |
| rag-098 | 14 | `春季食谱 1（总能量约 1200kcal）` | **kept and SCORED** |
| rag-030 | 12 | `杂粮饭（大米70g，黑米30g）` | **kept and SCORED** |
| rag-002 | 10 | `41(2),2009, pp. 459-471` (journal footer) | **kept and SCORED** |

That split is the whole story: 3 were already excluded by rebind, **6 were being
scored with gold 10-54** - and those 6 are the ones that depressed recall@5.

**Two changes, only one of which moves the metrics:**

- **Track 2a - prune the file (metric-neutral).** `tools/prune_golden_set.py
  --golden evals/tasks/rag/corpus_v2.json --apply` (dry-run by default; writes a
  `pruned_cases` audit record into the meta). **151 -> 142 cases**, max gold
  54 -> 5. All 9 were already excluded from scoring by rebind, so this changes
  NO metric. Its value is that the file is honest standing alone: anyone
  computing a ceiling without running rebind now gets the right answer.
- **Track 2b - guard the rebind/generator (THE behavior change).** See below.
  This drops the 6 scored cases -> scored set 142 -> **136**, and *that* is
  what lifts recall@5.

Verification across both views (file on disk vs actually scored):

| view | n | perfect-recall@1 | @3 | @5 | @10 |
|---|---|---|---|---|---|
| pruned FILE (no rebind) | 142 | 0.8723 | 0.9937 | **1.0000** | 1.0000 |
| actually SCORED (post-guard) | 136 | 0.8667 | 0.9934 | **1.0000** | 1.0000 |
| scored, exclude `lexical_leak` | 77 | 0.8296 | 0.9889 | **1.0000** | 1.0000 |

Real-stack A/B re-run on the scored set (`evals/reports/ab_20261001_173721.md`):

| metric | before (`ab_20260929_112152`) | after (`ab_20261001_173721`) |
|---|---|---|
| hit@1 | 0.914 | 0.909 |
| hit@5 (PRIMARY family) | 0.963 | 0.961 |
| mrr | 0.931 | 0.928 |
| **recall@5 (secondary)** | **0.895** | **0.932** |

The fingerprint is exactly as predicted: the primary any-of metrics barely move
(the guard cannot change whether the *first* gold chunk is found), while the
secondary all-of recall rises because its structural cap is gone. Also note
what did NOT change: `hybrid_rrf60+rerank` is still the best config, and
`hit@5` 0.961 keeps it far ahead of `vector_only` 0.753.

### Track 2b detail - recurrence guard

`MAX_ANY_OF_GOLD = max(KS) = 5` is now enforced in BOTH code paths that build a
gold set from excerpts:

- `build_golden_set._expand_any_of_gold` - refuses to expand past the guard and
  **removes** the case (counted, returned, printed, and recorded in the meta as
  `template_repeat_dropped`).
- `harness.rebind_golden` - a case whose excerpt matches `> MAX_ANY_OF_GOLD`
  chunks is dropped as `template_repeat` and listed in the rebind report,
  instead of being silently given a gold set nothing can satisfy.

⚠️ One thing this leaves standing: `rebind` still drops 6 cases per run as
cross-doc `ambiguous` (`rag-033/093/099/111/115/143`), so the scored set is 136
even though the file holds 142. That is a chunking-dependent property, not a
case defect, and it is reported loudly - deliberately not pruned.

### Deliberately NOT pruned: the wrong-section keys

rag-094's `#86` (冬季食谱3) and rag-096's `#37`/`#38` (冬季食谱1) stay in gold.
Their gold sets still contain the TRUE-section chunks, so the primary any-of
metric is correct - surfacing any true gold chunk is a hit. They only dilute
the secondary all-of recall, which is precisely the argument for demoting that
metric. A hand edit would also not survive `rebind_golden()`: the excerpt
genuinely IS in those chunks, so a faithful rebind would re-add them.
