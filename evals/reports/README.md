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
export PI_DATABASE_URL=... PI_MILVUS_URI=... PI_EMBEDDING_* ... PI_RAG_RERANK_* ...

# A/B 两个配置，输出报告到本目录
python tools/ab_rag.py --help          # 看当轮可用的参数
python tools/build_golden_set.py --help
python tools/rebuild_eval_index.py --check   # 向量投影与 SQL 真相源是否漂移
```

## 保留哪几份更值得

如果后续要挑少量报告入库留档，优先这几类（其余可丢）：

| 报告 | 价值 |
|---|---|
| `badcase_v2_lost4_diagnosis.md` | 坏例归因，解释了一个具体的检索失手（PDF 表格重复抽取） |
| `ab_20261001_*` | 最近一轮，结论与当前默认值对应 |
| `ab_20260929_022018.INVALID_embedder_midR8.md` | **标记为 INVALID**——保留是为了记住"哪次实验的结论不能用" |

`INVALID_` 前缀是作者的约定：该轮实验中途换过 embedder，结论不可比。
