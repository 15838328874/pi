# 语义记忆：设计决策与实测记录

本文记录 `MemoryRepo`（`src/pi/server/db.py`）去重/冲突判断的**演进过程、实测数据与取舍**。
目的不是罗列代码，而是把"为什么这么做、试过什么、数据说了什么"钉下来，换模型或后人
接手时不靠猜。

## 1. 现状一览（最终形态）

```
add(user_id, text)  —— per-user 锁内串行
  1. 输入守卫：strip 非空、且 _terms(text) 非空（纯 emoji/标点 = 垃圾，拒绝）
  2. embed 一次（向量路径开时），复用给「召回 + Milvus upsert」，绝不重复计费
  3. 混合召回：向量 top-8 ∪ BM25/IDF top-8，去重后 ≤12 条（§3.2）
  4. LLM judge（flash）看候选列表自己挑 target，判 duplicate / conflict / new
  5. 写新 / conflict 版本化退役（写新行 + 旧行 superseded_by，历史保留）
  6. 每用户上限驱逐（_MEMORY_LIMIT=500，最旧优先）
```

## 2. 判重/冲突（对齐 mem0：召回 → 全量 judge）

```
混合召回（向量 ∪ BM25）→ 候选列表全给 judge
   ├─ duplicate → 不写
   ├─ conflict  → 版本化退役（写新行 + 旧行 superseded_by → 新行）
   └─ new       → 写新
      │ judge 挂了 / 没配
      ▼
词法 Jaccard 0.85（最后的兜底，只抓逐字重复）
```

**没有廉价信号分流**。实测（§3.1）证明：cosine/reranker/词法都分不清「同义改写 /
换值冲突 / 同模板不同主体」，三者在 0.86~0.95 重叠，任何阈值要么丢记忆要么漏重复。
所以判重唯一可靠的是 LLM 语义判断，廉价信号只用于**召回**（把候选捞全），不用于**判重**。

**核心原则（贯穿全链）**：`宁可重复，不丢记忆` —— 丢一条用户明确记住的记忆**不可恢复**；
多一条重复有 500 上限 + 检索 top-k 兜底，代价极低。要做到"既不重复又不丢"需要 DB
唯一约束 + 层 2 fencing 贯穿下游，**实现太重、对记忆场景不成比例**，所以整个去重都取
fail-open：每一档失败都往「更可能写」的方向降级。

## 3. 关键发现（实测，不是推断）

### 3.1 廉价信号分不清三类 —— 单一阈值彻底死路

用真实 embedding（`qwen3.7-text-embedding`）对四类样本打分，结论：**任何廉价信号
（cosine/reranker/词法）都分不清「同义改写 / 换值冲突 / 同模板不同主体」，它们在
0.86~0.95 完全重叠**：

| 样本对 | cosine |
|---|---|
| 同义改写「我住在杭州」vs「我现在住在杭州」 | 0.99 |
| 同模板不同主体「项目prj0是阿里云」vs「项目prj3是阿里云」 | 0.89 |
| 换值冲突「项目prj3是阿里云」vs「项目prj3改成腾讯云」 | 0.88 |

**这推翻了"cosine 高分判 duplicate 安全"的早期结论**：0.88 阈值想抓弱同义，却把
「同模板不同主体」（0.89）和「换值冲突」（0.88）一并误杀——add 池子时"项目prj3是
阿里云"被"项目prj0是阿里云"误判 duplicate、根本没写入，后续 judge 召回不到 target
只能判 new（这正是 conflict 一度 80% 的根因）。

**结论：廉价信号只用于召回（把候选捞全），判重唯一可靠的是 LLM 语义判断。**
（早期"reranker 能取代 LLM 判重"的设想同样被推翻——reranker 也分不清，已删除。）

### 3.2 真正的瓶颈是「召回」，不是 judge —— 混合召回

规模实测（300 条）最初冲突 19/20 判 new，逐层 debug 后定位到根因：**不是 judge 判错，
而是召回根本没把正确的 target 捞给 judge**。

embedding 按"语义/值词"排序，`用户0的主语言改成Go` 里的 "Go" 会把所有 `用户X的主语言
是Go` 排到前面，把真正的 target `用户0的主语言是Rust` 挤出 top-5：

```
向量召回 top-5（全被"Go"占满）：用户1/5/9/13/17 的主语言是Go
正确 target「用户0的主语言是Rust」排名 #5 —— 被挤出，judge 看不到 → 只能判 new
```

解法是**加一条词法召回通道**（BM25/IDF，用户建议的方向）：对字符 bigram 做 IDF 加权。
bigram 是"实体"的载体——"用户0"和"用户1"共享所有有用 unigram（用/户/主/语/言），只在
数字上不同，但 bigram `户0` / `户1` 把实体区分开了（`户0` 罕见 → IDF 高）。这条通道
能把语义通道丢掉的 target 捞回来。

