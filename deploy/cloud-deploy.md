# pi-py 云服务器部署指南（火山引擎）

目标拓扑：pi-py 应用跑在一台 4 vCPU / 16 GiB 的云服务器（ECS，已装 Docker）上，
MySQL 与 Redis 使用火山引擎托管实例，通过 **VPC 私网** 访问（无公网流量、无公网延迟）。

```
                    公网
用户 ──────────────────────────────►  ECS (4C/16G, Docker)
   :443 (caddy, 可选)  :8300 (直连)     │  app 容器 (uvicorn :8300)
                                      │  migrate 一次性容器
                                      │  caddy 容器 (可选, TLS)
                    VPC 私网           ▼
        MySQL 8.0.43  <MYSQL_HOST>:3306  (库: pi_py)
        Redis 7.0.15  <REDIS_HOST>:6379
```

> 本文与 `docker-compose.cloud.yml` 里的实例域名一律写成 `<MYSQL_HOST>` / `<REDIS_HOST>`
> 占位符：真实值只活在 `.env`（git-ignored），仓库里不留任何指向具体云账号的标识。

## 1. 前置条件核对

| # | 项 | 状态 | 说明 |
|---|---|---|---|
| 1 | ECS 与 RDS/Redis 同 VPC（或已打通） | ☐ 待你确认 | 在 ECS 上 `ping "$MYSQL_HOST"` 能解析出私网 IP 即通 |
| 2 | RDS 安全组：3306 仅对 ECS 私网 IP 放行 | ☐ 待你确认 | 控制台 → MySQL 实例 → 安全组；公网 3306 建议关闭 |
| 3 | Redis 安全组：6379 仅对 ECS 私网 IP 放行 | ☐ 待你确认 | 同上 |
| 4 | MySQL 用户 `pi`（最小权限） | ✅ 已验证 | `pi`@`%`，仅 `pi_py`.* 的 DML+DDL；`pi_py` 库已建，schema 在 alembic 0002，数据为空 |
| 5 | ECS 安全组：对公网只开需要的端口 | ☐ 待你确认 | 用 caddy 就开 80/443；直连就开 8300 且**限定来源 IP** |
| 6 | Docker ≥ 20.10，compose v2 | ☐ 待你确认 | `docker compose version` 有输出即可 |

如果你忘了 `pi` 用户的密码（或想换一个），用 admin 账号重置：

```sql
ALTER USER 'pi'@'%' IDENTIFIED BY '新的强密码';
```

## 2. 部署步骤

### 2.1 上传代码到 ECS

在**本机**项目根目录执行（或用 git 拉取，任选其一）：

```bash
# 方式 A: 打包上传（排除无关文件）
cd D:/pycharm/project/pi/pi-python
tar --exclude='__pycache__' --exclude='.pytest_cache' --exclude='*.db' \
    -czf /tmp/pi-py.tar.gz .
scp /tmp/pi-py.tar.gz root@<ECS公网IP>:/opt/pi-py.tar.gz

# 方式 B: 服务器上直接 git clone（若仓库在远端）
# git clone <repo> /opt/pi-py && cd /opt/pi-py
```

在 **ECS** 上解压：

```bash
mkdir -p /opt/pi-py && tar -xzf /opt/pi-py.tar.gz -C /opt/pi-py && cd /opt/pi-py
```

### 2.2 配置环境变量（.env，不入库）

```bash
cp deploy/env.cloud.example .env
chmod 600 .env
vi .env    # 填 MYSQL_HOST / MYSQL_PASSWORD / REDIS_HOST / REDIS_PASSWORD / PI_JWT_SECRET / OPENAI_API_KEY 等
```

