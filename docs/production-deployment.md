# pi-py × CubeSandbox 生产部署手册（照做即可上线 + 大内存服务器适配）

> 这份手册的每一行都来自一台真实跑通、且已经过"上传→进沙箱→处理"全链路
> 验证的部署。目标读者是**拿到这套代码、要在自己服务器上把它跑起来的人**：
> 你不必复现作者的每一次试错，只需按本文顺序执行，遇到"改什么"的地方都标了
> ★，并给出从 3.6G 小内存一路适配到 64G/128G 大内存的调参方法。
>
> 一句话总结作者机器：**一台只有 4 核 3.6GB 内存、40GB 磁盘的腾讯云 Ubuntu
> 24.04，就把 CubeSandbox 平台 + pi-py 服务 + 文件管线（对象存储）全部跑通了**。
> 你迁移到更大的内存服务器，部署方式完全一样，唯一要动的就是第 9 节那几个数字。

---

## 0. 作者服务器真实画像（这台是怎么跑通的）

| 项 | 值 | 说明 |
|---|---|---|
| 规格 | 4 vCPU / 3.6 GiB / 40 GiB 系统盘 | 腾讯云标准型，**磁盘已被用到 95%** |
| 系统 | Ubuntu 24.04 LTS | 宿主 Python 3.12 |
| 虚拟化 | /dev/kvm 可用 | 硬前提，见 §3 |
| 网卡 | eth0 = `10.0.0.8/22`；docker0=`172.17.0.1`；cube 网桥若干 | 见 §2 拓扑 |

**内存账本（3.6G 上怎么塞下的）**：

```
宿主常驻 ≈ CubeSandbox 平台(~1.0G) + MySQL(~0.4G) + Redis/registry/webui(~0.5G) + pi-py serve(~0.12G) + 系统(~0.6G)
沙箱并发 ≈ 并发数 × (256M VM 配额 + ~20% 虚拟化开销)
```

作者在这台机器上并发起 8 个 256M 沙箱时内存到极限（free 仅剩几十 MB），结论是
**内存不是单机瓶颈，CPU 才是**（4 核时沙箱创建/工具调用/模型往返排队）。所以：

> **迁往大内存服务器 = 把"沙箱并发数"和"池大小"按新内存放大，其余都不变。**
> 具体每个参数怎么改 → 第 9 节。

---

## 1. 组件全景 + 精确版本清单（照这个版本走，别漂移）

> 版本敏感的东西只有三样：**e2b 系 SDK、envd、平台 CLI**。其余（fastapi、
> boto3 等）用 `pyproject.toml` 锁住的即可，装最新也不会崩。

| 组件 | 版本 | 角色 / 用途 |
|---|---|---|
| **CubeSandbox 平台** | `v0.7.2`（CLI `f1aaa737...` built 2026-09-24） | 沙箱控制面 + 数据面 + 模板中心 |
| **envd**（沙箱内探针二进制） | `0.5.13`（静态 Go，~10MB） | CubeMaster 探活 `:49983/health`，**缺失则容器起不来** |
| **沙箱镜像** | `pi-sandbox:1.0`（`alpine:3.20` + Python `3.12.13`） | 由本仓库 `deploy/sandbox/Dockerfile` 构建，见 §6 |
| **沙箱模板** | `tpl-2492096525f04f0aac655acb`（alias `pi-sandbox-ab`） | cpu=1000m / mem=256Mi / writable-layer=1Gi |
| e2b | `2.26.0` | 沙箱 SDK（连 CubeAPI 3000） |
| e2b-code-interpreter | `2.8.1` | 沙箱 SDK 扩展 |
| boto3 / botocore | `1.43.103` | 对象存储预签名（文件管线） |
| fastapi / uvicorn | `0.141.1` / `0.54.0` | Web 服务框架 |
| SQLAlchemy / aiomysql | `2.1.1` / `0.3.2` | 异步 ORM + MySQL 驱动（**连接串用 `mysql+aiomysql://`**） |
| alembic | `1.20.0` | 数据库迁移（服务启动自动 `upgrade head`） |
| httpx / pydantic | `0.28.1` / `2.13.5` | HTTP / 数据模型 |
| Python | 宿主 `3.12` + 沙箱 `3.12.13` | 两侧一致，musllinux wheel 通用 |
| MySQL | `8.0`（镜像 `opensource/mysql:8.0`） | 业务库（用户/会话/审计/文件元数据） |
| Redis | `7-alpine` | 会话锁/缓存（db15） |
| MinIO | `minio/minio`（latest） | 文件管线对象存储（**pi-minio，端口 19000**） |
| registry | `registry:2` | 沙箱镜像仓库（本地 5000） |

