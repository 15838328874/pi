#!/usr/bin/env python
"""Prune template-repeat gold inflation out of a corpus golden set.

Diagnosis: ``evals/reports/badcase_v2_lost4_diagnosis.md``.

A case whose ``answer_excerpt`` is a TEMPLATE sentence - one that recurs
verbatim across N chunks of a single compendium - cannot discriminate between
retrieval configs. Real examples in corpus v2:

    `*为食谱中用到的食药物质`          -> 54 chunks (every recipe's footnote)
    `宏量营养素占总能量比为：蛋白质 15%～20%` -> 54 chunks
    `全天总用量：植物油 15g，盐＜5g`    -> 24 chunks
    `杂粮饭（大米70g，黑米30g）`        -> 12 chunks

Almost any top-5 contains one of the N, so hit@k is 1 for EVERY config (the
case measures nothing) while the all-of recall ceiling is k/N (rag-017 with 54
keys can never exceed 5/54 = 0.093). Keeping them inflates the primary metric
and deflates the secondary one, both for reasons unrelated to retrieval.

Pruning is a LABEL fix: no retrieval code changes, so no new A/B is needed
beyond confirming the metric ceilings move as predicted.

Deliberately NOT pruned: WRONG-SECTION expansions (rag-094's #86 is 冬季食谱3
not 秋季食谱1; rag-096's #37/#38 are 冬季食谱1 not 春季食谱3). Those gold sets
still contain the TRUE-section chunks, so the primary any-of metric is correct
- surfacing any true gold chunk is a hit. They only dilute the secondary
all-of recall, which is exactly why that metric was demoted. A hand edit would
not survive ``rebind_golden()`` anyway: the excerpt genuinely IS in those
chunks, so a rebind would faithfully re-add them.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--golden", default="evals/tasks/rag/corpus_v2.json",
                    help="golden set JSON to prune (relative to repo root)")
    ap.add_argument("--max-gold", type=int, default=5,
                    help="drop a case whose gold set is LARGER than this. Defaults "
                         "to the eval's max k: a top-k can hold at most k keys, so "
                         "any gold above max-k is template-repeat by construction.")
    ap.add_argument("--apply", action="store_true",
                    help="write the pruned golden set + meta back (else dry run)")
    args = ap.parse_args()

    gp = ROOT / args.golden
    if not gp.is_file():
        raise SystemExit(f"golden set not found: {gp}")
    data = _load(gp)
    cases = data.get("cases", [])

    kept, dropped = [], []
    for c in cases:
        n = len(c.get("gold_chunk_keys") or [])
        (dropped if n > args.max_gold else kept).append(c)

    print(f"[prune] {len(cases)} cases -> keep {len(kept)}, "
          f"drop {len(dropped)} (gold > {args.max_gold})")
    for c in sorted(dropped, key=lambda c: -len(c["gold_chunk_keys"])):
        n = len(c["gold_chunk_keys"])
        ex = (c.get("answer_excerpts") or [""])[0]
        print(f"  DROP {c['id']}: {n:3d} gold keys | Q={c['query'][:44]}")
        print(f"       excerpt={ex[:48]!r}")

    if not dropped:
        print("[prune] nothing to do")
        return

    by_cat = Counter(c.get("category", "happy_path") for c in kept)
    by_doc = Counter(c["gold_chunk_keys"][0].split("#")[0] for c in kept)
    multi = sum(1 for c in kept if len(c["gold_chunk_keys"]) > 1)
    max_kept = max((len(c["gold_chunk_keys"]) for c in kept), default=0)
    print(f"[prune] after: categories={dict(by_cat)}")
    print(f"[prune]        multi_gold={multi} max_gold_in_set={max_kept}")

    if not args.apply:
        print("[prune] DRY RUN - pass --apply to write")
        return

    data["cases"] = kept
    gp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[prune] wrote {gp.relative_to(ROOT)}")

    mp = gp.with_name(gp.name.replace(".json", ".meta.json"))
    if mp.is_file():
        meta = _load(mp)
        meta["cases"] = len(kept)
        meta["cases_by_category"] = {k: int(v) for k, v in by_cat.items()}
        meta["cases_by_doc"] = {k: int(v) for k, v in by_doc.items()}
        meta["multi_gold_cases"] = multi
        meta["pruned_on"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        meta["pruned_reason"] = (
            "template-repeat gold: the answer_excerpt is one sentence recurring "
            "verbatim across N chunks of a compendium, so the case cannot "
            "discriminate (hit@k=1 for every config) and caps all-of recall@k at "
            "k/N. See evals/reports/badcase_v2_lost4_diagnosis.md"
        )
        meta["pruned_cases"] = [
            {"id": c["id"], "gold_keys": len(c["gold_chunk_keys"]),
             "excerpt": (c.get("answer_excerpts") or [""])[0][:60]}
            for c in sorted(dropped, key=lambda c: -len(c["gold_chunk_keys"]))
        ]
        mp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[prune] wrote {mp.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
