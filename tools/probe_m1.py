"""M1 corpus probe: how do the real 待测试文档 files actually parse/chunk?

Throwaway diagnostic (not a pytest test): prints backend, title, block-kind
histogram, heading paths and the first chunk body for each corpus file, so
parser weaknesses are visible as DATA instead of guessed at.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pi.rag.chunker import Chunker  # noqa: E402
from pi.rag.config import ChunkingConfig  # noqa: E402
from pi.rag.parser import parse_file  # noqa: E402

CORPUS = Path(r"C:\Users\朱文宝\Desktop\pi版本\pi-rag\待测试文档")

TARGETS = [
    "RAG评估.md",
    "html-tags-decode.html",
    "数组.docx",
    "训练数据.csv",
    "销售数据统计.xlsx",
    "甬兴证券-AI行业点评报告：海外科技巨头持续发力AI，龙头公司中报业绩亮眼.pdf",
]


def probe(name: str) -> None:
    print("=" * 72)
    print("FILE:", name)
    try:
        r = parse_file(CORPUS / name)
    except Exception as exc:  # noqa: BLE001
        print("  PARSE FAILED:", type(exc).__name__, str(exc)[:300])
        return
    print(
        f"  backend={r.backend} title={r.title!r} chars={r.char_count} "
        f"pages={r.page_count} heavy={r.needs_heavy_parser} q={r.quality:.2f} lang={r.language}"
    )
    if r.reason:
        print("  reason:", r.reason)
    kinds: dict[str, int] = {}
    for b in r.blocks:
        kinds[b.kind] = kinds.get(b.kind, 0) + 1
    print("  block kinds:", kinds)
    print("  --- first 4 blocks ---")
    for b in r.blocks[:4]:
        print(f"    [{b.kind} lvl={b.level} p={b.page}] {b.text[:160]!r}")
    drafts = Chunker(ChunkingConfig(max_chars=400)).chunk(r)
    print(f"  chunks={len(drafts)}")
    print("  title_paths:", [d.title_path for d in drafts][:5])
    print("  sizes:", [len(d.text) for d in drafts][:12])
    if drafts:
        print("  --- chunk[0].text[:300] ---")
        print("   ", repr(drafts[0].text[:300]))
        print("  --- chunk[0].embed_text[:120] ---")
        print("   ", repr(drafts[0].embed_text[:120]))


if __name__ == "__main__":
    for t in TARGETS:
        probe(t)