---

## 2. 依赖服务拓扑 + 端口对照（先看，避免起冲突）

一个单机上有**两套东西**：CubeSandbox 平台自带的依赖，和 pi-py 自己加的。
端口冲突（尤其 MinIO 的 9000）是"服务起不来"的头号原因。

### 2.1 CubeSandbox 平台（v0.7.2，随平台部署包安装，13 个 systemd 服务）

| 名称 | 形态 | 端口/说明 |
|---|---|---|
| `cube-sandbox-cubemaster` | 宿主进程 | **:8089** 控制面（`cubemastercli` 连这里） |
| `cube-sandbox-cube-api` | 宿主进程 | **:3000** E2B SDK 兼容层（pi-py 的 `PI_CUBE_API_URL`） |
| `cube-sandbox-cubelet` | 宿主进程 | 每宿主一个的数据面 agent（建/杀 VM） |
| `cube-sandbox-cube-proxy` | 宿主进程 | 沙箱数据面转发 |
| `cube-sandbox-cube-templatecenter` | 宿主进程 | 镜像 → 模板构建（§6 依赖它） |
| `cube-sandbox-cube-egress` | 宿主进程 | 出网网关（**沙箱出网白名单做在这里**） |
| `cube-sandbox-cubeops` | 宿主进程 | 运维/健康 |
| `cube-sandbox-coredns` / `cube-sandbox-dns` | 宿主进程 | 沙箱 DNS（`169.254.254.53`） |
| `cube-sandbox-webui` | 容器(openresty) | **:12088** 平台控制台 |
| `cube-sandbox-redis` | 容器 | `127.0.0.1:6379`（平台内部） |
| `cube-sandbox-mysql` | 容器 | `127.0.0.1:3306`（平台内部） |
| `cube-sandbox-minio` | 容器 | `10.0.0.8:9000`（**平台自己的归档 MinIO，别动**） |

### 2.2 pi-py 自己加的（本仓库负责）

| 名称 | 端口 | 说明 |
|---|---|---|
| **pi-minio** | `0.0.0.0:19000`（S3）/ `127.0.0.1:19001`（console） | 文件管线对象存储；**故意避开平台的 9000** |
| **cube-registry** | `0.0.0.0:5000` | 沙箱镜像仓库（平台部署包的 registry，也可能平台已带） |
| **pi-py serve** | `127.0.0.1:8300` | 本服务；**只绑本机，绝不暴露公网** |

> **端口冲突铁律**：MinIO 默认就是 9000/9001，而平台自带一个 minio 占着。
> 你的文件管线 MinIO 必须换端口（本环境 19000/19001），否则和平台 minio 抢端口，
> 两个里至少一个起不来。Milvus 的 etcd 2379 / gRPC 19530 也要在起之前核对。

---

## 3. 硬前提：嵌套虚拟化 KVM

CubeSandbox 用 QEMU/PVM 起微虚拟机，**没有 `/dev/kvm` 全部空谈**。

```bash
ls -l /dev/kvm                      # 必须有这个设备
sudo apt install -y qemu-kvm && sudo kvm-ok   # 提示 "KVM acceleration can be used" 才过
```

云厂商注意：部分机型默认关闭嵌套虚拟化，需在控制台或工单开启（腾讯云大部分标准型
默认支持；阿里云要工单申请）。**买机器前先确认 KVM，这是唯一没法靠代码绕过的前提。**

---

## 4. 中间件部署

pi-py 的依赖：MySQL + Redis +（文件管线）MinIO；记忆层 Milvus 可选（不配自动降级）。

> 资源紧张时可**复用平台自带的 MySQL/Redis**（作者这台就这么干的：在平台的
> `cube-sandbox-mysql` 里建 `pi_py` 库、用平台 redis 的 db15）。生产建议独立。

