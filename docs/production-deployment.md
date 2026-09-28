# pi-py × CubeSandbox 生产部署手册（单机 → 云服务器）

> 本手册的每一行都来自真实跑通的部署：作者在一台 **1 核 3.6GB 内存的
> Ubuntu 24.04 云主机**上完成了 CubeSandbox v0.7.2 沙箱平台 + pi-py Agent
> 服务的全部搭建、压力验证与生产就绪加固。因此本文不是"照文档抄"，而是
> **踩过坑之后的可用清单** —— 资源数字、并发值、错误码、规避动作均为实测。
>
> 适用目标：8 核 64GB ~ 16 核 128GB 的云服务器，单机部署全部组件。

---

## 0. 配置选型建议（先看这个）

| 项目 | 8 核 64GB | 16 核 128GB |
|---|---|---|
| 中间件（MySQL/Redis/Milvus/MinIO）| 余量充足 | 余量非常充足 |
| 沙箱并发（256M VM）| 6-12 个并行 | 12-24 个并行 |
| 并发 eval / 跑分 | 2-3 任务并行 | 6-10 任务并行 |
| 月成本 | 低 | 中 |

**关键判断**：作者在 1 核 3.6GB 上已把**全部组件**跑通（平台 ~1GB、pi-py 117MB、
系统 ~500MB、8 个沙箱 VM 并发时内存到极限 68MB free）。所以**内存从来不是
单机瓶颈，CPU 才是** —— 1 核时沙箱创建/工具调用/模型往返全部串行。

- **预算敏感 → 8 核 64GB**：首期生产 + 2-3 路并行 eval 完全够。65GB 里：
  平台 1GB + MySQL 1.5GB + Redis 1GB + Milvus 3GB + MinIO 1GB + 系统 2GB ≈ 9.5GB，
  剩下 54GB 够 12+ 个 4GB VM（或 20+ 个 256M VM）。
- **"看真实生产情况"（你的原话）→ 16 核 128GB**：并发 eval、多模板、
  大向量索引、MySQL 大 buffer 都不再需要精打细算；这个规格单机就能模拟
  一个小团队的真实负载。

**我的建议**：先租 **8 核 64GB** 起步。本手册第 7 节的并发模型可以精确算出
你需要的规模；真到 64GB 打满（那意味着单机 20+ 并发沙箱任务），再升级 128GB
也不迟 —— 因为部署方式完全一样，只是改两个数字。

---

## 1. 组件全景与资源基线（本地 3.6G 实测）

| 组件 | 角色 | 本地实测内存 | 说明 |
|---|---|---|---|
| Cubelet | 沙箱数据面 agent（宿主机）| 261 MB | 每台宿主一个（现在 + 未来水平扩容各加一个）|
| CubeMaster | 控制面（模板/快照/调度元数据）| 35 MB | 单点，随集群唯一 |
| CubeTemplateCenter | 模板构建服务（镜像 → 模板）| 19 MB | 构建期间峰值更高 |
| cube-proxy / cube-lifecycle / cube-egress | 数据面周边 | 各 ~50MB | 与 cubelet 同宿主 |
| 平台容器 ×8（registry/redis/webui/minio/mysql）| 平台自身依赖 | ~600MB | registry 可换云镜像仓库 |
| mysqld（宿主）| 业务库（用户/会话/audit/trace）| 126 MB | 中工作量下稳定值 |
| pi-py serve | Agent 服务 | 117 MB | 每进程 |
| MinIO（宿主）| 工作区归档 S3 | 66 MB | 归档可选 |
| 沙箱 VM（256M/1核）| 每次任务一个 | 256MB + 开销 | 平台模板配置 |

**内存模型（单机关键公式）**：

```
并发沙箱内存 ≈ 并发数 × (VM 配额 + 20% 虚拟化开销)
宿主常驻 ≈ 平台(1GB) + 中间件(MySQL1.5+Redis1+Milvus3+MinIO1) + pi-py(0.2) + 系统(2)
```

8 核 64GB 上：`常驻 9.7GB`，沙箱预算 `54GB ÷ 0.3GB ≈ 100+ 个 256M VM`，
CPU 限制（8 核）下实际可用 12-24 个同时跑 —— 平台并发上限远低于内存上限，
**CPU 是单机并发的真实天花板**。

---

## 2. 云服务器准备（硬前提：嵌套虚拟化）

