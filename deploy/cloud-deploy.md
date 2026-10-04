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
        MySQL 8.0.43  mysqldbe27c9b686f.rds.ivolces.com:3306  (库: pi_py)
        Redis 7.0.15  redis-cngzanmjgl9sd8ai4.redis.ivolces.com:6379
```

## 1. 前置条件核对

| # | 项 | 状态 | 说明 |
|---|---|---|---|
| 1 | ECS 与 RDS/Redis 同 VPC（或已打通） | ☐ 待你确认 | 在 ECS 上 `ping mysqldbe27c9b686f.rds.ivolces.com` 能解析出私网 IP 即通 |
| 2 | RDS 安全组：3306 仅对 ECS 私网 IP 放行 | ☐ 待你确认 | 控制台 → MySQL 实例 → 安全组；公网 3306 建议关闭 |
| 3 | Redis 安全组：6379 仅对 ECS 私网 IP 放行 | ☐ 待你确认 | 同上 |
| 4 | MySQL 用户 `pi`（最小权限） | ✅ 已验证 | `pi`@`%`，仅 `pi_py`.* 的 DML+DDL；`pi_py` 库已建（schema 随 migrate 容器推进，勿以本节历史编号为准），数据为空 |
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
vi .env    # 填 MYSQL_PASSWORD / REDIS_PASSWORD / PI_JWT_SECRET / OPENAI_API_KEY 等
```

要点：

- `PI_JWT_SECRET` 用 `openssl rand -hex 32` 生成，**换了它所有已发 token 立即失效**，保持稳定；
- 密码里若有 `@ : / # ? %` 等 URL 特殊字符，需 percent-encode（如 `@` → `%40`）；
- `PI_MODEL` / `OPENAI_API_KEY` / `OPENAI_BASE_URL` 决定模型走哪：默认走内置聚合网关
  （`OPENAI_BASE_URL` 留空即可，见 §2.5），`PI_FALLBACK_CHAIN` 可选。

### 2.3 构建并启动

```bash
docker compose -f docker-compose.cloud.yml up -d --build
```

compose 会先跑 `migrate` 一次性容器（把 alembic 版本推进到 head；head 随发版推进，
例如 0008_rag 之后新部署会真实执行未应用的迁移，不是空跑——**这个容器就是迁移的
唯一执行点，别跳过**），成功后（`service_completed_successfully`）才启动 `app`。

**为什么必须走 migrate 而不是依赖应用自建表**：`db.init()` 里的
`Base.metadata.create_all` 只建缺失的表、**不写 alembic_version**。绕过 migrate
裸跑 `pi-py serve` 会让新表被静默创建、版本号停留在旧值，下次 migrate 撞
"table already exists" 直接挂。裸金属部署同理：`pi-py migrate && pi-py serve`
的顺序不可省。

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
mysql -h mysqldbe27c9b686f.rds.ivolces.com -u pi -p pi_py \
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

### 2.5 LLM 聚合网关（new-api）

**为什么需要**：单一厂商账号的 QPS/并发上限远低于多人规模的需求（1000 人峰值约
150-200 在飞请求 + 20 QPS，见 ARCHITECTURE §5 规划账）。`llm-gateway`（new-api 容器）
是模型流量的统一前门：pi-py 只看到**一个** OpenAI 兼容端点，网关按渠道把流量分给
阿里云/DeepSeek/SiliconFlow 等多家，某渠道 QPS 打满或故障时自动切下一家。

**首次配置**（一次性，全部在网关 Web UI 完成）：

```bash
# 0) 管理 UI 只绑了 127.0.0.1:3000，经 SSH 隧道访问（绝不把 3000 暴露公网）：
ssh -L 3000:127.0.0.1:3000 root@<ECS公网IP>
# 浏览器打开 http://127.0.0.1:3000，默认管理员 root / 123456 登录，
# 立刻到「个人设置」改密码。顺手在「设置 → 运营设置」把日志保存天数设为 30-90 天。
```

**① 添加渠道（每家厂商一个）**：「渠道 → 新建渠道」，类型选 **OpenAI**，分组留
**default**，字段如下：

| 字段 | 阿里云百炼（示例） | DeepSeek（示例） | 说明 |
|---|---|---|---|
| 名称 | 阿里云-主力 | DeepSeek-备用 | 自定，好认即可 |
| 类型 | OpenAI | OpenAI | 两家都是 OpenAI 兼容协议 |
| 模型 | `qwen-max,qwen-plus,text-embedding-v3` | `deepseek-chat,deepseek-reasoner` | **渠道实际支持的模型**（厂商真实模型名），逗号分隔；不建议填 `*` |
| 模型重定向 | `qwen3.8-max:qwen-max` | `deepseek-chat:deepseek-chat` | `pi-py 请求名:渠道真实名`，逗号分隔多条。pi-py 发 `qwen3.8-max`，网关改发给厂商 `qwen-max` |
| 密钥 | 百炼 API-KEY（sk-...） | DeepSeek API key | 每家各自的 key |
| base_url | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `https://api.deepseek.com/v1` | 渠道的「代理」/base_url 字段（不同版本 UI 位置略异） |
| 优先级 | 10 | 5 | **数字越大越先走**——主力厂商设高，QPS 打满/故障时自动落下一家 |

