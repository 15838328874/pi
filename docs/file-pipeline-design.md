# 文件链路设计：上传 → MinIO → 沙箱处理 → 下载（P0 方案）

> 决策已定：① 沙箱镜像装 A+B 档（见 `deploy/sandbox/Dockerfile`）；② 文件真身在
> MinIO，服务器按业界主流"预签名 URL 直连"只签发不搬字节；③ workspace 改
> **会话级**隔离。

## 1. 目标与原则

1. **文件真身在 MinIO**，服务器只"签发 URL + 记账（MySQL 元数据）"，字节永远在
   客户端 ↔ MinIO ↔ 沙箱之间流动 —— 不占宿主带宽/磁盘（宿主已 83% 满、剩 6.5G）。
2. **凭据最小化**：沙箱/客户端只拿限时（≤15min）+ 限定对象的预签名 URL，绝不接触
   MinIO 长期 key。
3. **会话级隔离**：上传文件归属 user，但进入沙箱按 session 装载，会话间不串。
4. **1GB VM 磁盘硬边界**：小文件直拉落盘处理；大文件走 range 流式（边下边算）或
   挂载，不在 1GB 盘上撒全量。

## 2. MinIO 部署（独立实例，不借用平台 minio）

```bash
docker run -d --name pi-minio --restart unless-stopped \
  -p 127.0.0.1:19000:9000 -p 127.0.0.1:19001:9001 \
  -e MINIO_ROOT_USER=pi_minio \
  -e MINIO_ROOT_PASSWORD=<随机强口令> \
  -v pi-minio-data:/data \
  minio/minio server /data --console-address ":9001"
```

- S3 API 走 `127.0.0.1:19000`（沙箱通过宿主网络的 MinIO 源 IP 访问到此口，见 §5）。
- 两个 bucket：`pi-files`（用户上传真身）、`pi-artifacts`（沙箱产物）。
- 服务端环境变量（追加进 `pi-real.env`）：
  `PI_S3_ENDPOINT=http://127.0.0.1:19000`、`PI_S3_ACCESS_KEY`、`PI_S3_SECRET_KEY`、
  `PI_S3_BUCKET_FILES=pi-files`、`PI_S3_BUCKET_ARTIFACTS=pi-artifacts`。

## 3. 数据模型（MySQL `files` 表）

```sql
CREATE TABLE files (
  id           BIGINT AUTO_INCREMENT PRIMARY KEY,
  user_id      BIGINT       NOT NULL,          -- 归属用户（会话级装载仍按 session）
  object_key   VARCHAR(512) NOT NULL,          -- MinIO 对象键（唯一）
  bucket       VARCHAR(64)  NOT NULL DEFAULT 'pi-files',
  filename     VARCHAR(255) NOT NULL,          -- 用户看到的原始文件名
  size         BIGINT       NOT NULL,          -- 字节数
  content_type VARCHAR(128),                   -- MIME
  sha256       CHAR(64),                       -- 完整性 + 去重键
  created_at   DATETIME     NOT NULL,
  UNIQUE KEY uk_object (object_key),
  UNIQUE KEY uk_user_sha (user_id, sha256),  -- 去重：同用户同内容复用对象
  KEY idx_user (user_id, created_at)
);
```

对应 SQLAlchemy `FileRow` + 一条 alembic 迁移（沿用现有 `users`/`sessions` 关联）。

**sha256 去重语义**：同用户上传相同内容的文件（含同名重传），算 sha256 命中
`(user_id, sha256)` 就返回既有记录——不重新写 MinIO、不重复落库（省空间、天然
幂等）。文件名以首次上传为准；同内容不同文件名视为边缘情况，暂不拆两条记录。