CubeSandbox 用 KVM（QEMU/PVM）跑微虚拟机，**没有 /dev/kvm 一切都是空谈**。

```bash
# ① 镜像：Ubuntu 24.04 LTS（实测环境），或 22.04 LTS
# ② 买/开后立刻确认 KVM 可用：
ls -l /dev/kvm           # 必须有这个设备
# 云厂商注意：部分机型默认关闭嵌套虚拟化，需在控制台/工单开启
#   腾讯云：大部分标准型/计算型默认支持（开/关在控制台-实例-更多）
#   阿里云：需要工单申请开启 KVM 嵌套虚拟化
sudo apt install -y qemu-kvm && sudo kvm-ok   # 提示 KVM acceleration can be used 才算过
```

```bash
# ③ 磁盘与分区：建议 100GB+ 系统盘（模板镜像、工作区归档、MySQL 落盘）
#    数据目录放数据盘，挂载后 xfs/ext4 均可（本地为 xfs 实测 OK）
# ④ 安全组（云上最重要的一个动作）：
#    80/443   → 只对公网反代开放（Caddy/域名 Web）
#    8300     → 绝不暴露公网！pi-py 有开放注册接口，只允许本机/内网
#    KVM/网格端口（见 cube 平台安装文档）→ 只允许宿主网段
```

---

## 3. 中间件部署（全部 Docker，单机共存）

### 3.1 Docker 与镜像加速

```bash
curl -fsSL https://get.docker.com | bash
# 国内加速（腾讯云内网免加速）：配 /etc/docker/daemon.json
# {"registry-mirrors": ["https://mirror.ccs.tencentyun.com"]}
sudo systemctl enable --now docker
```

### 3.2 MySQL 8.0（业务库）

```bash
docker run -d --name pi-mysql --restart always \
  -e MYSQL_ROOT_PASSWORD=CHANGE_ME \
  -v /data/mysql:/var/lib/mysql \
  -p 127.0.0.1:3306:3306 \
  mysql:8.0
# pi-py 需要：CREATE DATABASE pi_py;（建表由服务自动 migrate）
```

### 3.3 Redis（pi-py 会话锁/缓存，db15；Cube 平台自带自己的 redis）

```bash
docker run -d --name pi-redis --restart always \
  -p 127.0.0.1:6379:6379 redis:7-alpine redis-server --requirepass CHANGE_ME
# 只绑 127.0.0.1 —— 有密码也别暴露公网
```

### 3.4 MinIO（工作区归档 S3 目标，可选但推荐）

```bash
docker run -d --name pi-minio --restart always \
  -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=CHANGE_ME \
  -v /data/minio:/data \
  -p 127.0.0.1:9000:9000 -p 127.0.0.1:9001:9001 \
  minio/minio server /data --console-address :9001
# ⚠️ 端口冲突警告：Cube 平台自带一个 minio 容器默认也占 9000/9001！
#   二选一：① 只用平台的 minio（归档指向它）② 改本容器端口。
```

### 3.5 Milvus（pi-py 记忆层向量库，可选但"生产"建议要）

pi-py 的记忆检索：`PI_MILVUS_URI` + `PI_EMBEDDING_MODEL` 都配置才启用；
**不配时自动降级为 MySQL 余弦兜底（index_fallbacks 指标可观测），不会崩**。
本地验证阶段就是降级跑的 —— 生产开启记忆需要 Milvus standalone：

```bash
# 官方 standalone（etcd + minio + milvus 三个容器）：
curl -sfL https://raw.githubusercontent.com/milvus-io/milvus/v2.4.x/scripts/standalone_embed.sh -o /tmp/milvus.sh
bash /tmp/milvus.sh    # 或手动 docker compose（milvusdb/milvus:v2.4-stanchalone）
# 端口：19530(gRPC) 2379(etcd) —— 注意与 pi-minio 的 9000 错开
# 内存：~3GB；磁盘：向量数据落盘 /data/milvus
```

验证：`python -c "from pymilvus import MilvusClient; c=MilvusClient('http://127.0.0.1:19530'); print(c.list_collections())"`

---

## 4. CubeSandbox 平台部署（v0.7.2）

> 平台官方以部署工具/文档分发（cubemaster、cubelet、registry、带 proxy/
> lifecycle/egress 的容器化数据面）。本节写**组件关系与本地实测要点**，
> 具体安装命令以你拿到的 v0.7.2 部署包为准。