前置条件：阿里云需在**百炼控制台开通**要用的模型（否则渠道配好也报"模型不可用"）；
DeepSeek 是**预付费**，未充值直接余额不足。

**② 令牌（pi-py 的入口凭据）**：「令牌 → 新建令牌」：

- 名称如 `pi-py-prod`；分组留 **default**（必须与渠道分组一致，否则请求不可达）；
- **额度填 -1（不限）**；过期时间设长或永不过期（换令牌要重启 app 生效）；
- 模型范围留空（不限）；
- 生成的 `sk-xxx` 写进 `.env` 的 `OPENAI_API_KEY`。

**③ embedding 渠道（建议走网关）**：embedding 同样是 OpenAI 类型渠道——阿里云
模型 `text-embedding-v3`（百炼控制台开通），`.env` 里
`PI_EMBEDDING_URL=http://llm-gateway:3000/v1` + `PI_EMBEDDING_API_KEY=sk-<令牌>`，
`PI_EMBEDDING_MODEL` 填 pi-py 侧名字并经重定向映射。

**验证与演练**：

```bash
# 1) 网关冒烟（在 ECS 上直接打）
curl -s http://127.0.0.1:3000/v1/models -H "Authorization: Bearer sk-<令牌>"

# 2) 跑一轮真实对话（§2.4 第 5 步），确认流式正常
# 3) 断渠道演练：网关 UI 禁用阿里云渠道 → 再跑一轮 →
#    应自动切到下一优先级渠道，用户无感；结束后重新启用
```

**常见坑**：

- **404"模型不存在"**：pi-py 请求的模型名既不在渠道「模型」列表、也没有「模型
  重定向」条目——把 `PI_FALLBACK_CHAIN` 里的每个名字都映射好再跑
- **请求全部失败**：令牌和渠道的分组不一致（一边 default 一边自定义）——统一用
  default 分组最省事
- **倍率没配**：不影响功能，只影响网关用量报表的金额数字——想按厂商成本归集就
  顺手把各家倍率填上
- **优先级都相同**：多厂商流量分配不可控——主力厂商优先级设高（如 10 vs 5）

**注意**：

- **存储用 SQLite（有意为之，不是偷懒）**：网关数据不是真相源——用户 token 账在
  pi-py 的 `usage_records` 表（MySQL），网关日志只是"每家厂商各花了多少"的对账单，
  丢了可用真相源重建；渠道/令牌十几行数据，几分钟可重录。SQLite 单文件 + docker
  volume，零运维。两条约定：① 网关 UI 设置里把**日志保存天数**设为 30-90 天，
  防文件无限增长；② `llm-gateway-data` 卷纳入备份范围。只有当需要**多网关实例 HA**
  或对网关日志做 SQL/BI 分析时才切 MySQL（`SQL_DSN` 环境变量，需先建独立库并授权）
- **流式透传**：new-api 默认透传 SSE，pi-py 的流式协议不受影响；网关的用量日志
  建议用采样/摘要模式，别把长流全量入库
- **双重退避**：pi-py 的 fallback.py 会重试 429/5xx，网关也会切渠道重试——网关侧
  渠道重试次数设 1 次即可，重试策略以 pi-py 侧为准，避免延迟叠加放大
- **模型名必须映射**：`PI_FALLBACK_CHAIN` 里的每个模型名都要在网关映射表里有条目，
  否则请求直接 404（网关报错明确，好排查）
- **兜底**：网关容器挂 = 模型不可用（app 不崩溃，run 报错）。可靠性要求更高时，
  可后续给 `PI_FALLBACK_CHAIN` 加"每入口独立 base_url/key"支持直连厂商兜底
  （`llm/registry.py` 小改，暂未实现）

## 3. 4 vCPU / 16 GiB 调优

