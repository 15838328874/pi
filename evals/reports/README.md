# 评测报告（本地产物，不进版本控制）

本目录下的 `*.md` 是 RAG 评测各轮次的 A/B 报告，**已 gitignore**，只保留本文件。

## 为什么移出仓库

- 单份 128~490 行，13 份合计约 250KB；每跑一轮评测就新增一份
- 进仓库后 diff 被报告淹没，评审真实代码改动时需要翻过大量评测输出
- 它们是**可再生的**：评测脚本 + golden set 都在仓库里（`evals/tasks/rag/`、
  `tools/ab_rag.py`），报告只是某一次运行的产物

> 注意：代码与文档里有多处 `See evals/reports/xxx.md` 的引用（例如
> `src/pi/rag/eval/harness.py`、`src/pi/rag/retriever.py`、
> `src/pi/rag/config.py`、`ARCHITECTURE.md` §21）。这些指的是**本机这些文件**，
> 用来追溯某个默认值/阈值的实测依据；不在版本控制内，需要时按下面方式重跑。

## 怎么重新生成

```bash
# golden set 在仓库里（evals/tasks/rag/corpus_v*.json）
set -a; source .env.local; set +a      # PI_EMBEDDING_* / PI_RAG_RERANK_* / PI_MILVUS_URI

# ⚠️ 两个必设的评测专用变量（ab_rag.py 的默认值在本机是错的）：
# 1) 测试库端口：ab_rag.py 默认连 3306/pi_py_test，但本机 3306 被 CubeSandbox 占用、
#    pi-py 的 MySQL 在 13306 → 不设就会把语料灌进**错误的库**（不报错，静默）。
export PI_ITEST_DATABASE_URL="mysql+aiomysql://pi:pi_py_local@127.0.0.1:13306/pi_py_test"
export PI_ITEST_MILVUS_URI="http://127.0.0.1:19531"

# 2) 语料目录：_corpus_dir() 从 tools/ 向上找 `待测试文档`；测试文档在仓库外
#    （~/zhu/rag-test-docs/待测试文档），需在仓库根建软链接（已 gitignore）：
ln -sfn ../rag-test-docs/待测试文档 待测试文档

# A/B 两个配置，输出报告到本目录
python tools/ab_rag.py --help          # 看当轮可用的参数
python tools/ab_rag.py --corpus v2 --only vector_only,hybrid_rrf60,hybrid_rrf60+rerank --keep
python tools/build_golden_set.py --help
python tools/rebuild_eval_index.py --check   # 向量投影与 SQL 真相源是否漂移
```

> 语料不全会静默降级：`ab_rag.py` 对缺失文档打 `SKIP missing`，`rebind_golden` 把对应
> case 列为 `unresolved` 后**只评剩下的**。跑完务必核对 `[rebind]` 段的 unresolved 数量
> 与 `[corpus] ... chunks in MySQL` —— 否则会拿一个子集的结果当全量结论。
>
> `ab_rag.py` 会自动装配 heavy parser（`PI_RAG_HEAVY_PARSER`），双栏/扫描 PDF 会走
> PaddleOCR 而不是被标 `needs_heavy_parser` 跳过（2026-10-02 修）。

## 保留哪几份更值得

如果后续要挑少量报告入库留档，优先这几类（其余可丢）：

| 报告 | 价值 |
|---|---|
| `badcase_v2_lost4_diagnosis.md` | 坏例归因，解释了一个具体的检索失手（PDF 表格重复抽取） |
| `ab_20261002_144034.md` | 解析层改进（双栏 → PaddleOCR 版面解析）的 A/B：rerank 后 recall@1 0.429→0.824；见 `ARCHITECTURE.md` §21.6 |
| `ab_20261001_*` | 上一轮，结论与当前默认值对应 |
| `ab_20260929_022018.INVALID_embedder_midR8.md` | **标记为 INVALID**——保留是为了记住"哪次实验的结论不能用"（中途换过 embedder） |
| `ab_20261002_142845.INVALID_heavy_parser_not_wired.md` | **标记为 INVALID**——评测工具当时没装配 heavy parser，双栏 PDF 全被跳过，评测静默退化成 10 case（只有 1 份单栏文档） |

`INVALID_` 前缀是作者的约定：该轮实验的条件不成立，结论不可比、不可用。保留它们是为了
记住"哪次实验的结论不能用、为什么"。