### 4.1 MySQL 8.0（业务库）

```bash
docker run -d --name pi-mysql --restart always \
  -e MYSQL_ROOT_PASSWORD=CHANGE_ME \
  -v /data/mysql:/var/lib/mysql \
  -p 127.0.0.1:3306:3306 mysql:8.0
# 只需建库：CREATE DATABASE pi_py; 表由服务启动时 alembic 自动建
```

### 4.2 Redis（会话锁/缓存，db15）

```bash
docker run -d --name pi-redis --restart always \
  -p 127.0.0.1:6379:6379 redis:7-alpine redis-server --requirepass CHANGE_ME
```

### 4.3 MinIO（文件管线对象存储 → 端口必须换成 19000 系）

```bash
docker run -d --name pi-minio --restart always \
  -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=CHANGE_ME \
  -v /data/minio:/data \
  -p 0.0.0.0:19000:9000 -p 127.0.0.1:19001:9001 \
  minio/minio server /data --console-address ":9001"
# ⚠️ 端口改了 19000/19001 —— 避开平台 minio 的 9000/9001（§2 铁律）
# 启动后建两个 bucket：pi-files（上传）pi-artifacts（产物），见 §7 的 PI_S3_BUCKET_*
```

### 4.4 Milvus（记忆向量库，可选）

`PI_MILVUS_URI` + `PI_EMBEDDING_MODEL` 都配才启用；不配自动降级 MySQL 余弦兜底。
官方 standalone：`standalone_embed.sh` 或 `docker compose`，端口 19530(gRPC)/2379(etcd)，
内存 ~3GB。生产要记忆才需要，本环境是降级跑的。

---

## 5. CubeSandbox 平台部署（v0.7.2）

平台以**官方部署工具/安装包**分发（cubemaster、cubelet、registry、proxy/lifecycle/
egress 等）。本节写组件关系与必须核对点，**具体安装命令以你拿到的 v0.7.2 部署包为准**。

- **CubeMaster**：控制面，`:8089`（`cubemastercli` 默认连 `0.0.0.0:8089`）。
- **CubeAPI**：E2B SDK 兼容层，`127.0.0.1:3000`（pi-py 的 `PI_CUBE_API_URL`）。
- **Registry**：`:5000`，存沙箱模板镜像（平台一般自带 `cube-registry`）。
- **CubeTemplateCenter**：镜像→模板构建服务；它不可用则 §6 的 `create-from-image` 直接失败。
- **Cubelet**：每宿主一个，配置文件里指定数据目录 + 上报告制面 + 资源池。

部署完自检：

```bash
cubemastercli list            # 能看到宿主在册
curl -s http://127.0.0.1:3000/sandboxes \
  -H "Content-Type: application/json" -d '{"templateID":"tpl-xxx"}'   # API 层能建沙箱
```

---

## 6. 沙箱镜像制作（完整流程，一条条照抄）

pi-py 的沙箱需要"能跑 pandas / openpyxl / python-docx / pypdf / 7z / poppler"的
环境。本仓库 `deploy/sandbox/Dockerfile` 已经是成品，按下面 6 步走即可。

### 6.1 构建素材（已在仓库，不用你找）

`deploy/sandbox/` 下有 4 个文件：

| 文件 | 作用 |
|---|---|
| `Dockerfile` | 镜像定义（alpine:3.20 + python3 + A/B 库 + envd 骨架） |
| `envd` | CubeSandbox 平台探针二进制（v0.5.13，~10MB，静态 x86-64） |
| `cube-entrypoint.sh` | ENTRYPOINT：拉起 envd 常驻 |
| `verify.sh` | 构建即自检（命令 + 11 个 Python 库 + zip/tar/PDF 往返） |

> `envd`/`cube-entrypoint.sh` 来自 CubeSandbox 平台分发（或从平台任一可用镜像里
> `docker run --rm --entrypoint cat <img> /usr/bin/envd` 提取）。**换平台版本时，
> 这两个文件要换成对应版本的**，否则与 CubeMaster 行为不符。

### 6.2 构建（国内必须走腾讯云 mirror，否则直连官方源极慢）

```bash
cd /path/to/pi-py
docker build -t pi-sandbox:1.0 -f deploy/sandbox/Dockerfile .
# 构建过程会打印 "== 自检全部通过 =="，任何库/命令缺失都会 fail —— 这是有意的
```

