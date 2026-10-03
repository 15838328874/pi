# 语义记忆：设计决策与实测记录

本文记录 `MemoryRepo`（`src/pi/server/db.py`）从「词法去重」演进到「reranker 精判 +
LLM 冲突更新」的**思考过程、实测数据与取舍**。目的不是罗列代码，而是把"为什么这么
做、试过什么、数据说了什么"钉下来，换模型或后人接手时不靠猜。

## 1. 现状一览

```
add(user_id, text)  —— per-user 锁内串行
  1. 输入守卫：strip 后非空、且 _terms(text) 非空（纯 emoji/标点 = 垃圾，拒绝）
  2. embed 一次（向量路径开时），复用给「召回 + 判重 + Milvus upsert」，绝不重复计费
  3. 召回 top-k 候选（向量 search k=3；无向量则词法）
  4. 判重/冲突（见 §2 降级链）
  5. 写新 / 冲突原地覆盖（保 id + created_at，Milvus 同键 upsert）
  6. 每用户上限驱逐（_MEMORY_LIMIT=500，最旧优先）
```

## 2. 判重/冲突的降级链（精判优先，层层兜底）

```
reranker 精判（PI_RAG_RERANK_URL 配了才启）
  ├─ score ≥ 0.65 → LLM judge 三分类
  │    ├─ duplicate → 不写
  │    ├─ conflict  → 原地覆盖旧行
  │    └─ new       → 写新
  └─ score < 0.65   → 写新（新事实）
      │
      ▼ reranker 挂了 / 没配
cosine 0.92 判重（无 reranker 时的 fallback）
      │
      ▼ cosine 也挂了
词法 Jaccard 0.85（最后的兜底，只防逐字重复）
```

**核心原则（贯穿全链）**：`宁可重复，不丢记忆` —— 丢一条用户明确记住的记忆**不可恢复**；
多一条重复有 500 上限 + 检索 top-k 兜底，代价极低。要做到"既不重复又不丢"需要 DB
唯一约束 + 层 2 fencing 贯穿下游，**实现太重、对记忆场景不成比例**，所以整个去重都取
fail-open：每一档失败都往「更可能写」的方向降级，而不是「更可能不写」。

## 3. 阈值是怎么定的（不是拍脑袋）

### 3.1 cosine 0.92：实测证明"单一阈值无法两全"

用真实 embedding（`qwen3.7-text-embedding`）跑 10 正/10 负（`tools/probe_memory_threshold.py`）：

| 样本 | cosine |
|---|---|
| 正：「回答请用中文」vs「用户偏好中文回答」 | **0.61** |
| 正：「数据库采用MySQL主从架构」vs「数据库用MySQL主从」 | 0.92 |
| 负：「用户A生日1月」vs「用户B生日1月」 | **0.91** |
| 负：「项目A用MySQL」vs「项目B用MySQL」 | 0.87 |

关键观察：**正样本最低 0.61 < 负样本最高 0.91** —— embedding 对"语义等价"和"语义相关"
的 cosine 排序会颠倒。短文本信息量少，cosine 区分度天然不足。所以 0.92 只能是
「零误杀、漏判 3/10」的保守点，调低到 0.9 就会误杀"生日 1 月"这类不同实体。

**结论**：cosine 阈值对短记忆去重是死路，只配当"无 reranker 时的兜底"。

### 3.2 reranker：实测区分度好一个量级

同一批样本用 `qwen3.7-text-rerank`（cross-encoder）打分：

| 样本 | rerank |
|---|---|
| 正：「回答请用中文」vs「用户偏好中文回答」 | **0.75** |
| 正：「数据库采用MySQL主从架构」vs「数据库用MySQL主从」 | 0.77 |
| 负：「用户A生日1月」vs「用户B生日1月」 | **0.48** |
| 负：「项目A用MySQL」vs「项目B用MySQL」 | 0.45 |

正样本 0.74+、负样本 0.59-，**间隙约 0.15**，阈值 0.65 是安全分界。cross-encoder 把
两条句子一起喂给模型做相关性判断，天然比"各自 embed 再算 cosine"更能区分"等价"和
"相关但不同"。

> 一个被数据推翻的直觉：最初担心"reranker 判相关性而非等价性，会对同类不同实例
> （生日 1 月）误杀更重"。实测恰好相反——reranker 对这类给 0.48（干净判为不同）。
> **教训：判别力要用数据验证，不要用模型语义去推。**

### 3.3 LLM judge 三分类

reranker 高分（≥0.65）只说明"相关"，分不清是"重复"还是"冲突"（如「偏好中文」→「偏好英文」）。
这一步交给 LLM（`PI_MODEL` 的 default_model）三选一：`duplicate / conflict / new`。
`conflict` 时原地覆盖旧行（不是删了再插，保 id 与 created_at，Milvus 同键 upsert）。