> ⚠️ **裸跑用的 `.env` 不能直接拿来跑 compose**（2026-09-06 实测）。裸跑路径读的是
> `PI_DATABASE_URL` / `PI_REDIS_URL` 两条**完整 URL**，密码内嵌在里面；而
> `docker-compose.cloud.yml` 要的是**分立的** `MYSQL_HOST` + `MYSQL_PASSWORD`
> （和 `REDIS_HOST` + `REDIS_PASSWORD`），由它自己拼 URL。只有一份裸跑 `.env` 时
> `docker compose config` 会直接失败：
> `required variable MYSQL_PASSWORD is missing a value`。
>
> 从既有 URL 里补出这四个键即可，host 必须与 URL 里的完全一致：
>
> ```bash
> python3 - <<'PY'
> import re, pathlib
> raw = pathlib.Path(".env").read_text()
> out = []
> for var, scheme, prefix in (("PI_DATABASE_URL", r"mysql\+aiomysql", "MYSQL"),
>                              ("PI_REDIS_URL",    r"redis",           "REDIS")):
>     m = re.search(rf'^{var}=\s*{scheme}://[^:]+:([^@]*)@([^:/]+):', raw, re.M)
>     out += [f"{prefix}_HOST={m.group(2)}", f"{prefix}_PASSWORD={m.group(1)}"]
> print("\n".join(out))   # 核对无误后再追加进 .env；注意密码可能已 percent-encode
> PY
> ```
>
> `DOMAIN` 只有走 `--profile tls`（Caddy）时才需要，普通 `up -d` 不涉及。

要点：

- `PI_JWT_SECRET` 用 `openssl rand -hex 32` 生成，**换了它所有已发 token 立即失效**，保持稳定；
- 密码里若有 `@ : / # ? %` 等 URL 特殊字符，需 percent-encode（如 `@` → `%40`）；
- `PI_MODEL` / `OPENAI_API_KEY` / `OPENAI_BASE_URL` 决定模型走哪，`PI_FALLBACK_CHAIN` 可选。

### 2.3 构建并启动

```bash
docker compose -f docker-compose.cloud.yml up -d --build
```

compose 会先跑 `migrate` 一次性容器（把 alembic 版本推进到 head，当前云端已是 0002，
所以是幂等空跑），成功后（`service_completed_successfully`）才启动 `app`。

### 2.4 验证清单

```bash
# 1) 容器状态：migrate 应为 exited(0)，app 应为 healthy（约 30s 后）
docker compose -f docker-compose.cloud.yml ps

# 2) 启动日志无报错（重点看 MySQL/Redis 私网连接）
docker compose -f docker-compose.cloud.yml logs app --tail=50

# 3) 健康检查
curl -s http://127.0.0.1:8300/healthz

# 4) 注册一个账号（开放注册，注册即普通用户）
curl -s -X POST http://127.0.0.1:8300/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"<强密码>"}'

# 4b) 需要管理员时：直连 MySQL 改库授予（代码里没有任何提权路由）
mysql -h <MYSQL_HOST> -u pi -p pi_py \
  -e "UPDATE users SET is_admin=1 WHERE username='alice'; SELECT id,username,is_admin FROM users;"

# 5) 登录 + 跑一轮对话（SSE）
curl -s -X POST http://127.0.0.1:8300/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"<强密码>"}'   # 拿 access_token
curl -N -X POST http://127.0.0.1:8300/v1/sessions/<sid>/runs \
  -H "Authorization: Bearer <token>" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"hello"}'
```

对外访问：没配域名时用 `http://<ECS公网IP>:8300`（明文，测试可用，生产建议 TLS）；
有域名则把 DNS A 记录指到 ECS，`.env` 里设 `DOMAIN`，然后：

```bash
docker compose -f docker-compose.cloud.yml --profile tls up -d
```

**无论走哪条路，8300 都必须限定来源 IP**（见上表第 5 项）。注册接口免鉴权且无任何门禁
——对全网开放 8300 等于任何人都能开号：每个号都带 `PI_DEFAULT_QUOTA_TOKENS` 的月配额和
一个 workspace 目录，而且每次注册都要付一次 20 万轮 PBKDF2（约 50ms CPU）。

PBKDF2 现在跑在线程池里而不是事件循环上（见 ARCHITECTURE §17 第 15 条），所以刷注册
**不再能让整个服务停止响应**，但仍然能吃满 4 个核、把所有人的请求拖慢——是从"打死"
降级成"拖慢"，不是变成"无害"。

注册和登录的每次尝试（成功与各种失败原因）现在都进审计日志，带客户端 IP 和 User-Agent，
这是事后封号和追责的唯一依据。**但只有 `PI_FORWARDED_ALLOW_IPS` 设对了，IP 才是真的**
（见下表与 §17 第 16 条）——走 Caddy 时不设，日志里所有请求都是同一个容器 IP。

## 3. 4 vCPU / 16 GiB 调优