### 6.3 推 registry

```bash
docker tag  pi-sandbox:1.0 127.0.0.1:5000/pi-sandbox:1.0
docker push 127.0.0.1:5000/pi-sandbox:1.0
```

### 6.4 建模板（镜像 → 可调度模板）

```bash
cubemastercli template create-from-image \
  --image 127.0.0.1:5000/pi-sandbox:1.0 \
  --alias pi-sandbox-ab \
  --cpu 1000 --memory 256 \
  --writable-layer-size 1Gi
# 完成后 template list 出现 READY 的 tpl-xxx，记下这个 id → 填 PI_SANDBOX_TEMPLATE
```

> `--cpu 1000`=1 核、`--memory 256`=256MiB、`--writable-layer-size 1Gi`=沙箱可写层
> （workspace 所在）1GB。这三个是"模板管配额"，**不吃内存的任务保持 256Mi 即可，
> 大文件/重型任务才加到 512Mi~1Gi**（见 §9）。

### 6.5 预热首个沙箱（大镜像必做）

A+B 镜像解包成 ext4 后约 400MB+，**模板 READY 后第一个沙箱冷启动可能 502**
（openresty 探活超时），后续实例化（rootfs 已缓存）稳定。上线前先预热一次：

```bash
# 用 SDK 或 API 建一个沙箱跑 sleep 再销毁，把 rootfs 在节点上热起来
```

### 6.6 镜像契约（写 Dockerfile 的人必读，缺一条容器就起不来）

1. **`/usr/bin/envd` 缺一不可**：ENTRYPOINT 里要 `envd -port 49983` 后台常驻，
   CubeMaster 靠 `:49983/health` 探活。缺 envd → `Exec mount failed`。
2. **ENTRYPOINT 必须是 `cube-entrypoint.sh`，`CMD []`**：若 `CMD ["python3"]`，
   主进程读完 stdin 即退 → `mount namespace` 失败。
3. **base 用 `alpine:3.20`，别用官方 `python:3.12-alpine`**：后者实测
   `reset guest time failed: BrokenPipe`（CubeSandbox guest 对 base 敏感）。
   做法：`FROM alpine:3.20` + `apk add python3 py3-pip`（PEP 668 需 pip.conf 加
   `break-system-packages=true`）。
4. **GNU 全集工具必装**（coreutils/findutils/grep/sed/gawk）：busybox 阉割版
   撑不起模型生成的 `grep -P` / `find -exec` / `sed -i`。
5. **构建源 mirror**：apk `mirrors.tencentyun.com/alpine` + pip
   `mirrors.tencentyun.com/pypi/simple`（换云厂商改对应 mirror）。

---

## 7. pi-py 服务部署 + 完整环境变量表

```bash
sudo python3.12 -m venv /opt/pi-venv
sudo -E /opt/pi-venv/bin/pip install -e /path/to/pi-py[production]   # production 含 boto3
```

下面这张表是**全部环境变量**（含默认值），★=上线必改。写进 `/etc/pi.env`，
systemd `EnvironmentFile` 引用。

### 7.1 核心（★ 必改）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PI_DATABASE_URL` | 无（不设直接起不来） | `mysql+aiomysql://USER:PASS@127.0.0.1:3306/pi_py` ★ |
| `PI_JWT_SECRET` | 空=自动生成并落盘 | 生产显式设强随机 ★ |
| `PI_MODEL` | `openai/gpt-4o` | 你的模型路由（如 `openai/qwen3.8-flash`）★ |
| `OPENAI_BASE_URL` | — | 模型网关 base_url ★ |
| `OPENAI_API_KEY` | — | 模型网关 key ★ |
| `PI_SANDBOX` | 空(本地模式) | `cubesandbox`（本手册场景） |
| `PI_SANDBOX_TEMPLATE` | 空 | §6 建出的 `tpl-xxx` ★ |
| `PI_CUBE_API_URL` | `http://127.0.0.1:3000` | CubeAPI 地址 |
| `PI_CUBE_API_KEY` | `e2b_000000` | 换强随机 ★ |
| `PI_CUBE_DOMAIN` | `cube.app` | 沙箱数据面域名后缀 |
| `SSL_CERT_FILE` | — | 平台自签 CA + 系统 CA 合并文件（**是 `SSL_CERT_FILE`，httpx 标准变量**）★ |

