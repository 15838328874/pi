# 文件产物交付设计（模型产出 → 用户可下载）

## 0. 一句话

模型写出来的文件，用户在「文件」面板能看到、能下载；产物跨会话持久、不被误删、
存储可控。**当前已落地：实时下载 + 100MB 配额；持久化（MinIO）与闲置清理列为后续。**

## 1. 问题背景（为什么需要这条设计）

- 模型用 `write` / `bash` 产生的文件，**原本没有任何用户可见的出口**——用户看到
  "Wrote N chars to <path>" 却拿不到文件。
- 前端「文件」面板走的是 `/v1/files`（对象存储文件管线），而 `PI_S3_*` 未配置时
  那条管线是关闭的，于是面板是空的。
- 存储有限：workspace 无限增长会打爆磁盘；bash 会产生大量过程垃圾（`.venv`、
  `node_modules`、`__pycache__`、日志、临时文件），不能和产物混存。

## 2. 关键事实：文件工具与沙箱的分工

（这是整套设计的地基，务必先读 ARCHITECTURE §7.3 / `tools/sandbox.py`）

| 工具 | 执行位置 | 文件落点 |
|---|---|---|
| `write` / `read` / `edit` / `grep` / `find` / `ls` | **宿主机 workspace**（`LocalFS`，直接） | `~/.pi-py/workspaces/<session>/` |
| `bash` | **沙箱 VM** 的 `/workspace` | VM 内，turn 结束 `save_workspace` 同步回宿主机 |

**推论**：turn 结束时，无论哪条路径产生的文件，**都齐在宿主机 workspace**。所以
"实时下载"直接读 workspace 即可（零同步、零依赖），"持久化"也从 workspace diff。

## 3. 架构定论：两层，各管各的

```
workspace（宿主机） = 草稿纸   小、临时、随会话、给模型用、会被清
MinIO（对象存储）   = 档案柜   大、持久、跨会话、给用户、可扩展
```

- **不能把 workspace 当档案柜**：它随会话生灭、会被归档清理，几百文件堆在 session
  目录里既找不到、又拿不到。
- **不能只靠 workspace**：1 万用户 × 100MB = 1TB，本地盘扛不住；对象存储可挂独立盘/
  集群/迁云（改 `PI_S3_ENDPOINT` 一行），这是它相对 workspace 的不可替代之处。

## 4. 完整方案（四层，闭环）

```
① 实时：workspace 文件实时可下载（读宿主机，零上传）
② 持久：turn 结束 diff 本轮新增 → 过滤 → 自动传 MinIO
③ 清理：会话闲置 7 天 → 清整个 workspace（文件已在 MinIO，不丢）
④ 配额：workspace 100MB/会话（草稿纸）+ MinIO 每用户 X GB + 90 天过期
```

### 4.1 实时下载（已实现 ✅）

- `GET /v1/sessions/{sid}/files`：列出该会话 workspace 里的文件（递归，跳过
  `SKIP_DIRS` + 符号链接，按 mtime 倒序）。
- `GET /v1/sessions/{sid}/files/{path}`：下载单个文件，`FileResponse` 流式。
- **安全**：ACL 用 `_owned_session`（跨用户 404 不泄漏存在性）；路径沙箱
  `resolve + relative_to(base)` 防 `../` 与 symlink 逃逸——与 `policy.path_sandbox`
  同一条纪律。
- **零同步**：直接读宿主机 workspace，不经过 MinIO（实时层要的是"快 + 无依赖"）。

### 4.2 持久化（后续）

- 时机：**turn 结束**（不是"会话结束"——会话可能开一个月，且没有明确结束事件；
  turn 结束落袋，服务随时崩都不丢）。插入点：`runner.py` 的 `finally`，
  `save_workspace` 之后、`archive_workspace` 旁边，**复用 archive 已经在算的
  baseline diff**（零额外扫描）。
- 上传：`ObjectStore.put_bytes`（服务器端上传，产物在宿主机，不能走预签名直传）；
  登记 `files` 表（加 `session_id` + `source` 列）。
- 过滤（挡 bash 垃圾，`archive.py` 的 `SKIP_DIRS` 打底）：`.git/.venv/node_modules/
  __pycache__/dist/build` + 扩展名黑名单（`.log/.tmp/.pyc`）+ 隐藏文件 + 单文件大小上限。
- 下载：复用现有 `/v1/files/{id}/url` 预签名 URL，与用户上传同一条路。

### 4.3 闲置清理（后续）