| 参数 | 建议值 | 依据 |
|---|---|---|
| `PI_MAX_CONCURRENT_RUNS` | 16 | 回合是进程内异步、LLM 网络调用为主，不吃 CPU；16 是 `docker-compose.cloud.yml` 的兜底值（代码默认 8），要生效就别在 `.env` 里写死 8。"单实例 8 并发 p99 <5s" 是前任维护者的自托管压测数据，**本部署只实测过连接层，应用层未复测** |
| `PI_MAX_CONCURRENT_RUNS`（开 docker sandbox 时） | **8，不要照抄 16** | 容器现在有真的内存上限了，所以这里算的是乘法：`PI_SANDBOX_MEMORY × PI_MAX_CONCURRENT_RUNS` 必须明显小于物理内存。1g × 16 = 16 GiB = 整机内存，还没算温池里另外那批闲置容器（上限 `PI_SANDBOX_POOL_MAX`）和 app 自己。要么保持 8（1g × 8 = 8 GiB），要么把 `PI_SANDBOX_MEMORY` 降到 512m 再谈 16 |
| `PI_SANDBOX_MEMORY` | 1g（默认） | 单容器内存上限；同时设等值 `--memory-swap` 关掉 swap（docker 默认允许 2 倍）。**docker 自己的默认是完全不限制**（`Memory=0`），而注册是开放的，不设等于任何账号一条命令就能打爆这台机器。实测：容器内申请 2 GiB 被 OOM kill，exit 137 |
| `PI_SANDBOX_PIDS` | 256（默认） | 单容器进程数上限，挡 fork bomb；`0` = 不限。docker 默认也是不限（`PidsLimit` 未设、容器内 `ulimit -u` unlimited） |
| `PI_SANDBOX_CPUS` | 1.0（默认） | 单容器 CPU 上限（`NanoCpus` = 1e9/核）；空 = 不限。4 vCPU 的机器上 8 并发 × 1.0 允许超卖，靠 CFS 配额分时——这是想要的行为，别设成 0.125 去"精确平分"，那样单条命令会慢得离谱 |
| `PI_RATE_LIMIT_RUNS_PER_MIN` | 30/用户 | 按用户固定窗口；30 是 `docker-compose.cloud.yml` 的兜底值（代码默认 20，`.env` 里也写的 30）。用户多时够用，嫌 429 多就调高 |
| `PI_FORWARDED_ALLOW_IPS` | `172.16.0.0/12`（compose 已设） | **走 `--profile tls` 就是必填项**。uvicorn 默认只信 `127.0.0.1`，而 Caddy 是独立容器（源 IP `172.x`），头部会被静默丢弃 → 审计日志里的 IP 全是 Caddy 自己。`172.16/12` 覆盖 Docker 默认网段范围；在 compose 里固定 subnet 后可以收窄成那一格。**设错不报错**，只看启动日志那行 `trusted proxies for ...`。绝不能写 `*`（见 §17 第 16 条） |
| `PI_REPLICAS` | 1 | 单机多副本可用（锁/限流都在 Redis）；跨机扩容前 workspace 必须换共享存储（见下） |
| 内存 | app 进程本身无需限制；**沙箱必须限** | app + 并发回合本身远低于 16 GiB，但沙箱容器要按上面那行乘法算，caddy 另计。不开沙箱（local 模式）时命令跑在 app 容器里，反而没有任何内存上限——这也是应该开沙箱的理由之一 |

**已知限制（重要）**：workspace 和审计日志放在本机 named volume（`workspaces`、`audits`）。
单机部署没问题；将来要跨主机扩容副本，需要先把 workspace 迁到共享存储（NFS/云盘挂载），
否则不同副本看到的会话文件不一致。

## 4. Sandbox（工具执行隔离）说明

- **默认（local）**：bash 工具命令在 app 容器内以非特权用户 `pi`（uid 10001）执行。
  有容器这层隔离（碰不到宿主机），但所有用户共享同一个 app 容器环境——
  **包括应用的全部环境变量**。任何注册用户都能 `echo $PI_JWT_SECRET` 拿到签发 token 的密钥、
  从 `PI_DATABASE_URL` 里读出 RDS 密码。裸跑（不在容器里）时更糟：那是宿主机 root。
  所以 local 只适合完全信任的单机场景，公网部署必须开 docker 模式。
