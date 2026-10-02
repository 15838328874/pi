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

## 🔢 三个反直觉的关键实测数字（先看，能省你几天）

这三个都是**实测**出来的，且**与直觉相反**。完整数据与复现方法见 **§10**。

### ① 空载密度：**一台 8 核 / 61GB 的普通云主机，实测扛住 1000 个沙箱**

| 放开的配额闸门 | 实测上限 | 卡在哪 |
|---|---|---|
| 全默认配置 | **16** | `quota_cpu 16000 ÷ 模板 1000m` |
| 只放开 CPU 配额 | **197** | `quota_mem_mb ÷ 400MB/沙箱` |
| CPU + 内存 + 网卡池都放开 | **1000** | `max_mvm_num`（= `mem_limit ÷ 512MB`） |
| 继续放大 `mem_limit` | ≈ **3800** | 物理内存 `58GB ÷ 15.2MB` |

1000 个存活时**只吃了 15.2 GB**（单 VM 均摊 **15.2 MB**），机器还剩 42 GB。

> **结论："只能跑十几个"是配额造成的，不是硬件不够。** 官方说的"单机数千"是同一口径（空载 CoW 密度）。

### ② 但「能持有 1000 个」≠「能同时干 1000 个活」

沙箱 90% 时间在等 LLM 返回，空载几乎不吃 CPU（160 个存活时宿主 `load` 仅 **0.3**）。
**真跑 pandas 这类计算时，物理核数才是硬顶** —— 8 核机器实际只能并行干 **8~16** 个。

> 所以 `PI_MAX_CONCURRENT_RUNS` 要对齐**物理核数**，不是配额算出来的密度（§10.6）。

### ③ `cpu=100m` 不是"调度权重"，是**硬 cgroup 节流**

| 模板 `--cpu` | guest `cpu.max` | 实际算力 | 实测耗时 |
|---|---|---|---|
| **1000m** | `100000 100000` | 单核 100% | 269 ms |
| 100m | `10000 100000` | 单核 **10%** | **2699 ms（慢 10 倍）** |

更糟：10% 算力下**沙箱创建后的第一条命令会 502**（envd 冷启动都撑不住）。

> **生产模板 CPU 不要低于 500m**（§10.3）。

### ④ 「能扛 1000 个沙箱」≠「能服务 1000 个用户」

用户容量按**吞吐**算。实测本机峰值 **~100 回合/分钟**，且**用户多了不会失败、只会排队**
（24 并发用户实测 24/24 成功）：

| 使用强度 | 每用户 | 可服务 |
|---|---|---|
| 轻度（5 分钟 1 回合） | 0.2 回合/min | **~500 人** |
| 中度（1 分钟 1 回合） | 1 回合/min | **~100 人** |
| 重度（4 回合/min） | 4 回合/min | **~25 人** |

> **注册用户总数无上限**（不活跃用户成本为零）。估服务器要用「**峰值同时活跃数**」，
> 不是「注册了多少人」（§10.9）。

---

> ### 路径约定（全文通用，先看这条再抄命令）
>
> | 写法 | 含义 | 你要做什么 |
> |---|---|---|
> | `/path/to/pi-py` | **本仓库克隆到你机器上的位置** | 换成你的实际路径，例如 `/opt/pi-py`、`~/pi-py` |
> | `/opt/pi-venv` | 示例 venv 路径 | 可换任意位置，但**必须与 systemd 单元里的 `ExecStart=` 一致**（§7），否则服务起不来 |
> | `/etc/pi.env` | pi-py 的环境变量文件 | 由 systemd `EnvironmentFile=` 读取（§7 全表） |
> | `/usr/local/services/cubetoolbox/...`、`/data/cubelet` | CubeSandbox 平台自身路径 | **由官方安装包决定，不要手改** |
>
> 建议开工前先定好变量，后面命令直接复用，避免手抄绝对路径出错：
>
> ```bash
> export PI_SRC=/opt/pi-py       # 本仓库位置
> export PI_VENV=/opt/pi-venv    # venv 位置
> ```

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

### 0.1 第二台验证环境（本文档 §10 的密度数据出处）

本文档除作者那台 3.6G 小机外，全部内容**已在一台不同云厂商的机器上完整复现**，
§10 的密度/规格实测数据即来自这台：

| 项 | 值 | 说明 |
|---|---|---|
| 规格 | **8 vCPU / 61 GiB / 200 GiB** | **火山引擎** Ubuntu 24.04，非腾讯云 |
| 虚拟化 | **无 `/dev/kvm`** → 走 PVM 内核（§3.2） | 验证了"没有嵌套虚拟化也能部署" |
| CPU 拓扑 | 4 核 × 2 线程 = 8 逻辑核 | 平台自动探测出 `quota_cpu=16000` 毫核 |
| 网段 | eth0 = `192.168.0.151/24` | **与默认 CIDR `192.168.0.0/18` 冲突** → 见 §12 |
| 磁盘 | 单块盘全给 ext4 根分区，无数据盘 | **`/data/cubelet` 需 XFS** → 见 §12 |
| 镜像源 | `mirrors.tencentyun.com` **不可达** | Dockerfile 需 `--build-arg` 换源 → 见 §6.2 |

**换一台机器 + 换一个云厂商，本文档照做即通**；两台机器的差异全部记录在 §12 避坑清单里。

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

## 3. 虚拟化能力：有 KVM 直接用；没有就装官方 PVM 内核

> 结论先行：**最终一定要有 `/dev/kvm`，但这不等于必须买一台"支持嵌套虚拟化"的机器** ——
> 官方提供 **PVM 宿主内核**，装完就在原本没有 KVM 的普通云主机上提供了 `/dev/kvm`。
> 没有 KVM 的机器走 §3.2。

### 3.0 部署前环境自检（30 秒，提前暴露 §12 里的大半坑）

```bash
echo "架构: $(uname -m)  内核: $(uname -r)"
echo "内存: $(free -g | awk 'NR==2{print $2}') GB   根盘可用: $(df -h / | awk 'NR==2{print $4}')"
echo "--- KVM ---"
[ -e /dev/kvm ] && echo "  ✅ 有 /dev/kvm → §3.1" || echo "  ⚠️  无 /dev/kvm → §3.2 装 PVM 内核"
grep -qE 'vmx|svm' /proc/cpuinfo && echo "  CPU 有硬件虚拟化标志" || echo "  CPU 无 vmx/svm（PVM 不需要）"
echo "--- /data/cubelet（必须 XFS）---"
df -T /data/cubelet 2>/dev/null | awk 'NR==2{print "  文件系统: "$2}' || echo "  未挂载 → 需准备 XFS，见 §12"
echo "--- 网段（192.168.x 会和默认 CIDR 冲突）---"
ip -4 -br addr | awk '$1!="lo"{print "  "$1" "$3}'
grep nameserver /etc/resolv.conf | sed 's/^/  DNS /'
echo "--- 引导 ---"
grep -qE '^GRUB_DISABLE_SUBMENU="?true' /etc/default/grub && echo "  ⚠️  GRUB_DISABLE_SUBMENU=true → §3.2② 需特殊处理" || echo "  GRUB 子菜单正常"
echo "--- 拉镜像可达性 ---"
curl -sS -o /dev/null -w "  docker.io      -> %{http_code}\n" --max-time 10 https://registry-1.docker.io/v2/ 2>/dev/null || echo "  ⚠️  docker.io 不可达 → §12 配镜像源"
curl -sS -o /dev/null -w "  镜像加速源     -> %{http_code}\n" --max-time 10 https://docker.m.daocloud.io/v2/ 2>/dev/null
```

### 3.1 有 KVM（标准路径，优先选）