### 7.2 沙箱池（会话级复用，内存压力自适应）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PI_SANDBOX_POOL_SIZE` | `4` | 池上限（VM 数）。**大内存服务器按 §9 放大** |
| `PI_SANDBOX_POOL_TTL` | `900` | 空闲回收秒数（内存宽裕时） |
| `PI_SANDBOX_POOL_TTL_TIGHT` | `300` | 内存紧张时的回收秒数 |
| `PI_SANDBOX_POOL_PRESSURE_HIGH` | `1572864000`(1.5G) | 可用内存低于此 → 进入紧张档 |
| `PI_SANDBOX_POOL_PRESSURE_LOW` | `536870912`(512M) | 高于此 → 回到宽裕档 |
| `PI_SANDBOX_NET` | 空=断网 | `host`=开外网（web 抓取类任务才开；出网白名单在宿主 egress） |
| `PI_MAX_CONCURRENT_RUNS` | `8` | 服务端并发上限，必须 ≤ 平台能同时养的 VM 数 |
| `PI_RUN_TIMEOUT_SECONDS` | `600` | 单回合超时 |

### 7.3 文件管线（对象存储，P0-P2 的核心）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PI_S3_ENDPOINT` | 空 | 如 `http://10.0.0.8:19000`。**取值原则见 §8.3** ★ |
| `PI_S3_ACCESS_KEY` / `PI_S3_SECRET_KEY` | 空 | MinIO 凭据 ★ |
| `PI_S3_BUCKET_FILES` | `pi-files` | 上传 bucket（需预先建好） |
| `PI_S3_BUCKET_ARTIFACTS` | `pi-artifacts` | 产物 bucket（预留） |
| `PI_S3_REGION` | `us-east-1` | 任填（MinIO 忽略） |
| `PI_MAX_UPLOAD_BYTES` | `943718400`(900M) | 上传大小上限；>900M 后续流式 |

### 7.4 其余（多数保留默认）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PI_REDIS_URL` | 空 | `redis://:PASS@127.0.0.1:6379/15`（不配则退内存缓存） |
| `PI_WORKSPACE_ROOT` | `<base>/workspaces` | 会话 workspace 宿主目录 |
| `PI_TRACER` | `jsonl` | 追踪落盘方式 |
| `PI_METRICS_TOKEN` | 空 | `/metrics` 拉取 token ★ |
| `PI_POLICY` | 空 | 命令/文件策略 yaml |
| `PI_TOKEN_TTL_MIN` | `720` | JWT 有效期 |
| `PI_MILVUS_URI` / `PI_EMBEDDING_MODEL` | 空 | 记忆层（可选） |
| `PI_SKILLS_DIR` / `PI_MCP_SERVERS` | 空 | 技能/MCP |

### 7.5 systemd 单元

```ini
[Unit]
Description=pi-py agent service
After=network.target

[Service]
EnvironmentFile=/etc/pi.env
ExecStart=/opt/pi-venv/bin/python -m pi.cli serve --host 127.0.0.1 --port 8300
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now pi
sudo journalctl -u pi -f    # 看启动日志，确认没有 PI_DATABASE_URL 报错
```

**安全三连**：①`8300` 有开放注册（`/v1/auth/register` 无鉴权）→ 只绑 127.0.0.1，
公网入口走反代 + 认证；②`PI_CUBE_API_KEY`/`PI_JWT_SECRET`/所有密码换强随机；
③`/metrics` 只会对持有 `PI_METRICS_TOKEN` 的请求放行。

---

## 8. 沙箱 VM 网络隔离（这是最容易踩的认知坑）

**结论：CubeSandbox 沙箱 VM 只能"NAT 单向出公网"，访问不到宿主的任何内网服务。**

实测：在开网的沙箱内 `curl` 宿主的 eth0(10.0.0.8)/docker0(172.17.0.1)/cube 网关
(169.254.68.5) 的 MinIO 端口，**全部 000 不可达**；而访问公网（网页）是通的。
也就是说：VM 内的 `127.0.0.1` 是 VM 自己，宿主的 MinIO/MySQL/pi-py 它都够不着。