- 时机：**不是传完就删**（模型下一轮还要 read 那些文件），而是**会话闲置 7 天**
  清整个 workspace。清的时候文件早就在 MinIO，零丢失。
- 频率：后台任务每小时一次（`asyncio.create_task` + `while True: sleep(3600)`，
  同沙箱池 `_sweep_loop` 模式）。7 天容差下早清晚清几小时无影响，扫描是毫秒级。
- **安全前提**：清理前确认产物都已传 MinIO；发现未传的文件则补传或跳过，宁多留不丢。

### 4.4 配额（已实现 workspace 100MB ✅，MinIO 配额后续）

- **workspace（草稿纸）**：`write` 写前检查累计大小，超 `PI_WORKSPACE_MAX_BYTES`
  （默认 100MB）直接拒绝（返回 error，模型可自愈）；`bash` 无法写前精确拦，
  turn 结束后检查，超了记 warning 不删文件（删用户文件比超限更糟）。
- **MinIO（档案柜）**：每用户配额 + 90 天生命周期（`export_file` 标记的重要文件跳过）。

## 5. 找回链路（workspace 清空后怎么回来）

```
会话闲置，workspace 被清
  → 用户回来开新会话："把上个月的攻略拿回来"
  → 模型 list_files（看 MinIO 文件列表，拿 id）
  → 模型 fetch_file(id)（从 MinIO 下载到 workspace）
  → 模型 read / 继续操作
```

**关键**：`fetch_file` 是模型**主动调用**的工具（参数是 `list_files` 返回的 id），
不是"本地找不到自动兜底去 MinIO"。所以"能否找回"取决于**当时有没有传 MinIO**。

## 6. 产物识别：diff 全量 + 过滤，export_file 加分项

- 默认：turn 结束 diff 全量传（不依赖模型自觉，不漏产物），靠三层过滤挡垃圾。
- 加分：`export_file` 工具让模型主动声明"重要产物"→ 标记跳过过期清理。模型忘调
  只是不"永久保留"，不丢文件。

## 7. 现状与后续（状态快照）

| 层 | 状态 |
|---|---|
| ① 实时下载 | ✅ 已实现 |
| ④ workspace 100MB 配额 | ✅ 已实现 |
| 文件管线上传/下载（MinIO 预签名直传） | ✅ 已通电（`PI_S3_*` 已配，`/v1/files` 全流程验证通过） |
| ② 产物自动进 MinIO | ⏳ 后续（`put_bytes` + turn 结束 diff 上传） |
| ③ 闲置清理 | ⏳ 后续（每小时任务 + 清理前安全检查） |
| MinIO 生命周期 / export_file | ⏳ 后续 |
| 传输模式：服务器中转（见 §8） | ⏳ 后续 |

## 8. 传输模式：预签名直传 vs 服务器中转（待优化）

**当前是预签名直传**：服务器签发带签名的 URL，浏览器**直连 MinIO** 上传/下载。
这是主流"direct handshake"（服务器不搬字节，省带宽）。

**它的固有要求**：客户端必须能直连对象存储。而预签名 URL 的 host 来自
`PI_S3_ENDPOINT`——当前是 `http://127.0.0.1:19000`，这个 `127.0.0.1` 在**客户端
浏览器里指向客户端自己**。于是：

- 服务器本机访问（脚本/curl）→ 正常（127.0.0.1 就是服务器自己）
- 用户浏览器（经 SSH 隧道访问前端）→ 失败（127.0.0.1 是用户电脑，连不上 MinIO）

**临时解**：用户本地额外开一条 19000 隧道（`ssh -L 19000:127.0.0.1:19000`）。

**方案 A（服务器中转，后续做）**：让 MinIO 完全藏在服务器后面，浏览器只访问 API：

```
上传：浏览器 → POST /v1/files（multipart）→ 服务器存 MinIO
下载：浏览器 → GET /v1/files/{id}/content → 服务器读 MinIO → 返回
```

- 客户端只需一个隧道（8300），MinIO 可收回 `127.0.0.1`（不再 `0.0.0.0` 裸奔）
- 对象存储不暴露给客户端——企业场景的正确形态
- 代价：大文件占服务器带宽/内存（流式响应缓解）
- 这条是"产物进 MinIO、下载走 MinIO"最终收敛的形态：浏览器永远只跟 API 打交道

**为什么现在只记不做**：当前规模小（单用户）、隧道能顶；等多人使用或 MinIO 需要
收回内网时再上。