### 4.1 控制面（一台机器）

- **CubeMaster**：调度/元数据/模板注册。默认端口 8089（CLI 通路），
  另起 CubeAPI（本环境 127.0.0.1:3000，由 nginx 把 3000 映射给 E2B SDK 用）。
- **Registry**（`registry:2` / 5000 端口）：存模板镜像，本地即可，
  云上可用 TKE 镜像仓库替代。
- **CubeTemplateCenter**：模板构建服务（systemd：cube-sandbox-cube-templatecenter），
  CubeMaster 把构建任务转发给它；它不可用则模板 build 直接失败。

### 4.2 数据面（同机则与控制面同宿）

- **Cubelet**（`cubelet --config /usr/local/services/cubetoolbox/Cubelet/config/config.toml`）：
  每台宿主一个。配置文件关键项：
  - 数据目录（XFS/ext4 均可；本地 /data/cubelet 15G 空间）
  - 上报控制面地址
  - 资源池配置（VM 配额/CPU 份额）
- **cube-proxy / cube-lifecycle-manager / cube-egress**：容器，跟随 cubelet 宿主。

### 4.3 平台自检（部署完必做）

```bash
# ① 平台 CLI 能看到宿主在册：
cubemastercli list
# ② API 层建沙箱（E2B SDK 兼容层，本地 3000，必填字段 templateID）：
#    curl -X POST http://127.0.0.1:3000/sandboxes \
#      -H "Content-Type: application/json" -d '{"templateID": "tpl-xxx"}'
# ③ 模板 READY（见下节）
```

---

## 5. 模板构建（镜像 → 可调度模板）

本地实测流程（产物见 `cubemastercli template list` 的 READY 状态）：

1. **写镜像**：基于公开 python 镜像，装 pip/curl 等 agent 常用工具；
   多档变体（128M / 256M / 1core / denyall / nonet）由同一镜像的不同
   **模板资源配置**产生，不用重复做镜像。
2. **推到本地 registry**：`docker tag cube-lite-py:1.0 127.0.0.1:5000/cube-lite-py:1.0 && docker push`。
3. **发起构建**：CubeMaster 将"镜像 → 模板"任务转发给 TemplateCenter，
   完成后 `cubemastercli template list` 可见 READY 模板 id（如
   `tpl-d71ab23c3f18467ca80fb490`），IMAGE_INFO 为 registry 镜像 sha。
4. **网络变体**：模板级 `denyall`/`nonet` 在**沙箱内**仍可被网络请求绕过
   （实测模板级关闭无效）—— 真正的出网控制必须做在宿主层（cube-egress
   网关白名单），设计时按"模板管配额、宿主管网络"分层。

### 5.1 pi-sandbox（企业场景 A+B 镜像）落地记录

`deploy/sandbox/Dockerfile` 已产出 `pi-sandbox:1.0` → 模板
`tpl-2492096525f04f0aac655acb`（alias `pi-sandbox-ab`，cpu=1000m/mem=256Mi/
writable-layer 1Gi）。构建踩坑（每条都是实测血泪）：

1. **沙箱镜像契约缺一不可**：CubeMaster 容器需要 `/usr/bin/envd`（探针守护，
   版本 0.5.13，静态 Go 二进制）+ `cube-entrypoint.sh` 作 ENTRYPOINT（后台拉起
   envd 常驻，:49983/health 探活）。缺 envd → `Exec mount failed`；ENTRYPOINT
   是 `CMD ["python3"]` → 容器主进程读完 stdin 即退 → `mount namespace` 失败。
2. **base 必须用 `alpine:3.20`，不能是官方 `python:3.12-alpine3.20`**：后者
   实测 `reset guest time failed: BrokenPipe`。改用 `alpine:3.20` + apk
   `python3 py3-pip`（3.12.13）即正常 —— CubeSandbox guest 对 base 敏感。
3. **GNU 全集工具必装**（coreutils/findutils/grep/sed/gawk）：busybox 阉割版
   撑不起模型生成的 `grep -P` / `find -exec` / `sed -i`（cube-lite-py 已踩）。
4. **构建源用腾讯云内网 mirror**（apk `mirrors.tencentyun.com/alpine` + pip
   `mirrors.tencentyun.com/pypi/simple`），否则 Dockerfile 内直连官方源极慢。