**两条通道 union 去重**（`_RECALL_K=8` 每通道，合并 ≤12）后，target 稳定进列表，judge
判对 conflict 从 **4/20 → 17/20**。

> 教训：**问题往往不在"判断器"，而在"喂给判断器的输入"**。先验召回覆盖率，再怪模型。

### 3.3 judge：flash + mem0 式多候选 + few-shot

- **模型用 flash**（`PI_MEMORY_JUDGE_MODEL`，默认 `openai/deepseek-v4-flash`）：judge
  每次非平凡 add 都跑，判三条短文本不需要 frontier 模型，flash 便宜且快。
- **喂候选列表（编号），不是预选 top-1**（借鉴 mem0 `DEFAULT_UPDATE_MEMORY_PROMPT`）：
  judge 输出 `{"verdict": "duplicate"|"conflict"|"new", "target": 编号或 null}`，自己
  从多条里挑 target，所以单个 top-1 选错不影响结果。
- **few-shot 示例**钉死 conflict 定义："同一主体、同一属性换值即 conflict，无论措辞是
  '是X'还是'改成X'"。

### 3.4 为何不区分 UPDATE 和 DELETE（保持三分类）

mem0 是「多值集合」模型（"喜欢X"可有多个），需要 UPDATE（合并）与 DELETE（矛盾删）两个
不同落库动作；我们是「单值原子事实」模型（一个主体一个属性一个值），"换值"即覆盖，没有
"合并"。DELETE 处理的"否定"措辞（"不是X了"）出现概率低、后果轻、引入成本高，**暂不引入**。
真正的"删除/遗忘"是显式用户操作，属"记忆删除/隐私（GDPR）"待办。

## 4. 并发正确性：per-user 锁 + fencing token

「查重 → 写入」是 check-then-act，两个并发 add 会都通过查重、都落库。解法是 per-user
分布式锁（`cache.acquire_lock_owned`），整段串行化。

**fencing token 分两层，只做了便宜的层 1：**
- **层 1（释放层，已做）**：acquire 生成唯一 token 存进锁值，release 用 Lua 原子
  `if GET==token then DEL`，防止锁过期被抢后误删别人的锁。
- **层 2（贯穿下游，未做）**：把单调递增 fencing 号传到 MySQL 写路径。对「记忆重复」
  过度，根问题已由"TTL 60s ≫ add 最坏耗时"概率性兜住。

> ⚠️ judge 调 LLM 在锁内执行，锁持有时间可能逼近 `_MEM_LOCK_TTL=60s`；超时被抢后果
> 也只是「偶发重复」（fail-open 语义内），非正确性事故。

## 5. fail-open 的一致性（一处曾写反，已修正）

所有失败点同一方向——**都往"更可能写"降级**：

| 失败点 | 降级 | 理由 |
|---|---|---|
| 锁拿不到/超时 | 无锁写入 | 偶发重复 > 丢记忆 |
| cosine 召回失败 | 词法召回 | 层层兜底 |
| judge 挂 | 判 `new`（写新） | 丢写 > 偶发重复 |

## 6. 修过的坑（回归测试各自钉死）

1. **无 token 垃圾记忆**：纯 emoji/标点被写入却永远检索不到，现 `_terms(text)` 为空直接拒绝。
2. **孤儿向量误判重**：驱逐删 MySQL 行、留 Milvus 向量，判重命中孤儿会让被驱逐记忆写不回来。
3. **重复计费**：embed 和 search 曾同 try 块，search 挂把已算好的 vec 置 None 导致二次计费。
4. **召回漏 target**：embedding 被值词干扰，`改成Go` 把 `用户X的主语言是Go` 全捞上来、
   漏掉真正的 `用户0的主语言是Rust`。混合召回（+BM25 bigram）修复。

## 7. 已知未修 / 后续（详见 ROADMAP §3）

- **embed 熔断**：Milvus 长期 down 时每次 add 仍 embed 计费、向量落不了地。需健康熔断。
- **驱逐失败语义**：`_enforce_limit` 失败会抛（记忆已 commit 但 remember 报错）。
- **换模型必须重标定**：换 embedding 模型重跑 `tools/probe_memory_threshold.py`；
  BM25/IDF 的召回深度（`_RECALL_K`）也依赖当前词法分布。
- **judge 模型独立化**：已默认 flash、可用 `PI_MEMORY_JUDGE_MODEL` 覆盖。
- **词法层语言局限**：中文单字 token 只防逐字重复；日文假名/韩文会被当垃圾拒绝。
- **孤儿向量清理**：驱逐留向量占索引空间，随 `memory rebuild` 命令一并清理。
