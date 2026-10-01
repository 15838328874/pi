# 媒体输出设计存档——图片/视频的生成与知识库检索（未实施）

> 状态：**仅规划，后续优化方向**（2026-10-01 讨论记录）。当前项目全链路纯文本
> （models.py 的 Block 只有 text/tool_call/tool_result 三类）。本文档记录媒体输出
> 的设计思路与两个具体场景（视频生成、知识库视频检索）的实现方案，未实施。

## 1. 两种"输出媒体"的含义

| 含义 | 谁产出 | 典型场景 | 结论 |
|---|---|---|---|
| **工具产出媒体** | 工具调外部 API 或截屏/ffmpeg，结果是图/视频文件 | 文生图、文生视频、computer-use 截图 | agent 产品 95% 是这种，本文档的主体 |
| **模型原生输出**（回复里直接带图） | LLM 自己吐图片 | GPT-4o 那种 | 需要 ImageBlock 进协议 + vision 模型，成本高，仅 computer-use 等场景才需要 |

## 2. 核心设计：媒体 = 文件，协议传引用不传字节

**URL/文件引用通道**，而不是把 base64 塞进消息协议：

```
工具产出媒体 → 存 MinIO → 工具结果带 file_id/URL 文本
→ SSE 传引用（几十字节）→ 前端 fetch→blob 渲染
→ 模型上下文里只是 URL 文本（纯文本模型完全兼容）
→ 落库存 file_id（不存 presigned URL——会过期）
```

复用现有件：MinIO 预签名管线（storage.py）、files 表（迁移 0007）、
`/v1/files/{id}/url`（app.py，属主校验 + 现签）、`ToolContext.ctx.store/ctx.files`
（工具层已能触达存储）、`ToolResult.payload` 字段（base.py:19，已预留未消费）。

## 3. 端到端链路（图片为例，各环节改动量）

以"画一只橘猫"走一遍：

1. **模型调工具**：`generate_image` 照常走 policy.check——新工具声明能力
   `image.generate`，P0-1 能力授权可直接闸门
2. **工具执行**：httpx 调生图 API → `ctx.store.put_bytes(...)`（**唯一新加的存储
   方法**，现有管线是客户端直传，服务端生成需要直接写）→ files 表插行
   （content_type=image/png）→ 返回
   `ToolResult(content="已生成橘猫：file_id=42", payload={"images":[{"file_id":42,"filename":"orange-cat.png"}]})`
   ——content 给模型看（纯文本），payload 给前端看（结构化元数据）
3. **结果回流三支流全部零改动**：进模型上下文（ToolResultBlock 文本）、进轨迹
   （jsonl+runs 表存文本）、进审计（preview 截断照旧）
4. **SSE**：`toolcall_end` 事件新增 `"images"` 字段（透传 payload）——协议唯一改动
5. **前端渲染**：`<img>` 标签带不了 Authorization 头 → JS fetch（带 Bearer 调
   `/v1/files/{id}/url` 拿 presigned URL）→ fetch 直连 MinIO → blob →
   `URL.createObjectURL` → `<img src=blobURL>`。约 20 行 JS
6. **落库**：messages.blocks 存 file_id 文本；用户重开会话时 renderMessages 解析
   file_id → 现签 URL → 重渲染。**历史永不过期**（存引用不存 URL）
7. **多轮后续**：改图 = `generate_image(image_id=42, ...)` 下载原图字节再生成；
   ffmpeg 处理 = 已有 `fetch_file`（≤256MB）stage 进沙箱

**改动清单（图片通道）**：storage.put_bytes（几行）+ toolcall_end 加 images 字段
（透传 payload）+ 前端 fetch→blob 渲染（~20 行 JS）。其余全吃现有管线。

## 4. 边界细节

- **跨用户**：`/v1/files/{id}/url` 属主校验 404 不泄漏存在性；模型拿别人的
  file_id 也取不到字节
- **压缩后**：摘要覆盖旧消息后模型可能"忘记"图——用户侧不受影响（前端渲染走
  DB 原文非摘要）；要模型长期记得，靠 remember 工具记 file_id
- **计量**：生图 API 费用不进 LLM token 计量——`ToolResult.usage`（base.py:20）
  已预留，可挂 API 调用计数
- **流量**：媒体字节走 MinIO 直连，不经过 pi-py 应用进程（与上传管线同原则）