```bash
ls -l /dev/kvm                      # 必须有这个设备
sudo apt install -y qemu-kvm && sudo kvm-ok   # 提示 "KVM acceleration can be used" 才过
```

云厂商注意：部分机型默认关闭嵌套虚拟化，需在控制台或工单开启（腾讯云大部分标准型
默认支持；阿里云要工单申请）。**买机器前先确认 KVM 最省事。**

### 3.2 没有 KVM：装 PVM 宿主内核（官方路径，无需嵌套虚拟化）

**怎么判断属于这一档**：`ls /dev/kvm` 不存在，且

```bash
grep -oE 'vmx|svm' /proc/cpuinfo | sort -u     # 无输出 → CPU 没暴露硬件虚拟化
systemd-detect-virt                            # 输出 kvm → 这台自己就是虚拟机
modprobe kvm_intel || modprobe kvm_amd         # 报 Operation not supported
```

说明宿主没开嵌套虚拟化。**PVM（Pagetable-based VM）不依赖宿主暴露 VT-x/AMD-V**，
在 guest 内核层用影子页表实现，对宿主 hypervisor 完全透明 —— 这正是官方为普通云服务器
准备的路径（详见 CubeSandbox 官方 `docs/zh/guide/pvm-deploy.md`）。

```bash
# ① 下载 PVM 宿主内核主包
#    Releases 页 https://cnb.cool/CubeSandbox/CubeSandbox/-/releases 过滤 kernel-release
dpkg -i linux-image-*opencloudos9.cubesandbox.pvm.host*_amd64.deb    # DEB 系
rpm -ivh --oldpackage kernel-*opencloudos9.cubesandbox.pvm.host*.rpm # RPM 系

# ② 设为默认启动项
#    ★ 不要照抄官方 PVM 指南的 GRUB_DEFAULT="Advanced options for Ubuntu>..." 写法：
#      若 /etc/default/grub 里有 GRUB_DISABLE_SUBMENU="true"（部分云厂商镜像默认如此），
#      就没有 "Advanced options" 子菜单，该写法会【静默失效】——
#      重启后仍进旧内核、PVM 不生效，且全程没有任何报错。
#    稳妥做法：直接从 grub.cfg 取顶层标题，保证逐字节匹配
TITLE=$(grep -oE "^menuentry '[^']*opencloudos9[^']*'" /boot/grub/grub.cfg | head -1 \
        | sed "s/^menuentry '//; s/'\$//")
sed -i "s|^GRUB_DEFAULT=.*|GRUB_DEFAULT=\"$TITLE\"|" /etc/default/grub
update-grub
grep -qF "menuentry '$TITLE'" /boot/grub/grub.cfg && echo "✅ 启动项匹配" || echo "❌ 不匹配，别重启！"

# ③ 写入 PVM 所需内核参数并重启
curl -sL https://cnb.cool/CubeSandbox/CubeSandbox/-/git/raw/master/deploy/pvm/grub/host_grub_config.sh | bash
reboot

# ④ 重启后验证（四条都要过）
uname -r | grep -q cubesandbox.pvm.host && echo "✅ PVM 内核已生效"
modprobe kvm_pvm && lsmod | grep kvm_pvm
ls -la /dev/kvm
echo 'kvm_pvm' > /etc/modules-load.d/kvm-pvm.conf      # 开机自动加载
```

> **重启前务必确认能通过云控制台 VNC/救援模式登录**，以防新内核起不来。
>
> 安装 CubeSandbox 时记得带 **`CUBE_PVM_ENABLE=1`**（见 §5），否则装的是普通 guest 内核，
> PVM 不生效。安装日志里应出现
> `[one-click] CUBE_PVM_ENABLE=1, selected PVM guest kernel: ... vmlinux -> vmlinux-pvm`。

**已实测环境**（供对照）：火山引擎 8C/61G Ubuntu 24.04，宿主无 `vmx`/`svm`、无 `/dev/kvm`；
装 `kernel-release-260921-1`（`6.6.69-...pvm.host`）后 `/dev/kvm` 出现，平台与沙箱全部跑通。

**回滚**（新内核起不来时，从云控制台 VNC/救援模式进去）：

```bash
cp /root/grub.backup.* /etc/default/grub && update-grub && reboot   # 改回旧内核（修改前先备份！）
dpkg -r linux-image-6.6.69-opencloudos9.cubesandbox.pvm.host-*     # 卸载 PVM 内核
```

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

> **启用条件（代码写死，`src/pi/server/config.py::vector_memory_enabled`）**：
> 下面**四项必须全部非空**，缺一个就退回 MySQL 词法检索（`lexical_fallback`）：

```python
all((embedding_url, embedding_api_key, embedding_model, milvus_uri))
```

| 变量 | 值 |
|---|---|
| `PI_MILVUS_URI` | `http://127.0.0.1:19530` |
| `PI_EMBEDDING_URL` | `https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding` |
| `PI_EMBEDDING_API_KEY` | 阿里云百炼 key |
| `PI_EMBEDDING_MODEL` | `text-embedding-v3`（1024 维） |

**嵌入接口是 DashScope 原生格式**（`{"input":{"texts":[...]}}`，不是 OpenAI 的 `{"input":"..."}`）。
pi-py 只用 **embedding**，**没有 rerank 功能**（`grep -r rerank src/` 无结果），别被误导去配 rerank。

**部署（docker-compose，端口必须避开平台已有服务）：**

| Milvus 默认端口 | 冲突方 | 改用宿主端口 |
|---|---|---|
| MinIO `9000/9001` | 平台 `cube-sandbox-minio` | `19100/19101` |
| metrics `9091` | 平台 egress admin | `9092` |
| gRPC `19530` | 空闲 | 不变 |

```bash
# 官方 compose（改端口后）
curl -sSL -o milvus-standalone-docker-compose.yml \
  https://github.com/milvus-io/milvus/releases/download/v2.4.15/milvus-standalone-docker-compose.yml
# 改三处宿主端口：9001->19101、9000->19100、9091->9092（容器内部端口不动）

# 镜像源（daocloud 挡 minio:latest 但放行旧 tag；milvus 走 1panel）
docker pull quay.io/coreos/etcd:v3.5.5
docker pull docker.m.daocloud.io/minio/minio:RELEASE.2023-03-20T20-16-18Z
docker tag  docker.m.daocloud.io/minio/minio:RELEASE.2023-03-20T20-16-18Z minio/minio:RELEASE.2023-03-20T20-16-18Z
docker pull docker.1panel.live/milvusdb/milvus:v2.4.15
docker tag  docker.1panel.live/milvusdb/milvus:v2.4.15 milvusdb/milvus:v2.4.15

docker-compose up -d   # 注意是 docker-compose（v1 连字符）；本机无 `docker compose` 子命令
```

> ⚠️ **两个 compose 相关的坑**：
> 1. **官方 compose 没有 `restart: always`** —— 三个服务默认重启策略是 `no`，**主机重启后不会自启**，
>    届时 pi-py 会静默降级回词法检索（不报错）。上线前给 etcd/minio/standalone 各加一行 `restart: always`。
> 2. **docker-compose v1 补 restart 后 `up -d` 会炸**：报 `KeyError: 'ContainerConfig'`，且会把旧容器
>    改名成 `<前缀>_milvus-xxx` 后退出。原因是它想 reconcile 旧容器（创建时没 restart 策略）却读不到镜像配置。
>    正确姿势是**先 `docker rm -f` 掉旧容器，再 `up -d`**（数据在 bind mount 卷里，删容器不丢数据）。

**自检（四步都要过）：**