- **`PI_SANDBOX=docker`（预热池模式，默认）**：每个 workspace 一个常驻温容器——
  turn 开始时预热（与 LLM 首个响应并行），命令通过 `docker exec` 进入（单次调用
  只有 exec 开销，没有 create/start/销毁的完整生命周期）；容器闲置超过
  `PI_SANDBOX_IDLE_TTL`（默认 600s）自动回收，达到 `PI_SANDBOX_POOL_MAX`（默认 16）
  按 LRU 驱逐（全部忙碌时允许临时超额，不阻塞）；命令超时的容器视为脏、立即销毁
  重建；容器意外消失（daemon 重启/寿命上限）会透明重建一次。同一 workspace 的命令
  共享容器内进程态（pip 装的包、env 变量在调用间保留——文件本来就在挂载卷里持久）。
  温容器内置 2h 硬性自毁（`PI_SANDBOX_WARM_LIFETIME`），应用崩溃也不会在宿主机
  留下永久孤儿容器。
- **`PI_SANDBOX_POOL=0`**：退回旧模式（每次调用一个全新 `docker run --rm` 容器），
  进程态每次清零，但每次调用都付完整容器生命周期成本。**注意这条路径超时会漏容器**
  （只杀了本地 docker CLI，容器自己还在跑），见 ARCHITECTURE §17 第 21 条。
- **实测确认过的隔离效果**（真容器里跑的，不是推断）：容器内看不到应用的任何环境变量；
  网络完全不通（`Network is unreachable`，DNS 也失败），**包括火山引擎元数据服务
  `100.96.0.96`**；没有挂 docker socket；只挂了该会话自己的 workspace 到 `/ws`，
  宿主机 `/root` 不可见；跨用户目录访问被拒；`Privileged=false`。资源上限也已生效：
  容器内申请 2 GiB 被 OOM kill（exit 137），cgroup 读到
  `memory.max=1073741824` / `pids.max=256` / `cpu.max=100000 100000`，
  `docker inspect` 显示 `Memory`/`MemorySwap`/`PidsLimit`/`NanoCpus`/`NetworkMode=none` 都对。
  **沙箱曾经管不到 `web_fetch`/`web_search`**（它们跑在应用进程里，见 §17 第 20 条）——
  这两个工具**已删除**，`policy.json` 的 `deny_tools` 因此为空；联网走模型端点自己的
  builtin tools（`POST /runs` 的 `builtin_tools` 字段），由 provider 侧发起请求。
- **`PI_SANDBOX_USER`（默认不设，跟随应用自身 uid:gid）**：容器通过 bind mount 写的文件
  会**落在宿主机上**，归属就是容器内执行它的那个 uid。默认取应用自己的 uid:gid
  （裸跑 root → `0:0`，compose uid 10001 → `10001:10001`），所以应用永远读得到也删得掉
  这些文件；如果让容器以 root 跑，非 root 的应用（compose 里是 10001）就清不掉留下的
  root 文件。不显式给 group 会得到 `gid=0`，所以格式是 `uid:gid`。`docker exec` 会继承
  容器的 user，因此只需在创建时设一次。
- **启用前提**：app 容器需要访问宿主 Docker——要么挂 `/var/run/docker.sock` + 镜像内装
  docker CLI（等同给 app 容器宿主机 root 权限，需自行评估），要么给 Docker daemon 开
  TCP（配 TLS）并通过 `PI_DOCKER_HOST` 指定。**当前 compose 默认不开启**；要开先改
  Dockerfile 装 docker CLI 并在 compose 挂 socket。
  **切 compose 前先检查 `.env`**：两个 compose 写的是 `PI_SANDBOX: ${PI_SANDBOX:-}`，
  会从 `.env` 插值。仓库 `.env` 为裸跑设了 `PI_SANDBOX=docker`，直接拿它跑 compose
  会把这个值带进容器——而容器里没有 daemon，于是每次 bash 调用都抛
  `PI_SANDBOX=docker but neither the docker CLI nor PI_DOCKER_HOST is available`
  （fail-closed，不会退回本机执行，但报错很难联想到是 `.env` 继承来的）。
  部署用的 `.env` 里把它清空，或者给容器配好上面那条通路。

## 5. 日常运维