### 8.1 它对文件管线的直接影响

"沙箱 curl 直拉 MinIO 预签名 URL"这条主流做法，在这个平台隔离下**物理不通**。
因此 pi-py 的 `fetch_file`（拉文件进沙箱）用的是**服务器内存中转（方案 B）**：

```
MinIO 对象 ──服务器 get_bytes──▶ 服务器内存 ──SandboxFS.write──▶ VM 的 /workspace
```

文件真身始终在 MinIO、**服务器磁盘零占用**（只内存过一遍）；上传/下载两端仍是
预签名直连。当前中转上限 256MB（防 OOM 宿主），大文件流式记档待做。若你将来给
MinIO 一个公网地址，可无缝切回 VM 真直连。

### 8.2 出网控制（只影响"联网抓取"类任务）

沙箱默认断网。`PI_SANDBOX_NET=host` 才开外网；**模板级的 denyall/nonet 在沙箱内
仍可被绕过**（实测），真正的白名单必须做在宿主 cube-egress 层 —— 记住
"模板管配额、宿主管网络"。

### 8.3 `PI_S3_ENDPOINT` 怎么填（别人部署问得最多）

这个值同时干两件事，所以必须让**两方都可达**：

1. **服务器**用 boto3 连它去签发/读对象（fetch_file 中转、head 验证）→ 服务器可达；
2. **客户端**（浏览器预签名直传、`curl` 下载）要拿着它生成的 URL 去 MinIO →
   **客户端可达**。

因此 `127.0.0.1` 只在"客户端就是服务器同一台机"时成立；生产上客户端是别的机器，
必须填**内网 IP 或经反代的公网域名**。作者测试机填的 `http://10.0.0.8:19000`
（eth0 内网 IP），生产建议换成内网 DNS 名或公网反代。

---

## 9. 迁往大内存服务器的适配（只动这几个数）

部署方式与 3.6G 小机**完全相同**，按新内存放大下面几个值即可：

| 参数 | 3.6G 本机值 | 8C/64G 建议 | 16C/128G 建议 | 依据 |
|---|---|---|---|---|
| `PI_SANDBOX_POOL_SIZE` | 4 | 8-12 | 12-24 | 池 = 常驻 VM 数；每 VM ≈ 256M×1.2 |
| `PI_SANDBOX_POOL_PRESSURE_HIGH` | 1.5G | 16G | 32G | 可用内存低于此进"紧张档"（回收加速） |
| `PI_SANDBOX_POOL_PRESSURE_LOW` | 512M | 8G | 16G | 回到"宽裕档"阈值 |
| `PI_MAX_CONCURRENT_RUNS` | 8 | 16 | 32 | 受 **CPU** 限制而非内存（4 核→8 核→16 核） |
| `--writable-layer-size`（模板） | 1Gi | 2Gi | 4Gi | 沙箱 workspace 磁盘；处理大文件时加大 |
| 沙箱模板 `--memory` | 256Mi | 256/512Mi | 512Mi/1Gi | Python agent 任务 256Mi 够；重型任务才加 |
| `PI_MAX_UPLOAD_BYTES` | 900M | 900M 不变 | 900M 不变 | 上限由"流式未实现"决定，与内存无关 |
| fetch_file 中转上限 | 256M（代码常量） | 可放宽到 512M+ | 可放宽 | `pi/tools/files.py::_MAX_FETCH_BYTES` |

**核心理由再强调**：沙箱并发上限由 **CPU 核数**决定（每沙箱 1 核 vCPU），内存只是
容纳"常驻服务 + 池 + 峰值 VM"的容器。所以升级顺序是：**先升核数撑并发，内存跟着
放大池/压力阈值即可，别提前把所有数字都拉满。**

---

## 10. 配额与并发调参（实测值）

| 参数 | 本机实测 | 建议 | 说明 |
|---|---|---|---|
| 沙箱模板配额 | 256M/1核 | 256M-1G/1核 | 256M 跑 Python agent 任务 OK |
| 平台并发安全值 | 6-8（4核） | 核数 × 1.5-2 | 看 `pi_sandbox_create_duration` 是否恶化 |
| 命令超时 | 120s 默认/600s 上限 | 不变 | 沙箱内 GNU timeout 包装，退出码 124 |
| close 总超时 | 90s | 不变 | 超时后清理线程继续收尾，VM 必死 |
| 平台 VM 空闲回收 | 600s | 不变 | 最后防泄漏防线 |