```bash
curl -s http://127.0.0.1:9092/healthz                 # 200
# 建 collection + query 往返
/opt/pi-venv/bin/python3 -c "from pymilvus import MilvusClient; c=MilvusClient(uri='http://127.0.0.1:19530'); print(c.list_collections())"
# 配好四项后重启，readyz 应多出 milvus:ok
curl -s http://127.0.0.1:8300/readyz                   # {"db":"ok","cache":"ok","milvus":"ok"}
# 跑两回合对话后看指标，出现 vector_hit 即向量检索生效
curl -s -H "Authorization: Bearer $PI_METRICS_TOKEN" http://127.0.0.1:8300/metrics | grep pi_memory_retrievals_total
```

> ⚠️ **`embed_failed` 计数在涨、且 `pi_memories` 集合一直不出现** = 嵌入调用失败。
> 首查 `/etc/pi.env` 里 key 是否真的写进去了（写 env 时注意别把 `{KEY}` 当字面量存进去——
> 实测踩过：存成 5 个字符的 `{KEY}`，日志报 `embedding endpoint returned HTTP 401`，但同一个 key
> 手动 curl 是 200，一眼看不出来）。集合只在**第一次嵌入成功**时才创建（维度取自首个向量）。

内存 ~3GB（etcd + minio + milvus 三容器）。生产要语义记忆才需要；不装时自动降级。

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

### 6.2 构建（镜像源按云厂商选，选错会慢几十倍）

Dockerfile 的 apk / pip 源通过 `--build-arg` 传入，**默认值指向腾讯云内网**
（作者环境）。换云厂商必须覆盖，否则 `mirrors.tencentyun.com` 不可达或极慢。

```bash
cd /path/to/pi-py

# 腾讯云（默认值，可省略全部 --build-arg）
docker build -t pi-sandbox:1.0 -f deploy/sandbox/Dockerfile .

# 其他云（示例：火山云/阿里云）—— apk 用官方源、pip 用清华源
docker build -t pi-sandbox:1.0 -f deploy/sandbox/Dockerfile \
  --build-arg APK_MIRROR_HOST=dl-cdn.alpinelinux.org \
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
  --build-arg PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn \
  .

# 构建过程会打印 "== 自检全部通过 =="，任何库/命令缺失都会 fail —— 这是有意的
```

| build-arg | 默认（腾讯云） | 说明 |
|---|---|---|
| `APK_MIRROR_HOST` | `mirrors.tencentyun.com` | 替换 alpine 的 `dl-cdn.alpinelinux.org` |
| `PIP_INDEX_URL` | `http://mirrors.tencentyun.com/pypi/simple` | pip 索引 |
| `PIP_TRUSTED_HOST` | `mirrors.tencentyun.com` | pip 信任主机 |

> **实测速度差**（火山云，下 alpine APKINDEX）：`mirrors.aliyun.com` 0.16s ·
> 清华 1.4s · 官方 `dl-cdn` 5.7s。apk 阶段用慢源会让 81 个包的安装拖到十几分钟。
> 非腾讯云环境建议 `APK_MIRROR_HOST=mirrors.aliyun.com`。

### 6.3 推 registry

> ⚠️ 先确认 `127.0.0.1:5000` 有 registry 在跑。**一键安装不一定带 registry**
> （实测平台 13 个服务里没有它），没有就先起一个：
>
> ```bash
> ss -ltn | grep :5000 || docker run -d --name cube-registry --restart always \
>   -p 0.0.0.0:5000:5000 registry:2
> curl -s http://127.0.0.1:5000/v2/_catalog      # 期望 {"repositories":[]}
> ```

```bash
docker tag  pi-sandbox:1.0 127.0.0.1:5000/pi-sandbox:1.0
docker push 127.0.0.1:5000/pi-sandbox:1.0

# 确认真的进去了（§6.4 会用这个镜像）
curl -s http://127.0.0.1:5000/v2/pi-sandbox/tags/list   # 期望 {"name":"pi-sandbox","tags":["1.0"]}
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

# ★ 必须补装 greenlet：pyproject 只声明了 sqlalchemy>=2.0，而 SQLAlchemy 2.1 的
#   asyncio 扩展【硬性要求】greenlet。缺了服务直接起不来：
#     ImportError: The SQLAlchemy asyncio module requires that the Python 'greenlet' library is installed
sudo -E /opt/pi-venv/bin/pip install greenlet
```

> pi-py 的 `pyproject.toml` 未包含 **e2b 系 SDK**，必须按 §1 的版本单独装
> （`e2b==2.26.0` + `e2b-code-interpreter==2.8.1`），否则 `PI_SANDBOX=cubesandbox` 起不来。

下面这张表是**全部环境变量**（含默认值），★=上线必改。写进 `/etc/pi.env`，
systemd `EnvironmentFile` 引用。

### 7.1 核心（★ 必改）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PI_DATABASE_URL` | 无（不设直接起不来） | `mysql+aiomysql://USER:PASS@127.0.0.1:3306/pi_py` ★ |
| `PI_JWT_SECRET` | 空=自动生成并落盘 | 生产显式设强随机 ★ |
| `PI_MODEL` | `openai/gpt-4o` | 你的模型路由（如 `openai/qwen3.8-flash`）★ |
| `PI_MODEL_LIST` | 空=仅默认 | 前端模型选择器可选项，逗号分隔（如 `openai/qwen3.8-flash,openai/deepseek-v4-pro`）；只决定下拉清单，不改默认 |
| `PI_RUN_TIMEOUT_SECONDS` | `600` | 单次 run 超时（秒）；写游戏/搭项目等大任务建议 `1800` |
| `OPENAI_BASE_URL` | — | 模型网关 base_url ★ |
| `OPENAI_API_KEY` | — | 模型网关 key ★ |
| `PI_SANDBOX` | 空(本地模式) | `cubesandbox`（本手册场景） |
| `PI_SANDBOX_TEMPLATE` | 空 | §6 建出的 `tpl-xxx` ★ |
| `PI_CUBE_API_URL` | `http://127.0.0.1:3000` | CubeAPI 地址 |
| `PI_CUBE_API_KEY` | `e2b_000000` | 换强随机 ★ |
| `PI_CUBE_DOMAIN` | `cube.app` | 沙箱数据面域名后缀 |
| `PI_SANDBOX_CA_FILE` | `/root/.local/share/mkcert/rootCA.pem` | 平台自签 CA **+ 系统 CA 合并**文件；服务非 root 运行时必须改到可读路径（§12）★ |
| `SSL_CERT_FILE` | — | 同上内容的另一入口（**httpx 标准变量**）。由 `PI_SANDBOX_CA_FILE` 自动 setdefault ★ |

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
| `PI_ARCHIVE_S3_ENDPOINT` | 空 | **工作区归档**上传目标（不设则只落本地，§11 的 `s3` 字段会是 null）★ |
| `PI_ARCHIVE_S3_BUCKET` | 空 | 归档 bucket，如 `pi-archives`（**不会自动创建，需先建**） |
| `PI_ARCHIVE_S3_ACCESS_KEY` / `PI_ARCHIVE_S3_SECRET_KEY` | 空 | 归档 MinIO 凭据 |

> ⚠️ **`PI_ARCHIVE_S3_*` 与文件管线的 `PI_S3_*` 是两套独立变量**，别只配一套。
> 前者管"回合结束把 workspace 打包归档到对象存储"（`src/pi/server/archive.py`），
> 后者管"用户上传文件/产物"。只配 `PI_S3_*` 时归档仍只落本地，
> §11 清单里的「归档 json 的 `s3` 字段 non-null」永远打不上勾，且**不报错**（静默跳过）。
> 归档只在该回合**真用过沙箱**（有 baseline）时触发，纯聊天回合不产生归档。

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

> ⚠️ **先读 §10**：模板 CPU/内存怎么定、并发上限由什么决定、为什么"密度 ≠ 吞吐"，
> 都在 §10。本节只讲"换了大机器要改哪几个数"。