真实 DeepSeek 冒烟三例全对：中文→英文 = conflict、Orion 改写 = duplicate、项目A/B MySQL = new。

> **规模实测观察（integration/test_memory_real_scale.py，300 条）**：去重 20/20 全对；
> 但冲突 20 条里 19 条被判 `new`、只有 1 条 `conflict`。样本是「用户0的主语言是Rust」vs
> 「用户0的主语言改成Go」——judge 对带"改成/更新为"这类**动作动词**的冲突倾向判成
> "新事实"而非"覆盖"，对直接矛盾的「是Rust / 是Go」才稳。**结论：judge 的 conflict
> 判定对措辞敏感**，后续优化方向是 prompt 里明确"同一主体、同一属性换值即 conflict，
> 无论措辞是'改成'还是'是'"。

## 4. 并发正确性：per-user 锁 + fencing token

「查重 → 写入」是 check-then-act，两个并发 add 会都通过查重、都落库。解法是 per-user
分布式锁（`cache.acquire_lock_owned`），整段串行化。

**fencing token 分两层，只做了便宜的层 1：**

- **层 1（释放层，已做）**：acquire 时生成唯一 token 存进锁值，release 用 Lua 原子
  `if GET==token then DEL`。防止"锁 TTL 过期、被别人拿走、原持有者 finally 误删别人的锁"。
- **层 2（贯穿下游，未做）**：把单调递增的 fencing 号传到 MySQL 写路径、存储只接受
  最大号。这才能根治"锁过期后双持有者同时写"，但对「记忆重复」这种低风险场景过度，
  且根问题已由"TTL 60s ≫ add 最坏耗时"概率性兜住。真需要时（多实例高并发）再上。

> ⚠️ 锁 TTL 与 judge 延迟：judge 调 LLM 在锁内执行（`deepseek-v4-pro` 短 prompt 通常
> 5~15s，慢/重试时更久），加上 embed + rerank，锁持有时间可能逼近 `_MEM_LOCK_TTL=60s`。
> 一旦超 TTL，锁过期被抢，后果也只是「偶发重复」（fail-open 语义内），非正确性事故；
> 但若日后把 judge 换成更慢的模型，应同步调大 TTL。

锁失败 **fail-open**（拿不到锁就无锁写入）：偶发重复 > 丢记忆。

## 5. fail-open 的一致性（一处曾写反，已修正）

三个失败点必须同一方向——**都往"更可能写"降级**：

| 失败点 | 降级 | 理由 |
|---|---|---|
| 锁拿不到/超时 | 无锁写入 | 偶发重复 > 丢记忆 |
| reranker 挂 | cosine → 词法 | 层层兜底 |
| judge 挂 | 判 `new`（写新） | 丢写 > 偶发重复 |

> judge 失败最初写成"保守判 duplicate"，与"宁漏不误"自相矛盾（judge 挂 → 丢一条
> 新记忆）。自查时发现并改回 fail-open。**这是本次最重要的自查教训：同一条原则在
> 每个失败点都要验一遍，别只记得锁那一处。**

## 6. 修过的坑（回归测试各自钉死）

1. **无 token 垃圾记忆**：纯 emoji/标点/符号文本被写入却永远检索不到。现在 `_terms(text)`
   为空直接拒绝（等同空白）。
2. **孤儿向量误判重**：驱逐只删 MySQL 行、Milvus 向量残留，语义判重命中孤儿向量会
   让被驱逐的记忆永久写不回来。现在判重命中后先 `_rows_by_ids` 验证 id 仍在 MySQL。
3. **重复计费**：早期 embed 和 search 在同一 try 块，search 挂了把已算好的 vec 置 None，
   导致 `_vector_add` 再 embed 一次（同文本计两次费）。现拆成两个 try 块，vec 保留复用。

## 7. 已知未修 / 后续（详见 ROADMAP §3）

- **embed 熔断**：Milvus 长期 down 时每次 add 仍 embed 计费、向量却落不了地。需健康熔断。
- **驱逐失败语义**：`_enforce_limit` 失败会抛，记忆已 commit 但 remember 报错（部分成功）。
- **换模型必须重标定**：换 embedding 模型重跑 `tools/probe_memory_threshold.py`；换
  rerank 模型同理（reranker 阈值 0.65 依赖当前 `qwen3.7-text-rerank` 的分数分布）。
- **judge 模型独立化**：当前 judge 用 default_model（偏贵），可 pin 一个便宜模型。
- **词法层语言局限**：中文单字 token 只防逐字重复；日文假名/韩文会被当垃圾拒绝。
- **孤儿向量清理**：驱逐留向量占索引空间，随 `memory rebuild` 命令一并清理。