**错误码速记**：平台返回 `error code 130597: no more resource` = 平台配额耗尽 →
先 `cubemastercli list` 看堆积 VM，kill 泄漏源，别盲目重试。

---

## 11. 生产化清单（部署完逐项打勾）

- [ ] `ls /dev/kvm` 在（嵌套虚拟化开）—— 没有它沙箱全挂
- [ ] 8300 只绑本机；公网仅 80/443 反代；`/metrics` 带 token
- [ ] 所有 `CHANGE_ME` / `e2b_000000` 换成强随机
- [ ] `PI_S3_ENDPOINT` 用客户端可达地址（§8.3）；MinIO 已建 `pi-files`/`pi-artifacts`
- [ ] 模板 READY 且做了首次预热（§6.5）
- [ ] `pi_py` 库自动迁移成功（`journalctl -u pi` 里无 migration 报错）
- [ ] 端到端冒烟：建会话 → 上传文件 → 让 agent `list_files`+`fetch_file` 处理 → 结果正确
- [ ] `/metrics` 四个沙箱系列在（create/close/health/trace failures）
- [ ] 归档目录可写；配 MinIO 后归档 json 的 `s3` 字段 non-null
- [ ] 备份：MySQL 每日 dump；归档同步异机/对象存储

---

## 12. 避坑清单（全部实测踩过）

| 坑 | 现象 | 规避 |
|---|---|---|
| 无 /dev/kvm | 建沙箱报 virtualization 错误 | 买前确认嵌套虚拟化，`kvm-ok` 先测 |
| MinIO 端口撞平台(9000) | pi-minio 或平台 minio 起不来 | 文件管线 MinIO 用 19000 系（§2/§4.3） |
| PI_S3_ENDPOINT 用 127.0.0.1 | 客户端直传时 URL 指向自己 → 403/连不上 | 用客户端可达地址（§8.3） |
| boto3 签名 403(SigV2) | 预签名 PUT 时 `SignatureDoesNotMatch` | storage 里强制 `Config(signature_version="s3v4")`（已内置） |
| 沙箱镜像缺 envd/entrypoint | `Exec mount failed` / `mount namespace` | §6.6 三条契约 |
| base 用官方 python 镜像 | `reset guest time BrokenPipe` | `FROM alpine:3.20`（§6.6） |
| 大镜像首个沙箱 502 | 模板 READY 后首启超时 | §6.5 预热 |
| VM 泄漏 | 跑几次后 `no more resource` | finally close + close 超时 + 平台回收（已内置） |
| 模板级断网无效 | denyall 模板仍出网 | 出网控制在宿主 egress（§8.2） |
| 8300 暴露公网 | 任何人可注册 | 只绑本机 + 反代认证 |
| `PI_MAX_CONCURRENT_RUNS` 开太大 | 排队任务全撞配额 | ≤ 平台 VM 上限 |
| Galera/Milvus/MySQL 端口冲突 | 各服务起不来 | 部署前过一遍 §2 端口表 |
| 连接串写 `mysql+pymysql`（同步） | 异步栈性能/兼容问题 | 用 `mysql+aiomysql://` |

---

## 13. 从本机迁移到大内存服务器（动作清单）

1. 新机装 Ubuntu 24.04 → 开嵌套虚拟化 → `kvm-ok` 过。
2. Docker + 镜像加速 → 部署 §4 中间件（MySQL/Redis/MinIO **19000**/可选 Milvus）。
3. 部署 CubeSandbox 平台（§5）→ 推模板镜像（§6.1-6.3）→ 建模板（§6.4）→ 预热（§6.5）。
4. 拷 pi-py 源码 → venv 装 `[production]` → 写 `/etc/pi.env`（§7 全表，按 §9 调资源数字）→ systemd 起服务。
5. 按 §11 清单逐项打勾；重点跑一次 §11 的"端到端冒烟"（上传→进沙箱→处理）。
6. 旧机收尾：保留 30 天归档与审计日志后下线。