部署方式与 3.6G 小机**完全相同**，按新内存放大下面几个值即可：

| 参数 | 3.6G 本机值 | 8C/64G 建议 | 16C/128G 建议 | 依据 |
|---|---|---|---|---|
| `PI_SANDBOX_POOL_SIZE` | 4 | 8-12 | 12-24 | 池 = 常驻 VM 数；每 VM ≈ 256M×1.2 |
| `PI_SANDBOX_POOL_PRESSURE_HIGH` | 1.5G | 16G | 32G | 可用内存低于此进"紧张档"（回收加速） |
| `PI_SANDBOX_POOL_PRESSURE_LOW` | 512M | 8G | 16G | 回到"宽裕档"阈值 |
| `PI_MAX_CONCURRENT_RUNS` | 8 | **12**（别用 16） | **24**（别用 32） | 对齐**物理核数**并留余量；**等于平台配额上限 = 零余量**，第 N+1 个请求必失败（§10.6） |
| `--writable-layer-size`（模板） | 1Gi | 2Gi | 4Gi | 沙箱 workspace 磁盘；处理大文件时加大 |
| 沙箱模板 `--memory` | 256Mi | 256/512Mi | 512Mi/1Gi | Python agent 任务 256Mi 够；重型任务才加 |
| `PI_MAX_UPLOAD_BYTES` | 900M | 900M 不变 | 900M 不变 | 上限由"流式未实现"决定，与内存无关 |
| fetch_file 中转上限 | 256M（代码常量） | 可放宽到 512M+ | 可放宽 | `pi/tools/files.py::_MAX_FETCH_BYTES` |

**核心理由再强调**：沙箱并发上限由 **CPU 核数**决定（每沙箱 1 核 vCPU），内存只是
容纳"常驻服务 + 池 + 峰值 VM"的容器。所以升级顺序是：**先升核数撑并发，内存跟着
放大池/压力阈值即可，别提前把所有数字都拉满。**

---

## 10. 沙箱规格、并发与密度（为什么这样设计 + 实测数据）

> 本节回答三个后来者一定会问的问题：**模板该给多少 CPU/内存？能跑多少并发？为什么官方说"单机数千"而我这里只有十几个？**

### 10.1 一句话结论

**模板用 `1 核 / 256Mi`。并发上限 = `CPU 配额 ÷ 模板 CPU`，与内存几乎无关。**

### 10.2 并发上限的真实公式：三道闸门取最小

```bash
# ① CPU 闸门（绝大多数情况下就是它）
平台 quota_cpu(毫核) ÷ 模板 cpu(毫核)
# ② 内存闸门 —— 注意：平台按【每沙箱固定 ~400MB】计费，与模板申请多少无关（见 10.7）
平台 quota_mem_mb ÷ 400
# ③ 网卡池闸门
cubelet 配置里的 tap_init_num（默认 500）
```

查你这台的实际值：

```bash
# 平台侧配额
docker exec cube-sandbox-mysql mysql -ucube -pcube_pass cube_mvp \
  -e "SELECT quota_cpu, quota_mem_mb, max_mvm_num FROM t_cube_node_registration\G"
# 网卡池
grep tap_init_num /usr/local/services/cubetoolbox/Cubelet/config/config.toml
```

**实测验证（8 核 / 61GB 机器，`quota_cpu=16000`）**——公式与实测精确吻合：

| 模板 CPU | 公式预测 | 实测结果 |
|---|---|---|
| 1000m | 16000÷1000 = **16** | 16 成功，**17 失败** ✅ |
| 100m | 16000÷100 = 160 | 160 成功，170 失败 ✅ |

### 10.3 ★ `cpu=NNNm` 不是"调度权重"，是硬 cgroup 节流

这是**最容易误配**的地方。它不是"抢 CPU 的优先级"，而是直接换算成 guest 内的 `cpu.max`：

| 模板 `--cpu` | guest `/sys/fs/cgroup/cpu.max` | 实际算力 | 实测纯 CPU 负载(3e6 循环) |
|---|---|---|---|
| **1000m** | `100000 100000` | **单核 100%** | **269 ms** |
| 100m | `10000 100000` | 单核 **10%** | **2699 ms（慢 10 倍）** |

```bash
# 自己验证（沙箱内）
cat /sys/fs/cgroup/cpu.max     # 输出 "<quota> <period>"，quota/period 就是算力比例
```

> ⚠️ **`cpu=100m` 不只是"慢"，还会让数据面间歇性不可用**：实测 10% 算力下，**沙箱创建后的第一条命令直接 502**
> （envd 冷启动都撑不住，重试一次才通）。任何生产模板都**不要低于 500m**。

**为什么容易被误用**：调小模板 CPU 能让"并发数"这个数字变大（16→160），看起来更漂亮 ——
但那只是**配额除法**，代价是每个沙箱慢 10 倍。**别为了好看的密度数字牺牲算力。**

### 10.4 内存该给多少：实测各负载的真实峰值

A+B 镜像（`deploy/sandbox/Dockerfile`）里，各负载的 cgroup `memory.peak`（每个负载用**全新沙箱**单独测，否则高水位会累加）：

| 负载 | 峰值内存 |
|---|---|
| 空载（仅 guest OS + envd） | **3.7 MiB** |
| `import pandas + numpy` | 52.9 MiB |
| **pandas 读 20MB CSV + groupby** | **118.9 MiB** |
| openpyxl 写 5 万行 xlsx | 55.2 MiB |
| 处理 30MB 二进制 | 67.8 MiB |

**关键换算**：模板 `--memory 256` 在 guest 内实际可见 `MemTotal` 只有 **209 MiB**（有虚拟化开销）。
所以 pandas 那个 119 MiB 峰值已经吃掉了一半以上 —— **256Mi 是底线，不是宽裕值**。

### 10.5 推荐配比（及理由）

| 场景 | CPU | 内存 | 为什么 |
|---|---|---|---|
| **pi-py agent 默认** | **1000m** | **256Mi** | Python agent 任务以单线程为主，1 核=满速单核；256Mi 够跑常规 pandas/openpyxl |
| 大文件 / 重任务 | 1000m | **512Mi~1Gi** | pandas 峰值已 119MiB/209MiB，稍大的 CSV 就会 OOM |
| 通用代码沙箱（含浏览器） | 2000m | 2 GiB | 官方基准口径 |
| 只读轻查询（提密度用） | **500m（下限）** | 128~256Mi | 想多塞几个时降 CPU，但别低于 500m（见 10.3） |

**三条设计原则：**
1. **CPU 决定"单个干得多快" + "能并发几个"**（配额除法）；**内存决定"能处理多大的文件"**。
2. **CPU 别低于 500m**：100m 实测慢 10 倍且首条命令 502。
3. **内存 256Mi 是 A+B 镜像的底线**：由 pandas 峰值 119MiB 对 guest 可见 209MiB 量出来的。

### 10.6 ★ 密度 ≠ 吞吐（最重要的认知）

**"能同时持有多少空载沙箱"和"能同时干多少活"是两回事。**

- 空载沙箱**几乎不吃资源**：实测 160 个沙箱时宿主 `load` 只有 **0.3**，单 VM 真实内存仅 **16 MiB**
- 但**真跑任务时物理核数是硬顶**：8 核机器上，8~16 个沙箱同时跑 pandas 就会互相排队

平台侧也是同一逻辑：官方基准里 **1000 个沙箱 × 2 vCPU = 2000 vCPU 跑在 96 核上**（20 倍超配）。
超配对"等待 LLM 返回"的 agent 负载完全合理（沙箱 90% 时间在空转），但不能据此估算计算吞吐。