5. **find 首启超时**：A+B 镜像 ~400MB → ext4 rootfs 大，模板 READY 后第一个
   沙箱冷启动可能 502（openresty 探活超时）；后续实例化（rootfs 已缓存）稳定。
   `create-from-image` 完成即 READY，但首个沙箱建议预热后再生任务。

---

## 6. pi-py 服务部署（systemd 常驻）

```bash
# ① Python 3.12 venv（不用系统 python，避免污染）
sudo python3.12 -m venv /opt/pi-venv
sudo -E /opt/pi-venv/bin/pip install -e /path/to/pi-py

# ② 环境变量（模板，按需改；★ 为必改安全项）
cat > /etc/pi.env <<'EOF'
PI_DB_DSN=mysql+pymysql://root:CHANGE_ME@127.0.0.1:3306/pi_py
PI_REDIS_URL=redis://:CHANGE_ME@127.0.0.1:6379/15
PI_MILVUS_URI=http://127.0.0.1:19530          # 可选；不配则记忆降级
PI_EMBEDDING_MODEL=text-embedding-3-small      # 可选；配 Milvus 时必填
PI_MODEL=openai/qwen3-32b                      # 你的模型路由
PI_SANDBOX=cubesandbox                          # 沙箱路由：cubesandbox / docker / local
PI_SANDBOX_TEMPLATE=tpl-xxx                    # 模板 id（§5 构建产物）
PI_SANDBOX_NET=host                            # ★ 沙箱出网开关（dev 版显式化）：默认关=断网
                                               #   最安全；web 抓取类任务才设 host 开外网
PI_CUBE_API_URL=http://127.0.0.1:3000          # E2B 兼容层
PI_CUBE_API_KEY=e2b_000000                     # 本环境固定值；生产换强随机
PI_CUBE_DOMAIN=cube.app                        # 沙箱数据面域名后缀
PI_SSL_CERT_FILE=/etc/ssl/certs/ca-certificates-meye.crt  # ★ 平台 CA（自签则合并）
PI_POLICY=/etc/pi/policy.yaml                  # 命令/文件策略（安全分层之一）
PI_ARCHIVE=1                                   # 工作区归档默认开
PI_ARCHIVE_S3_ENDPOINT=http://127.0.0.1:9000   # 可选：MinIO 归档
PI_ARCHIVE_S3_BUCKET=pi-archives
PI_ARCHIVE_S3_ACCESS_KEY=minioadmin
PI_ARCHIVE_S3_SECRET_KEY=CHANGE_ME
PI_METRICS_TOKEN=CHANGE_ME                     # /metrics 拉取 token
EOF

# ③ systemd 单元 /etc/systemd/system/pi.service
[Unit]
Description=pi-py agent service
After=network.target docker.service

[Service]
EnvironmentFile=/etc/pi.env
ExecStart=/opt/pi-venv/bin/python -m pi.cli serve --host 127.0.0.1 --port 8300
Restart=always
# ★ 8300 只绑本机 —— 前面必须再放一层反向代理 + 认证

[Install]
WantedBy=multi-user.target

sudo systemctl daemon-reload && sudo systemctl enable --now pi
```

**安全要点**（本地事故的教训）：
- `8300` 有 **开放注册**（`/v1/auth/register` 无鉴权）→ 只绑 127.0.0.1，
  公网入口一律走反代（Caddy 等）并自行加认证层。
- `PI_CUBE_API_KEY` 换强随机（e2b_000000 是占位）。
- CA 合并文件是"平台自签 CA + 系统 CA"拼接产物 —— 云端用平台正式 CA 即可。

---

## 7. 配额与并发调参（实测值，直接用）

| 参数 | 本地实测 | 8 核 64GB 建议 | 说明 |
|---|---|---|---|
| `PI_MAX_CONCURRENT_RUNS` | 8 | 8-16 | 服务端并发上限；**必须 ≤ 平台能同时养的 VM 数** |
| 沙箱模板配额 | 128M/256M/1核 | 256M-1G/1核 | 256M 跑 Python agent 任务实测 OK；吃内存任务用 1G |
| 平台并发安全值 | 6-8（1核机器）| 12-24 | 受 CPU 限制而非内存；用 `pi_sandbox_create_duration` 观察创建延迟 |
| 命令超时 | 120s 默认/600s 上限 | 不变 | 沙箱内 GNU timeout 包装，超时零残留 |
| turn 总超时 | 600s（PI_RUN_TIMEOUT_SECONDS）| 不变 | |
| close 总超时 | 90s（PI_SANDBOX_CLOSE_TIMEOUT_SECONDS）| 不变 | 超时后清理线程继续收尾，VM 必死 |
| 平台 VM 空闲回收 | 600s | 不变 | 最后一道防泄漏防线 |