| 参数 | 建议值 | 依据 |
|---|---|---|
| `PI_MAX_CONCURRENT_RUNS` | 16 | 回合是进程内异步、LLM 网络调用为主，不吃 CPU；16 是 `docker-compose.cloud.yml` 的兜底值（代码默认 8），要生效就别在 `.env` 里写死 8。"单实例 8 并发 p99 <5s" 是前任维护者的自托管压测数据，**本部署只实测过连接层，应用层未复测** |
| `PI_MAX_CONCURRENT_RUNS`（开 docker sandbox 时） | **8，不要照抄 16** | 容器现在有真的内存上限了，所以这里算的是乘法：`PI_SANDBOX_MEMORY × PI_MAX_CONCURRENT_RUNS` 必须明显小于物理内存。1g × 16 = 16 GiB = 整机内存，还没算温池里另外那批闲置容器（上限 `PI_SANDBOX_POOL_MAX`）和 app 自己。要么保持 8（1g × 8 = 8 GiB），要么把 `PI_SANDBOX_MEMORY` 降到 512m 再谈 16 |
| `PI_SANDBOX_MEMORY` | 1g（默认） | 单容器内存上限；同时设等值 `--memory-swap` 关掉 swap（docker 默认允许 2 倍）。**docker 自己的默认是完全不限制**（`Memory=0`），而注册是开放的，不设等于任何账号一条命令就能打爆这台机器。实测：容器内申请 2 GiB 被 OOM kill，exit 137 |
| `PI_SANDBOX_PIDS` | 256（默认） | 单容器进程数上限，挡 fork bomb；`0` = 不限。docker 默认也是不限（`PidsLimit` 未设、容器内 `ulimit -u` unlimited） |
| `PI_SANDBOX_CPUS` | 1.0（默认） | 单容器 CPU 上限（`NanoCpus` = 1e9/核）；空 = 不限。4 vCPU 的机器上 8 并发 × 1.0 允许超卖，靠 CFS 配额分时——这是想要的行为，别设成 0.125 去"精确平分"，那样单条命令会慢得离谱 |
| `PI_RATE_LIMIT_RUNS_PER_MIN` | 30/用户（默认） | 按用户固定窗口；用户多时够用，嫌 429 多就调高 |
| `PI_FORWARDED_ALLOW_IPS` | `172.16.0.0/12`（compose 已设） | **走 `--profile tls` 就是必填项**。uvicorn 默认只信 `127.0.0.1`，而 Caddy 是独立容器（源 IP `172.x`），头部会被静默丢弃 → 审计日志里的 IP 全是 Caddy 自己。`172.16/12` 覆盖 Docker 默认网段范围；在 compose 里固定 subnet 后可以收窄成那一格。**设错不报错**，只看启动日志那行 `trusted proxies for ...`。绝不能写 `*`（见 §17 第 16 条） |
| `PI_REPLICAS` | 1 | 单机多副本可用（锁/限流都在 Redis）；跨机扩容前 workspace 必须换共享存储（见下） |
| 内存 | app 进程本身无需限制；**沙箱必须限** | app + 并发回合本身远低于 16 GiB，但沙箱容器要按上面那行乘法算，caddy 另计。不开沙箱（local 模式）时命令跑在 app 容器里，反而没有任何内存上限——这也是应该开沙箱的理由之一 |

**已知限制（重要）**：workspace 和审计日志放在本机 named volume（`workspaces`、`audits`）。
单机部署没问题；将来要跨主机扩容副本，需要先把 workspace 迁到共享存储（NFS/云盘挂载），
否则不同副本看到的会话文件不一致。

## 4. Sandbox（工具执行隔离）说明

- **默认 = `cubesandbox`（CubeSandbox microVM，公网 SaaS 形态）**：compose 默认值，
  每会话独立 microVM，恶意多租户级隔离。需配 `PI_SANDBOX_TEMPLATE` + `PI_CUBE_API_KEY`
  （`PI_CUBE_API_URL`/`PI_CUBE_DOMAIN` 有默认值）；**配一半 = fail-closed**——模板或
  key 缺失时每次 bash 调用都报错、`/readyz` 翻 503，绝无静默回退。平台本身（coredns
  等）挂掉时同理（§2.5 的硬检查）。
  > ⚠️ **`docker-compose.cloud.yml` 只起 app + 中间件（llm-gateway / caddy），不含
  > CubeSandbox 平台本身。** 那套是独立安装包（`cubetoolbox`，13 个 `cube-sandbox-*`
  > systemd 服务：coredns / cube-api / cubelet / cube-proxy / cube-egress / cubemaster…），
  > 需按 [`production-deployment.md`](production-deployment.md) §2.1 / §5.1 单独部署到
  > 宿主机。compose 里只有 `PI_CUBE_API_URL` 等连接参数，不是平台本体。
- **`PI_SANDBOX=local`（为什么不再是默认）**：bash 在 app 容器内以非特权用户 `pi`
  （uid 10001）执行。有容器这层隔离（碰不到宿主机），但所有用户共享同一个 app 容器
  环境——**包括应用的全部环境变量**。任何注册用户都能 `echo $PI_JWT_SECRET` 拿到签发
  token 的密钥、从 `PI_DATABASE_URL` 里读出 RDS 密码。裸跑（不在容器里）时更糟：
  那是宿主机 root。所以 local 只适合完全信任的单机场景。
- **`PI_SANDBOX=docker`（自托管单机/团队形态，本地开发也用这个）**：每个 workspace
  一个常驻容器，首次 bash 调用时惰性创建（会话级复用，不是 turn 开始时预热）；
  命令通过 `docker exec` 进入（单次调用
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
  **但沙箱管不到 `web_fetch`/`web_search`**——它们跑在应用进程里，见 §17 第 20 条，
  目前靠 `policy.json` 的 `deny_tools` 整个禁掉。
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