## 5. 场景一：视频生成（异步 job 模式）

**核心矛盾**：视频生成 2-5 分钟（Runway/可灵/Sora），工具调用是同步的、run 超时
600s——同步等 = 卡死整轮 + 撞超时。解法是**提交即返回**：

```
① generate_video("海边日落") → 提交任务拿 job_id → 立即返回
   ToolResult("已提交视频生成 job=abc123，预计 3-5 分钟")   ← 不等
② 模型回复"开始生成了"，run 正常结束
③ 服务端后台任务轮询 job（复用 sweeper 的 asyncio 周期任务模式）
④ 完成 → put_bytes 存 MinIO → files 表插行（video/mp4）
⑤ 前端"生成中"卡片翻转成 <video>：
   - 最省事：前端轮询 GET /v1/files 发现新文件（零协议改动）
   - 更实时：SSE 通知端点（长连接，工程量+1）
⑥ 下一条用户消息时，模型从历史/list_files 知道"好了，file_id=43"
```

失败路径：job 失败 → 后台任务记失败标记，下一轮模型可见。模型全程只接触
job_id/file_id，视频字节永不进上下文。

## 6. 场景二：知识库视频检索（向量化文本代理）

**核心原则：视频不能直接向量检索，向量化它的"文本影子"。**

### 摄取（入库时一次性）

```
视频入库 → 提取文本代理：
  ├─ 标题/描述（元数据）
  ├─ 字幕/ASR 转写（ffmpeg 抽音轨 + whisper，或直接用字幕轨）
  └─ 按时间切段（如 30s 一段，每段带起止时间戳 + 转写文本）
→ 每段文本走现有 embedding 管线 → Milvus 新集合 media_segments
→ files 表加 media 元数据列（duration/segments/transcript）
```

### 检索（用户提问时）

```
用户问"怎么配置 Clash 分流规则" → 语义检索命中《WSL 网络配置》第 2:15-3:40 段
→ 注入模型上下文：转写文本 + 定位（file_id=44, start=135）
→ 模型基于转写回答
```

### 关键决策：让用户"看到"视频

只把转写塞提示词 = 模型知道了、用户没看到，知识库视频价值废一半：

| 方案 | 做法 | 评价 |
|---|---|---|
| A. 纯提示词注入 | 转写进 system prompt，模型口头转述 | 用户没视频可看 |
| B. **show_media 工具** | 检索命中后模型调 `show_media(file_id, start=135)`——零副作用工具，只把 payload 挂上 SSE | ✅ 推荐：完全复用 §3 的 payload 渲染通道，前端弹视频卡从 2:15 播放 |

**场景二的展示侧与场景一的完成侧是同一段代码**——两个场景只在"视频从哪来"
不同：生成 job 灌进文件库 vs 摄取管线灌进文件库。

## 7. 改动清单对比与实施顺序

| 层 | 图片通道（§3） | 场景一（生成） | 场景二（检索） |
|---|---|---|---|
| 工具 | generate_image | generate_video + 可选 check_video | show_media（纯展示） |
| 后台 | — | job 轮询任务 | **摄取管线**（ffmpeg/whisper + 分段）——主要工程量 |
| 存储 | put_bytes + files 行 | 同左 | 同左 + files 表 media 列 |
| 向量 | — | — | Milvus 新集合 media_segments |
| 协议 | toolcall_end 加 media payload（三者共用，一次做完） | 同左 | 同左 |
| 前端 | <img> 渲染 | 生成中卡片 → <video> | 视频卡（带 start 跳转） |

**建议顺序**：先做共用的 media payload 通道（一次投入三处受益）→ 场景一（轻，
一两天）→ 场景二（重在摄取管线）。视觉模型（图片进模型上下文，computer-use
前置）独立于本方案，需要时再动。

## 8. 与现有设计的衔接

- P0-1 能力授权：`image.generate` / `video.generate` / `media.show` 进能力词表，
  策略可单独闸门（design 见 ARCHITECTURE §9.1）
- `ToolResult.payload`（base.py:19）与 `ToolContext.ctx.store/ctx.files` 已预留，
  工具层改动最小
- 交互式 run（docs/run-durability-design.md §11）与场景一的"生成中卡片"可叠加：
  后台完成后的通知通道可复用未来的 SSE 通知端点