**错误码速记**：平台返回 `error code 130597: no more resource` =
平台配额耗尽 —— 先 `cubemastercli list`（或 API `GET /sandboxes`）看堆积 VM，
kill 泄漏源再继续；不要盲目重试。

---

## 8. 生产化清单（部署完逐项打勾）

- [ ] `ls /dev/kvm` → 在用（嵌套虚拟化开启）—— 没有它沙箱全挂
- [ ] 8300 只绑本机；公网仅 80/443 反代；`/metrics` 带 token
- [ ] pi-py 登录/注册走强密码策略；`PASSWORD` 全部换强随机
- [ ] `/metrics` 拉取验证：`pi_sandbox_create_failures_total` 等 4 系列在（沙箱健康）
- [ ] 工作区归档目录可写；配置 MinIO 后归档 json 的 `s3` 字段非 null
- [ ] 平台模板 READY；API 建沙箱 + 销毁闭环（防泄漏防火墙）
- [ ] 监控：Prometheus 抓 pi-py `/metrics` + 平台指标；告警盯
      `sandbox_create_failures` / `sandbox_close_failures` / `trace_failures`
- [ ] 备份：MySQL 每日 dump；归档 tar 同步到异机/对象存储
- [ ] 出网控制：宿主层 egress 白名单（模板级无效，见 §5.4）
- [ ] 换机后可复现：本手册 + `docs/cube-sandbox-design-notes.md` + pi-py 源码
      三件套在手，任何一台 Ubuntu 24.04 + KVM 的机器 30 分钟内还原

---

## 9. 避坑清单（本地全部踩过 → 云端规避）

| 坑 | 现象 | 规避 |
|---|---|---|
| 无 /dev/kvm | 建沙箱报 virtualization 错误 | 买前确认嵌套虚拟化，`kvm-ok` 先测 |
| VM 泄漏 | 跑几次任务后 `no more resource` 500 | 服务端 finally close + close 超时 + 平台回收 三重防线（已内置）|
| close kill 静默失败 | VM 悄悄堆积 | kill 失败打 warning + `pi_sandbox_close_failures_total` 告警 |
| 平台 CLI 视图陈旧 | `cubemastercli list` 显示已删 VM | 排查以 API（3000）/服务日志为准 |
| 模板级断网无效 | denyall 模板仍能出网 | 出网控制做宿主 egress 层 |
| 工作区 >10MB | 装载报 413 HTML | 已内置 10MB 上限 + 清晰报错；或清理工作区大文件 |
| 命令超时语义乱 | timed_out 恒 False | 已内置沙箱内 GNU timeout 包装（退出码 124）|
| 后台进程不重定向 | 命令挂到连接 deadline | 提示模型 `cmd >/dev/null 2>&1 &` 写法 |
| 8300 暴露公网 | 任何人可注册 | 只绑本机 + 反代认证 |
| static 并发开太大 | 排队任务全撞配额 | `PI_MAX_CONCURRENT_RUNS` ≤ 平台 VM 上限 |
| Milvus 端口撞 MinIO | 19530/2379/9000 冲突 | 部署时核对端口，MinIO 9000 尤其容易撞 |

---

## 10. 从本机迁移到云（动作清单）

1. 云机装 Ubuntu 24.04 → 开嵌套虚拟化 → `kvm-ok` 过。
2. Docker + 镜像加速 → 部署 §3 中间件（MySQL/Redis/MinIO/Milvus）。
3. 部署 CubeSandbox 平台（§4）→ 推模板镜像 → 构建模板（§5）→ READY。
4. 拷 pi-py 源码 → venv 安装 → 写 `/etc/pi.env`（§6）→ systemd 起服务。
5. 跑 §8 清单逐项打勾（尤其 /metrics 4 个沙箱系列、归档闭环、备份）。
6. 回归：跑一遍企业 eval（5 任务）确认端到端；故障注入探针 /tmp/prod_probe.py
   确认超时/假死/413 三个故障路径行为与本地一致。
7. 旧机收尾：保留 30 天归档与审计日志后下线。