**配置建议**：`PI_MAX_CONCURRENT_RUNS` 应对齐**物理核数量级**，而不是配额算出来的密度上限。
例如 8 核机器 → `PI_MAX_CONCURRENT_RUNS=12`（留余量），而不是 16（正好等于配额上限，零余量）。

### 10.7 为什么官方说"单机数千"，而你这里只有十几个？（口径澄清）

官方 README 写「单沙箱**额外开销** <5MB，CoW + 内核共享，单机可运行**数千个**实例」。
这句话**是对的，但说的是"空载密度"，不是"并发处理能力"**。官方基准报告里写得很清楚：

| 口径 | 官方数据（96 核 / 375 GiB 裸金属，2vCPU/2GiB 规格） |
|---|---|
| 实测创建 | **1000 个**，零回滚，单 VM 均摊 **~25 MB**，1000 个仅耗 25 GiB |
| 官方自己的估算 · **空载** | 受 ~25MB 摊销主导 → **可达数千** |
| 官方自己的估算 · **满载** | `375 GiB ÷ 2 GiB ≈` **185 个** |

**为什么空载这么省**：2 GiB 规格的沙箱**空载时不预占 2 GiB**，CoW 按需分配，只在真写入时分配。

**但要注意平台调度器的"计费"和"实际占用"是两回事**（实测发现）：
- 调度器按**每沙箱固定 ~400 MB** 计费（`quota_mem_mb 78797 ÷ 197 = 400.0` 正好整除）
- 而**与模板申请多少无关**：128Mi 模板拐点 197，512Mi 模板拐点 196，几乎一样
- 真实占用却只有 ~16 MB/沙箱（197 个总共 3.2 GB，机器还剩 55 GB）

所以「能塞多少」由**配额**决定，「实际吃多少内存」由 **CoW** 决定，两者不要混为一谈。

**一台 8 核 / 61GB 普通云主机上，逐层放开配额实测出的"空载能到多少"**（模板均为 1 核）：

| 放开的闸门 | 实测上限 | 卡在哪 |
|---|---|---|
| 全默认（`mcpu_limit=0` → 16000） | **16** | `quota_cpu 16000 ÷ 模板 1000m` |
| 只放开 CPU（`mcpu_limit=2000000`） | **197** | `quota_mem_mb 78797 ÷ 400MB/沙箱` |
| CPU + 内存（`mem_limit="500Gi"`）+ `tap_init_num=1500` | **1000** | `max_mvm_num`（由 `mem_limit ÷ 512MB` 推导，500Gi→1000） |
| 继续放大 `mem_limit`（如 `1Ti`） | ≈ **3800**（理论） | 物理内存：`58GB ÷ 15.2MB` 摊销 |

关键实测数据（1000 个存活时）：

```
单 VM 均摊开销 : 15.2 MB     （vs 官方 BMI5 的 ~25MB，同量级）
1000 个共耗    : 15.2 GB     （机器还有 42 GB 可用）
创建耗时       : 8.6s / 100 个（快照恢复）
销毁耗时       : 25.6s / 1000 个
```

> **三个闸门是"串行"的：放开一个就露出下一个。** 这也是为什么"我放开配额后还是没到几千" ——
> 每层都得放。而 `max_mvm_num` 是**由 `mem_limit` 推导**的（`mem_limit ÷ 512MB`），
> 所以调 `mem_limit` 时它会被自动重算，不用单独改。

### 10.8 真要提密度怎么做（以及代价）

> **三层闸门要一起放**（§10.7 的实测表），只放一层会在下一层卡住。

```bash
# ① 抬高 CPU 配额 + 内存配额（cubelet 动态配置，0/"" = 自动探测）
vi /usr/local/services/cubetoolbox/Cubelet/dynamicconf/conf.yaml
#   host:
#     quota:
#       mcpu_limit: 2000000    # 毫核；原 0 = 自动探测出 16000
#       mem_limit: "500Gi"     # k8s 风格数量串；原 "" = 自动探测出 78797(MB)
#                              # ★ max_mvm_num 会按 mem_limit÷512MB 自动重算（500Gi→1000）
systemctl restart cube-sandbox-cubelet.service

# ② 提高网卡池上限（默认 500；目标密度必须 ≤ 这个值，否则建不下去）
vi /usr/local/services/cubetoolbox/Cubelet/config/config.toml
#   [plugins."io.cubelet.internal.v1.network"]
#     tap_init_num = 1500
systemctl restart cube-sandbox-cubelet.service
#   ⚠️ tap 池【只能增不能减】：把它改回 500 并重启后，已预建的 tap 设备不会消失
#      （实测等过两个 reconcile 周期仍是 1500 个，cubelet 继续持有约 6000 个句柄）。
#      无害——只是多占一点 slab 和 fd；若确实要回收需整机重启或手工 ip link del。
#      所以压测时按需调，别为了"试试"随手调到几千。

# ③ 验证配额已生效（三项都要看）
docker exec cube-sandbox-mysql mysql -ucube -pcube_pass cube_mvp \
  -e "SELECT quota_cpu, quota_mem_mb, max_mvm_num FROM t_cube_node_registration\G"
```

**代价与注意**：
- 抬高 `mcpu_limit` 会让调度器允许**远超物理核数**的 CPU 超配（例如 8 核机器上允许 250 倍）。
  空载没事，**一旦多个沙箱同时算东西就会互相拖垮**。生产上要配合 `PI_MAX_CONCURRENT_RUNS` 兜住。
- **测完务必还原**，三个都要还：`mcpu_limit: 0`、`mem_limit: ""`、`tap_init_num: 500`。
  验证还原是否干净：`diff` 一下改动前的备份，或核对 `quota_cpu` 是否回到自动探测值（8 核机器 → 16000）。
- 改完配置要 `systemctl restart cube-sandbox-cubelet`，重启后**约 15-30 秒**节点才重新注册，
  期间 `cubemastercli list` 会显示 `NODES_SCANNED 0/0`，**这是正常的，别当成故障**。
- **压测会留下沙箱**：如果测试进程异常退出（例如此前的 502），沙箱不会被回收，会一直占着模板导致
  `template delete failed: template is still in use`。测试后先 `cubemastercli list` 清干净再删模板。

### 10.9 能服务多少用户？（实测吞吐换算）

"能扛 1000 个沙箱" ≠ "能服务 1000 个用户"。用户容量要按**吞吐**算，量的是端到端回合（含 LLM 往返 + 工具调用 + 沙箱冷启动）。

**实测（8 核 / 61GB，`PI_MAX_CONCURRENT_RUNS=12`，每用户一个独立账号跑一个回合）：**

| 并发用户 | 成功 | 总墙钟 | 吞吐 | 单回合 p50 | p95 | 失败 |
|---|---|---|---|---|---|---|
| 4 | 4/4 | 27.8s | 9 /min | 9.0s | 27.8s | 0 |
| 8 | 8/8 | 6.6s | 73 /min | 6.2s | 6.6s | 0 |
| **12** | **12/12** | 7.9s | **91 /min**（峰值） | 4.9s | 7.9s | 0 |
| 16 | 16/16 | 12.5s | 77 /min | 6.5s | 12.5s | 0 |
| **24** | **24/24** | 14.2s | 101 /min | 8.8s | 13.2s | **0** |

> 第一档 4 用户 27.8s 是**冷启动**（首次建沙箱 + 解包），§6.5 说的预热问题；后续档位无此开销。

**三个关键结论：**

1. **用户多了不会失败，只会排队。** 24 个并发用户（是配额 12 的**两倍**）依然 24/24 成功，
   只是 p95 从 7.9s 涨到 13.2s。pi-py 用信号量排队（`RunManager` 的 `max_concurrent`），不是拒绝。