```bash
# 看日志 / 跟踪
docker compose -f docker-compose.cloud.yml logs -f app

# 升级（新代码上传后）
tar -xzf /opt/pi-py-new.tar.gz -C /opt/pi-py
docker compose -f docker-compose.cloud.yml up -d --build   # migrate 自动先行

# 回滚：git checkout <旧tag> 后同样 up -d --build；
# 数据库回滚不建议（alembic downgrade 需人工评估），尽量向前兼容

# 停止 / 清理
docker compose -f docker-compose.cloud.yml down            # 保留数据卷
docker compose -f docker-compose.cloud.yml down -v         # 危险：连数据卷一起删
```

备份策略：MySQL/Redis 是托管实例，在控制台开启自动备份（RDS 快照）即可；
ECS 侧需要备份的是 named volume 里的 workspace（用户会话文件）：
`docker run --rm -v pi-py_workspaces:/data -v /backup:/backup alpine tar czf /backup/workspaces-$(date +%F).tar.gz -C /data .`

### 5.1 定位一次 run 出了什么问题

每次 `POST /v1/sessions/{id}/runs` 都在 MySQL 留下完整轨迹，**默认就开着，不需要任何
配置**。用户报"答错了 / 很慢 / 不记得我说过的事"时：

```bash
# 1) 拿 request_id：响应头 X-Request-Id，访问日志那行 JSON 里是同一个 id。
# 2) 管理端页面：「管理控制台 → 执行轨迹」，按用户过滤 → 点 run_id 展开。
#    或直接用 API（需要管理员 token，见 §2.4 的 4b）：
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" \
  "http://127.0.0.1:8300/v1/admin/traces?anomaly=true&limit=50"
curl -s -H "Authorization: Bearer $ADMIN_TOKEN" \
  "http://127.0.0.1:8300/v1/admin/traces/<run_id>"
# 3) 拿 request_id 反查那一次请求的访问日志：
docker compose -f docker-compose.cloud.yml logs app | grep '<request_id>'
```

步骤（`agent_steps.kind`）各自回答什么：

| kind | 回答的问题 |
|---|---|
| `retrieval` | **"模型为什么不记得"**。每次 run 最多一行，`args` 是查询原文，`detail` 是整份账：`outcome`（injected / no_hits / gated_out / rerank_empty / embed_failed / recall_failed / join_failed / disabled）、`index`（**召回到底走没走成向量库**：index / index_empty / index_failed / breaker，后三者是 MySQL 全表暴力余弦降级）、每个候选的 `cosine` 与 `rerank` 分数及 `verdict`（kept / below_cosine_gate / below_rerank_gate / over_top_k / absent_in_repo）、当时的阈值、各阶段毫秒、以及**实际注入的文本**——注入的记忆从不进 messages，所以对话回放里看不到它，只有这一行有 |
| `llm_call` | **"慢还是循环"**。每轮模型调用一行：第几轮、哪个 provider、stop_reason、本轮 token、耗时；失败的那轮也在（`ok=false` + `error`）。run 级的总时长和总 token 对"一次很慢"和"五次很快"是一样的，这里才分得开 |
| `tool_call` | 工具的**完整入参与完整结果**（不是 SSE 那 200 字预览） |
| `plan` / `compaction` / `error` | 计划提交 / 历史压缩 / run 级错误 |

`agent_runs.flags` 是异常判定，`anomaly=true` 过滤的就是它：`timeout` / `error` /
`empty`（用户付了钱模型什么都没答）/ `tool_storm`（≥3 次工具失败）/ `memory_failed`
（召回某个阶段抛了异常）。**`memory_failed` 只标记"坏了"，不标记"没召回到"**——对
还没有事实的用户，空召回就是正确答案。

### 5.2 span 与指标（Jaeger / Prometheus，默认不起）

```bash
# 起采集器 + 把 span 从 jsonl 文件改成 OTLP 推送：
PI_TRACER=otel docker compose -f docker-compose.cloud.yml --profile observability up -d

# 两个 UI 都不鉴权，而一条 trace 里带着 prompt、工具入参和工具输出，所以端口只绑
# 127.0.0.1。走隧道看，别在安全组里开 16686/9090：
ssh -L 16686:127.0.0.1:16686 -L 9090:127.0.0.1:9090 <vm>
#   Jaeger     http://127.0.0.1:16686  （service = pi-py，一次 run 一棵树）
#   Prometheus http://127.0.0.1:9090  （target: pi-py，配置在 deploy/prometheus.yml）

# 指标不经过 Prometheus 也能直接看：
curl -s http://127.0.0.1:8300/metrics | grep -E '^pi_(runs|memory|llm)' 
```

