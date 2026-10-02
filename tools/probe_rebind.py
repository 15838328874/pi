"""Probe: which cases rebind_golden drops, and the resulting metric ceiling.

Checks two golden sets side by side (ORIG = pre-prune 151, PRUNED = current 142)
so the effect of a prune/guard change is visible, and prints for each:

- every rebind outcome (unchanged / rebound / ambiguous / unresolved /
  template_repeat) with the case ids that fell into each bucket, and
- the perfect-retrieval recall@k ceiling on the POST-REBIND **scored** set.

The second number is the one that matters. `ab_rag` scores the post-rebind set,
so a ceiling computed on the raw file is wrong - doing that produced a bogus
0.9545 once, when the true historical value was 0.9708 on the 142 scored cases.
Rule: compute any ceiling on the post-rebind set.
See evals/reports/badcase_v2_lost4_diagnosis.md

Requires: local MySQL with the corpus ingested (eval user 992002 for v2).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import pi  # noqa: F401,E402  (loads ./.env)

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from pi.rag.eval.harness import GoldenSet, rebind_golden  # noqa: E402
from pi.rag.types import Chunk  # noqa: E402


async def main() -> None:
    eng = create_async_engine("mysql+aiomysql://pi:pi_py_local@127.0.0.1:3306/pi_py_test")
    async with eng.connect() as c:
        rows = (
            await c.execute(
                text(
                    "SELECT id, doc_key, user_id, seq, text, title_path "
                    "FROM rag_chunks WHERE user_id=:u"
                ),
                {"u": 992002},
            )
        ).all()
    await eng.dispose()

    chunks = [
        Chunk(chunk_id=r[0], doc_key=r[1], user_id=r[2], seq=r[3],
              text=r[4], title_path=r[5])
        for r in rows
    ]
    print(f"chunks loaded: {len(chunks)}")

    for label, fname in (("ORIG(151, pre-prune)", "corpus_v2.orig.json"),
                         ("PRUNED(142, current)", "corpus_v2.json")):
        gs = GoldenSet.load(ROOT / "evals" / "tasks" / "rag" / fname)
        rb, rep = rebind_golden(gs, chunks)
        print(f"\n=== {label} ===")
        print(f"  in={rep.total} unchanged={rep.unchanged} rebound={rep.rebound} "
              f"ambiguous={rep.ambiguous} unresolved={rep.unresolved} "
              f"template_repeat={rep.template_repeat} -> kept={len(rb.cases)}")
        print(f"  ambiguous ids      : {rep.ambiguous_ids}")
        print(f"  template_repeat ids: {rep.template_repeat_ids}")
        print(f"  unresolved ids     : {rep.dropped_ids}")

        sc = rb.cases
        sizes = sorted((len(c.gold_chunk_keys) for c in sc), reverse=True)
        print(f"  scored n={len(sc)}  max gold={sizes[0] if sizes else 0}  "
              f"top sizes={sizes[:6]}")
        for k in (1, 3, 5, 10):
            r = sum(min(k, len(c.gold_chunk_keys)) / len(c.gold_chunk_keys)
                    for c in sc) / len(sc)
            print(f"    perfect-recall@{k:<2} = {r:.4f}")


if __name__ == "__main__":
    asyncio.run(main())