2. **吞吐在高并发处走平**：12 用户 91/min 是峰值，16 用户反而降到 77/min（排队调度开销）。
   这就是 `PI_MAX_CONCURRENT_RUNS` 该对齐物理核数的原因（§10.6）。
3. **热池只留 8 个会话**（`PI_SANDBOX_POOL_SIZE=8`）：前 8 个用户的下一个回合是"秒回"，
   第 9 个起每回合走一次冷启动快照恢复（~0.5s，代价很小，不是失败）。

**换算成"能服务多少用户"**（用 `服务用户数 = 吞吐 ÷ 每用户回合频率`，假设必须明说）：

| 使用强度 | 每用户 | 本机可服务 |
|---|---|---|
| 轻度（5 分钟 1 回合） | 0.2 回合/min | **~500 人** |
| 中度（1 分钟 1 回合） | 1 回合/min | **~100 人** |
| 重度（4 回合/min） | 4 回合/min | **~25 人** |

按业界"峰值并发 ≈ 注册用户数的 2%~5%"估：

| 峰值并发占比 | 对应注册用户数 |
|---|---|
| 2% | **~600 人** |
| 5% | **~240 人** |

> **注册用户总数本身没有上限**（只占数据库行）—— 不活跃的用户成本为零，
> 只有"正在跑回合"的用户才占并发槽。所以别用"注册了多少人"来估服务器，要用"**峰值同时活跃**"。

**注意**：本测试的延迟**包含外部模型网关往返**。若网关有速率限制或变慢，实际吞吐会随之下降——
这部分不在本机可控范围内。

### 10.10 本节避坑清单

| 坑 | 现象 | 原因 / 规避 |
|---|---|---|
| **用 1 核模板测"密度"** | 测出"只能跑 16 个" | 那是 `quota_cpu÷模板CPU` 的配额除法，**不是密度**。密度要用小 CPU 模板 + 看真实内存占用 |
| **为了密度把 CPU 调到 100m** | "并发 160 个！" 但每个慢 10 倍，首条命令 502 | 别刷这种数字。CPU 下限 500m |
| **只放开一层配额就以为到顶了** | "放开了 CPU 配额，还是只有 197 个" | 三层闸门是**串行**的：放开 CPU → 撞内存计费(400MB/沙箱) → 放开 `mem_limit` → 撞 `max_mvm_num`（由 mem_limit 推导）。见 §10.7 实测表 |
| **忘了 `tap_init_num` 也要放大** | 配额够但建到 500 就失败 | 默认 500，目标密度必须 ≤ 它 |
| **压测异常退出留下沙箱** | 删模板报 `template is still in use` | 测试后先 `cubemastercli list` 清理残留沙箱（挂着的会占住模板） |
| **以为内存配额会限制密度** | 想不通"2GiB 规格怎么能在 375GiB 上跑 1000 个" | 空载不预占配额（CoW），调度器另有 ~400MB/沙箱的固定计费 |
| **按密度上限设 `PI_MAX_CONCURRENT_RUNS`** | 高峰时"看起来没满"却排队/超时 | 并发上限要对齐**物理核数**，不是配额密度；且留余量 |
| **改配额测试后忘记还原** | 后续真实负载互相拖垮 | 测完把 `mcpu_limit` 还原为 `0`、`tap_init_num` 还原为 `500` |
| **重启 cubelet 后立刻判定"节点掉了"** | `NODES_SCANNED 0/0` | 重新注册要 15-30s，等一会儿再看 |

**错误码速记**：平台返回 `error code 130597: no more resource` = 配额耗尽（CPU 或内存计费）→
先 `cubemastercli list` 看堆积 VM 并清理，再核对 10.2 的三道闸门，**别盲目重试**。

**其余调参项**：

| 参数 | 实测值 | 建议 |
|---|---|---|
| 命令超时 | 120s 默认 / 600s 上限 | 不变（沙箱内 GNU timeout 包装，超时退出码 124） |
| close 总超时 | 90s | 不变（超时后清理线程继续收尾，VM 必死） |
| 平台 VM 空闲回收 | 600s | 不变（最后一道防泄漏防线） |

---

## 11. 生产化清单（部署完逐项打勾）

- [ ] `ls /dev/kvm` 在（嵌套虚拟化开）—— 没有它沙箱全挂
- [ ] 8300 只绑本机；公网仅 80/443 反代；`/metrics` 带 token
- [ ] 所有 `CHANGE_ME` / `e2b_000000` 换成强随机
- [ ] `PI_S3_ENDPOINT` 用客户端可达地址（§8.3）；MinIO 已建 `pi-files`/`pi-artifacts`
- [ ] 模板 READY 且做了首次预热（§6.5）
- [ ] `pi_py` 库自动迁移成功（`journalctl -u pi` 里无 migration 报错）
- [ ] 端到端冒烟：建会话 → 上传文件 → 让 agent `list_files`+`fetch_file` 处理 → 结果正确
- [ ] **模板规格核对**：`cpu=1000m`、`memory=256Mi`（重任务 512Mi）；**`cpu` 不低于 500m**（§10.3）
- [ ] **`PI_MAX_CONCURRENT_RUNS` 对齐物理核数**（8 核→12），不是按配额密度上限设（§10.6）
- [ ] **若做过压测**：`mcpu_limit` 已还原为 `0`、`tap_init_num` 已还原为 `500`（§10.8）
- [ ] `/metrics` 四个沙箱系列在（create/close/health/trace failures）
- [ ] **`/readyz` 的 `checks.sandbox` 是 `ok`** —— 平台不可用时它会翻 **503**（含数据面 `*.cube.app` 解析检查）。这是目前**唯一能自动发现"沙箱平台整体挂掉"的信号**：没有它时 `/readyz` 照样返回 200 ready，只能等用户报错或人工翻日志（见 §12 的 coredns 两条）。指标虽已就位，但**尚无采集与告警**，待办见 `ROADMAP.md` §3
- [ ] 归档目录可写；配 MinIO 后归档 json 的 `s3` 字段 non-null
- [ ] 备份：MySQL 每日 dump；归档同步异机/对象存储

---

## 12. 避坑清单（全部实测踩过）