## 4. API 契约

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/files` | multipart 上传（小文件直传；大文件分片见 §7） |
| GET | `/v1/files` | 当前用户文件清单（agent 可枚举） |
| GET | `/v1/files/{id}/url` | 签发预签名 GET URL（进沙箱 / 下载复用） |
| DELETE | `/v1/files/{id}` | 删对象 + 删元数据 |

## 5. 预签名直连流程（业界主流，AWS/E2B 同款）

```
① 上传（真身进 MinIO，不落服务器盘）
  客户端 ──POST /v1/files──▶ 服务器
  服务器：预签名 PUT URL（限制 object_key + ≤15min）
  客户端 ──PUT 直传──▶ MinIO（字节不过服务器）
  服务器：记 files 表元数据

② 进沙箱（VM 直拉，不占宿主磁盘）
  agent 调 file 工具 → 服务器签发预签名 GET URL
  沙箱 VM：curl -sS -o /workspace/x.csv "<presigned GET>"
  （沙箱有 curl/wget/python3，直拉现成）

③ 处理
  模型用 bash/python 在 VM 内读写 /workspace 文件

④ 结果回写（预签名 PUT 直传，或 save_workspace 回传宿主再 PUT）
  沙箱：curl -X PUT --upload-file out.csv "<presigned PUT>"

⑤ 下载
  客户端 ──GET /v1/files/{id}/url──▶ 预签名 GET URL → 直拉
```

关键点：**服务器只"签发 + 记账"**，`_load_workspace`/`save_workspace` 保持给
workspace 自身（agent 手写产物），上传文件走预签名直拉，两套互不干扰。

## 6. MinIO 对象布局

```
pi-files/  {user_id}/{yyyy-mm}/{uuid}-{filename}       # 上传真身
pi-artifacts/ {session_id}/{yyyy-mm-dd}/{filename}     # 沙箱产物
```

- object_key 内不含用户可见路径（防路径穿越），文件名单独存 `filename` 列。
- 结果产物对象键带 session_id，天然会话粒度的可审计。

## 7. 大文件边界策略（1GB VM 磁盘是硬约束；P0/P1 只支持 ≤900MB）

| 文件大小 | 策略 |
|---|---|
| ≤ ~500MB | 预签名 GET → `curl` 直拉落盘 /workspace，常规处理 |
| 500MB–900MB | 直拉 + 明确提示"VM 磁盘 1GB，处理完及时删中间产物" |
| > 900MB | **P0/P1 直接拒绝**（HTTP 413），不落盘进沙箱 —— 流式/分片后续单独做 |

## 8. 鉴权与安全

- 预签名 URL：HMAC 签名、限定 `bucket+object`、TTL ≤15min（对齐回合时长）；
- 沙箱网络默认断网，直拉文件时按需开（`PI_SANDBOX_NET=host` egress 白名单允许
  MinIO 源 IP，不放全量出网）；
- MinIO root 凭据只存于服务端 env，不进入任何沙箱/客户端；
- files 表按 `user_id` 隔离，越权访问 404。

## 9. 落地顺序

| 阶段 | 交付 | 依赖 |
|---|---|---|
| **P0 · 数据入口** | 独立 MinIO 容器 + `files` 表/alembic + 上传/列表/预签名 API + boto3 依赖 | 本设计 §2§3§4§5① |
| **P1 · 进沙箱** | 会话级 workspace 改造 + `file` 工具接预签名直拉 + 产物回写/下载 | P0 + §5②④ |
| **P2 · 能力** | A+B 镜像构建成新模板（PDF/xlsx/docx 能力就位） | `deploy/sandbox/Dockerfile` |

## 10. 已定决策与未做项

**已拍板**：
- **sha256 去重**：同用户同内容（含重传）复用既有对象与记录，不重写 MinIO、
  不重复落库（§3 语义），文件名以首次上传为准。
- **大小边界**：P0/P1 只支持 ≤900MB，>900MB 上传即拒（413）。

**未做项（后续单独做，先记档）**：
- >900MB 大文件：预签名 + `Range: bytes=` 流式边下边算（不落全量盘）+ 结果流式写回；
- 分片上传（multipart）加速大文件/弱网上传；
- 结果产物 bucket（`pi-artifacts`）的对象生命周期/定期清理策略；
- 同内容不同文件名的拆分（当前按 sha256 合并为一条记录）。