`PI_TRACER=jsonl`（默认）时 span 写容器内 `~/.pi-py/traces-<日期>.jsonl`，一次 run
一个 `trace_id`、带 `parent_span_id`，可以还原成树；`audits` 卷已经把它持久化了。
Jaeger 的 all-in-one 把 span 存在**内存**里，是排障视图不是归档——持久记录始终是
MySQL 里的 `agent_runs`/`agent_steps`（`PI_TRACE_RETENTION_DAYS`，默认 30 天）。

## 6. 故障排查

| 症状 | 排查 |
|---|---|
| `migrate` 容器反复失败 | `docker compose -f docker-compose.cloud.yml logs migrate`；多为安全组没放行 3306（私网不通）或密码错 |
| app 起不来，日志报 Redis 连接超时 | 同理查 6379 安全组；确认用的是 `*.ivolces.com` 私网域名而不是 `*.volces.com` 公网域名 |
| 连接随机断（运行一段时间后） | 已内置 `pool_pre_ping` + `pool_recycle=280`，正常不会；若仍出现，查 RDS 的连接数限制与超时参数 |
| 8300 公网访问不通 | ECS 安全组没开 8300，或开了但来源 IP 不对 |
| 注册返回 409 username already exists | 重名，换一个即可；注册永久开放，不存在"已关闭"状态 |
| `/v1/admin/*` 全部 403 | 注册不会给管理员权限，必须改库：`UPDATE users SET is_admin=1 WHERE username='...'`。想清库重来：按 FK 顺序清 `usage_records → messages → sessions → users` |
| SSE 响应被缓冲/截断 | 走了不支持流式的代理；直连或用本仓库的 Caddyfile.cloud（`flush_interval -1`） |
| 前端没有「管理控制台」按钮 | 就是上一条：按钮 `v-if="auth.isAdmin"`，而当前账号 `is_admin=0`。改库后**重新登录**（`isAdmin` 是登录时从 `/v1/auth/login` 与 `/v1/me` 读的） |
| `PI_TRACER=otel` 但 Jaeger 里没数据 | 看 app 日志有没有 `falling back to jsonl`（exporter 没装 → 镜像是旧的，重新 `up -d --build`）；确认 `PI_OTLP_ENDPOINT=http://jaeger:4317` 且 jaeger 真起来了（`--profile observability`，`docker compose ... ps`）；BatchSpanProcessor 是批量发的，进程刚被 kill 可能还没 flush（正常退出时 lifespan 会 `tracer.shutdown()` 刷一次）。**采集器不可达时的实测行为**：启动和 run 都正常（只在导出时打一两行 `StatusCode.UNAVAILABLE`，不会每个 span 刷一次），但优雅退出会多花约 7s 走完导出重试——重启变慢先怀疑这里，而不是怀疑数据库 |
| `/metrics` 返回 503 | `PI_METRICS=0`，或镜像里没有 `prometheus-client`（旧镜像）。响应体里就写着原因 |
| `/metrics` 返回 404 | 设了 `PI_METRICS_TOKEN` 而请求没带对的 `Authorization: Bearer`。**刻意答 404 而不是 403**——403 等于告诉扫描器这里有个端点 |
| Prometheus 里 target 一直 down | `deploy/prometheus.yml` 的 target 是 `app:8300`，只在 compose 网络内可达，宿主机上 curl 不到是正常的。另外 `PI_REPLICAS>1` 时该服务名会轮询多个副本，每次抓到不同进程 → 计数器看着像被重置、`rate()` 出垃圾，得按副本拆成多个 target |
| 轨迹里没有 `retrieval` 步骤 | 记忆没配置（`PI_MILVUS_URI` / `PI_EMBEDDING_MODEL` 任一为空）就不召回，也就不写这一步——这是刻意的，否则每个 run 都多一行 "disabled"。**配了却没起来**的仍然会写，`outcome` 会是 `embed_failed` 之类 |