| 坑 | 现象 | 规避 |
|---|---|---|
| 无 /dev/kvm | 建沙箱报 virtualization 错误 | 有 KVM 直接买；**没有也能救 → 装 PVM 内核（§3.2）** |
| PVM 内核没生效 | 重启后 `uname -r` 仍是旧内核，`ls /dev/kvm` 仍不存在，**且无任何报错** | 宿主机 `GRUB_DISABLE_SUBMENU="true"` 时官方 `GRUB_DEFAULT` 写法会静默失效，按 §3.2 ② 用顶层标题 |
| MinIO 端口撞平台(9000) | pi-minio 或平台 minio 起不来 | 文件管线 MinIO 用 19000 系（§2/§4.3） |
| PI_S3_ENDPOINT 用 127.0.0.1 | 客户端直传时 URL 指向自己 → 403/连不上 | 用客户端可达地址（§8.3） |
| boto3 签名 403(SigV2) | 预签名 PUT 时 `SignatureDoesNotMatch` | storage 里强制 `Config(signature_version="s3v4")`（已内置） |
| 沙箱镜像缺 envd/entrypoint | `Exec mount failed` / `mount namespace` | §6.6 三条契约 |
| **怎么快速判断模板镜像合格** | create 返回里 `envdVersion` 是 `0.2.0`（占位值，正常应 `0.5.x`）；数据面 `502 ... connect() failed ... :49983` | 该镜像没有可用的 envd，换 §6 构建的镜像 |
| base 用官方 python 镜像 | `reset guest time BrokenPipe` | `FROM alpine:3.20`（§6.6） |
| **e2b 系 SDK 版本漂移** | 建沙箱报 `405`（`POST /v2/sandboxes`，`allow: GET,HEAD`），或 connect 报 `404 /v2/sandboxes/{id}/connect` | **必须锁 §1 的 `e2b==2.26.0` + `e2b-code-interpreter==2.8.1`**：新版会改用 `/v2/` 前缀而 v0.7.2 的 CubeAPI 只有 v1 路径。`pip install` 不锁版本会漂到 2.5x/2.10 就必现 |
| **数据面 TLS 校验失败** | `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate` | 设 `SSL_CERT_FILE` 指向「平台自签 CA + 系统 CA」合并文件（§7 环境变量表） |
| 大镜像首个沙箱 502 | 模板 READY 后首启超时 | §6.5 预热 |
| VM 泄漏 | 跑几次后 `no more resource` | finally close + close 超时 + 平台回收（已内置） |
| 模板级断网无效 | denyall 模板仍出网 | 出网控制在宿主 egress（§8.2） |
| 8300 暴露公网 | 任何人可注册 | 只绑本机 + 反代认证 |
| `PI_MAX_CONCURRENT_RUNS` 开太大 | 排队任务全撞配额 | ≤ 平台 VM 上限 |
| Galera/Milvus/MySQL 端口冲突 | 各服务起不来 | 部署前过一遍 §2 端口表 |
| 连接串写 `mysql+pymysql`（同步） | 异步栈性能/兼容问题 | 用 `mysql+aiomysql://` |
| **宿主网段是 192.168.x** | 安装报 `default CubeSandbox network CIDR '192.168.0.0/18' conflicts with existing host network` | 加 `CUBE_SANDBOX_NETWORK_CIDR=10.66.0.0/16`（掩码须 /16~/24 且网络地址对齐，避开已有网段与 DNS） |
| **/data/cubelet 不是 XFS** | 安装预检报 `... is not XFS` | 预检硬性要求 XFS。无空闲数据盘时可用文件+loop 临时造：`truncate -s 80G /var/lib/cubelet-disk.img && mkfs.xfs -f 它 && mount -o loop,noatime 它 /data/cubelet`（生产仍建议真实 XFS 数据盘） |
| **拉不到 docker.io** | 建模板 `pull image fail: ... index.docker.io ... i/o timeout` | 配 containerd 镜像源（`Cubelet/config/config.toml` 的 `registry.mirrors."docker.io"`）与 `/etc/docker/daemon.json`；建模板时也可直接用全限定镜像地址绕过 |
| **镜像源挡特定仓库**（403） | 配了加速源仍拉不到某个镜像，如 `unexpected status from HEAD request ... 403 Forbidden`（实测 daocloud 对 `minio/minio` 返回 403，token 有效但仓库被挡） | 换一个源：`daemon.json` 里**按顺序配多个** `registry-mirrors` 做兜底，或显式用能拉到的源，例如 `docker pull docker.1panel.live/minio/minio:latest`，再 `docker tag` 成本地标准名。注意 `docker` 重启会**杀掉正在跑的 build**（见 §12 末行） |
| **重启 docker 打断镜像构建** | 构建中途 `exit code: 137`，日志停在某个 `apk add` / `pip install` | 137=SIGKILL，**先确认是不是自己 `systemctl restart docker` 杀的**。改 `daemon.json` 请避开构建期间，或构建完再重启 |
| **集群外客户端解析不了 `*.cube.app`** | `DNS error: no records found` | 用官方 `cubesandbox` SDK 时设 `CUBE_PROXY_NODE_IP`（内置 `IPOverrideTransport`，直连节点 IP 并保留 Host 头，同时免掉 DNS 与自签证书） |
| **pi-py 侧 `*.cube.app` 解析不了** | 服务日志 `Name or service not known`；e2b SDK 建完沙箱连不上数据面 | pi-py 用的是裸 e2b SDK（无 IP 覆盖），必须让**宿主机**能解析 `*.cube.app`。**不要手改 `/etc/resolv.conf`**——systemd-resolved 会重新生成覆盖掉。正确做法是按域路由（持久，且不影响其他域名解析）：<br>`/etc/systemd/resolved.conf.d/cube-app.conf`：<br>`[Resolve]`<br>`DNS=169.254.254.53`<br>`Domains=~cube.app`<br>然后 `systemctl restart systemd-resolved` |
| **coredns 没跑 → `*.cube.app` 整个解析不了** | 与上一行同样的 `Name or service not known`，但**根因不同**：`cube-sandbox-coredns` 未运行 | `systemctl status cube-sandbox-coredns cube-sandbox-dns`，**两个都要 active**：前者答 `*.cube.app → 节点IP`（Corefile 里写死 10.0.0.8），后者把 `~cube.app` 路由到 169.254.254.53。⚠️ **coredns 是 cubesandbox 模式的必需组件**，不是"可选/闲置"——只有不启用 cubesandbox 时才可不管它。`cube-sandbox-dns` 因 `Requires=coredns` 会连带起不来 |
| **coredns 起不来：`bind: permission denied`** | `Listen: listen tcp 169.254.254.53:53: bind: permission denied`，容器反复重启 | 镜像自带 `USER nonroot`，非 root 无 `CAP_NET_BIND_SERVICE` 绑不了 53；vendor 脚本又没留 `--cap-add` 口子。加 `/etc/sysctl.d/99-cube-coredns.conf`：`net.ipv4.ip_unprivileged_port_start = 53`（容器用 `--network host`，故对其生效），`sysctl --system` 后重启服务。**回滚时别忘了显式设回 1024**——删配置文件不会回退运行时值 |
| **`PI_SANDBOX_CA_FILE` 指向 `/root/...` 读不到** | 非 root 服务沙箱连接/TLS 失败，或 CA 静默缺失 | 默认值 `/root/.local/share/mkcert/rootCA.pem` 位于 `/root`（700），非 root 服务读不了。复制到可读路径再设 `PI_SANDBOX_CA_FILE`。**必须是「平台 CA + 系统 CA 合并」**：只放平台 CA 会让 `SSL_CERT_FILE` 覆盖系统信任链，**直接打断模型与 embedding 的 HTTPS**。<br>`cat /root/.local/share/mkcert/rootCA.pem /etc/ssl/certs/ca-certificates.crt > <可读路径>/cube-ca-bundle.pem` |
| **embedding 端点必须是原生格式，不能用 OpenAI 兼容** | 日志 `vector memory search failed (embed); falling back to lexical`，栈里 `KeyError: 'output'` | `EmbeddingClient` 只认 DashScope 原生契约（请求 `{"input":{"texts":[...]}}`、应答 `{"output":{"embeddings":[...]}}`）。填 `/compatible-mode/v1/embeddings`（OpenAI 形状）必挂。用 `https://<host>/api/v1/services/embeddings/text-embedding/text-embedding`。**失败是静默降级**，只在日志留一行，表现为"记忆检索时而好用时而不好用" |
| **向量召回"有命中反而失败"（`KeyError: 'id'`）** | 记忆条数从 0 变 1 后，向量检索开始恒抛错并被降级吞掉 | pymilvus 3.x 主键挂在 `Hit.id` 属性上，`Hit.entity` 只含请求的 `output_fields`；旧写法 `h["id"]` 在**空结果时不触发**（列表推导式不执行）故长期潜伏。改用 `h.id` |
| **`docker-compose` v1 管不了已存在的容器** | `docker compose up -d` 报 `KeyError: 'ContainerConfig'`（`compose/service.py: get_container_data_volumes`）| v1 (1.29.2) 与 Docker 29 不兼容，**只能在从零创建时用**。装 compose v2：apt 无 `docker-compose-plugin` 时从镜像源取 `.deb`，或把二进制放到 `/usr/local/lib/docker/cli-plugins/docker-compose` |
| **本地栈 milvus 容器反复 Restarting** | `tini` 打印 usage 后退出（exit 1）| `milvusdb/milvus` 镜像 **`Cmd=null`**，compose 不显式给命令就无程序可执行。补 `command: ["milvus","run","standalone"]`；embedded etcd 还需 `ETCD_DATA_DIR` 与 `ETCD_CONFIG_PATH=/milvus/configs/advanced/etcd.yaml` |
| **同机跑 CubeSandbox 后本地栈端口冲突** | `pi-py-mysql` / `pi-py-redis` 起不来（3306/6379 被平台组件占用）| `docker-compose.local.yml` 端口已参数化：在 `deploy/.env`（不纳入版本控制）里设 `PI_MYSQL_PORT` / `PI_REDIS_PORT` 覆盖即可，默认值保持不变 |
| **换目录后 `ModuleNotFoundError: No module named 'pi'`** | venv 里 `pi-py` 能用，代码一挪就不能 | editable 安装把源码路径**焊死**进 venv（`.pth` 指向构建时的目录）。别依赖它：在服务定义里显式 `Environment=PYTHONPATH=<repo>/src` + `WorkingDirectory=<repo>`，位置只声明一处，迁移只改这两行 |
| **`traces-*.jsonl` 写不进去（PermissionError）** | 服务日志 `PermissionError: .../.pi-py/traces-YYYY-MM-DD.jsonl` | `~/.pi-py` 被早先用 root 跑过的进程创建成 root 属主，非 root 服务写不进。`chown -R <运行用户>:<组> ~/.pi-py` |
| **服务起不来：`requires greenlet`** | `ImportError: The SQLAlchemy asyncio module requires that the Python 'greenlet' library is installed` | `pip install greenlet`（`pyproject.toml` 缺这个依赖，`[production]` 不会带上，见 §7） |
| **归档 `s3` 字段一直是 null** | 归档 json 里 `s3: null` 且 `s3_error: null`（**无任何报错**） | 归档用 `PI_ARCHIVE_S3_*` 而**不是** `PI_S3_*`，两套独立；配齐并先建好归档 bucket（见 §7.4）。另注意归档只在"该回合真用过沙箱"时触发 |


---

## 13. 从本机迁移到大内存服务器（动作清单）

1. 新机装 Ubuntu 24.04 → 开嵌套虚拟化 → `kvm-ok` 过。
2. Docker + 镜像加速 → 部署 §4 中间件（MySQL/Redis/MinIO **19000**/可选 Milvus）。
3. 部署 CubeSandbox 平台（§5）→ 推模板镜像（§6.1-6.3）→ 建模板（§6.4）→ 预热（§6.5）。
4. 拷 pi-py 源码 → venv 装 `[production]` → 写 `/etc/pi.env`（§7 全表，按 §9 调资源数字）→ systemd 起服务。
5. 按 §11 清单逐项打勾；重点跑一次 §11 的"端到端冒烟"（上传→进沙箱→处理）。
6. 旧机收尾：保留 30 天归档与审计日志后下线。

---

## 14. 沙箱性能基准（实测，企业 SLA 参考）

> 数据来自本机（4 核/3.6G，本地数据面 `127.0.0.1`）直连 CubeAPI(3000)，
> 模板 `pi-sandbox-ab`（481MB 镜像，1 核/256M）。命令跑 30 次取 p50/p95/p99。
> 数字随节点算力/网络变化，但**量级**可直接作为给企业用户的性能承诺。

### 14.1 冷启动（新建 VM → 可跑命令）

| 场景 | 耗时 |
|---|---|
| rootfs 已缓存（**常态**：节点跑过一次后） | **~0.25s**（`create()` 0.07s + 数据面就绪 0.18s） |
| 同会话池复用（热 VM） | 省去 create，仅命令延迟 ~10ms |
| rootfs 未缓存（模板刚建 / 新节点 / 镜像更新后首沙箱） | 首命令稳定 502（openresty 探活超时），预热后回到 0.25s，一次性成本 |

关键认知：`create()` 返回 ≠ 数据面就绪。microVM 起来后 envd 还要注册、
openresty 探活过了才放行命令；首沙箱要额外解包 ~400MB ext4，超过探活超时即 502。

### 14.2 已建 VM 的通信（latency / throughput）

| 指标 | 实测 | 说明 |
|---|---|---|
| 空命令往返 | p50 **9.2ms** / p95 11.8ms / p99 14.6ms | 纯数据面往返 ~10ms，本地 vsock 直通 |
| python3 -c 往返 | p50 34.7ms | 其中 ~25ms 是解释器启动 |
| 文件上传（宿→VM） | **154 MB/s** | `fetch_file` 进沙箱走这条通道 |
| 文件下载（VM→宿） | **30 MB/s** | `save_workspace` 回传走这条 |
| 1MB stdout 回传 | ~14.5 MB/s | 命令大输出时的瓶颈通道 |

### 14.3 典型任务时间账（对用户≈瞬时）

```
冷启动 250ms + 命令行数 × 10ms + 文件传输(10MB 上传 65ms / 下载 330ms)
≈ 远小于 1 秒 / 任务；同会话第二回合连 250ms 都省掉，只剩 10ms × 命令数
```

### 14.4 镜像体积定位

| 镜像 | 解压后 | 定位 |
|---|---|---|
| cube-lite / cube-lite-py | 8MB / 39MB | 平台设计基线（乐高微镜像） |
| **pi-sandbox（A+B 全套 office/pdf）** | **481MB**（压缩后 115MB） | **中小偏大** |
| E2B 官方模板 | 数百 MB ~ 2GB | 常规 |
| 数据科学 / ML / CUDA | 2GB ~ 15GB | 大镜像 |

**结论**：481MB 在行业尺度不算大（真大的以 GB 计）；但 CubeSandbox 的 openresty
探活超时按"几十 MB 微镜像"调校，400MB 级的 ext4 解包刚好踩到其超时边缘 ——
这才是首沙箱 502 的根因，不是镜像太大。

### 14.5 吞吐瓶颈提示

- **下载 30MB/s 明显慢于上传 154MB/s**（SDK files.read 回传有分块/轮询）。
  当前 900MB 上限 + 产物几 MB 的场景完全够用；若将来做"GB 级文件沙箱处理回传"，
  1GB 约需 33s，需走流式/直连优化（记档待做）。

### 14.6 测法（可复现）

```bash
# 冷启动 & 通信（需 /etc/ssl/certs/ca-certificates-meye.crt 或你的 SSL_CERT_FILE）
export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates-meye.crt
python3 - <<'PY'
import os, time, statistics
from e2b_code_interpreter import Sandbox
sbx = Sandbox.create(template="tpl-2492096525f04f0aac655acb", timeout=300,
                     api_url="http://127.0.0.1:3000", api_key="e2b_000000", domain="cube.app")
lats=[]; sbx.commands.run("true")
for _ in range(30):
    t=time.perf_counter(); sbx.commands.run("echo hi", timeout=10); lats.append((time.perf_counter()-t)*1000)
lats.sort(); print(f"命令往返 p50={lats[14]:.1f} p95={lats[28]:.1f} ms")
data=os.urandom(5*1024*1024)
t=time.perf_counter(); sbx.files.write("/tmp/up.bin", data); print(f"上传 {5/(time.perf_counter()-t):.0f} MB/s")
t=time.perf_counter(); sbx.files.read("/tmp/up.bin", format="bytes"); print(f"下载 {5/(time.perf_counter()-t):.0f} MB/s")
sbx.kill()
PY
```