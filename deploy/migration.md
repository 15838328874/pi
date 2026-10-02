# pi-py 服务器迁移 Runbook

本文档固化"把 pi-py 从一台服务器搬到另一台服务器"的完整步骤。目标机**同样启用
CubeSandbox（microVM 沙箱）**——这是本部署的既定前提（沙箱 `fail-closed`，无降级）。

所有命令以当前服务器（旧机，路径 `/home/ubuntu/zhu/pi`）为准；`<目标机>` 需按实际替换。

---

## 0. 架构定论（先看这段，避免做无用功）

这台机器的部署是**三块**，各有各的形态，**刻意不塞进一个 compose**：

| 块 | 内容 | 形态 | 为什么 |
|---|---|---|---|
| ① pi-py 应用 | `pi-py serve`（:8300） | **裸机 systemd** | CubeSandbox 深度绑定宿主机（见下），应用必须跑在宿主机才能零成本对接 |
| ② pi-py 基础设施 | MySQL / Redis / Milvus / MinIO | **docker**（`deploy/docker-compose.local.yml` + 单独起的 MinIO） | 有状态服务，容器化最省事 |
| ③ CubeSandbox | cubelet/cubemaster/cube-api/coredns/proxy… 一整套 microVM 平台 | **独立安装**（它自己的 one-click installer） | 第三方平台，需要宿主机级权限，pi-py 只是它的客户端 |

**为什么 pi-py 应用不容器化**：`PI_SANDBOX=cubesandbox` 让应用依赖四处宿主机资源，
bridge 网络容器每处都要补对接，脆弱且无收益：

| 依赖 | 值 | 说明 |
|---|---|---|
| 控制面 | CubeAPI `http://127.0.0.1:3000`（监听 `0.0.0.0`） | 裸机直连 loopback |
| 数据面域名 | `*.cube.app → 10.0.0.8` | 走 systemd-resolved 的 `cube-dns0` 域路由（`~cube.app`），容器默认不继承 |
| 数据面网段 | `10.0.0.8/22` 在宿主机 `eth0` 上 | 容器要额外配路由 |
| rollback CLI | `/usr/local/services/cubetoolbox/CubeMaster/bin/cubemastercli` | 宿主机二进制，容器里没有 |
| CA bundle | `~/.pi-py/cube-ca-bundle.pem` | 数据面 HTTPS 验证，需挂载 |

真要容器化 pi-py，唯一合理的是 `network_mode: host`，但那样和裸机没差别，不推荐。

---

## 1. 迁移总览：五块清单

| # | 块 | 要搬的东西 | 数据位置 |
|---|---|---|---|
| A | 代码 + 配置 | git 仓库 + `.env.local` + systemd unit | `/home/ubuntu/zhu/pi` |
| B | 基础设施（docker） | compose 文件 + 数据卷 | `deploy_mysql-data` / `deploy_redis-data` / `deploy_milvus-data` / `pi-minio-data` |
| C | CubeSandbox | 重装（不搬数据，sandbox 是无状态 VM） | `/usr/local/services/cubetoolbox` |
| D | 运行时数据 | workspace / audit / trajectory（在 `~/.pi-py/` 下） | `~/.pi-py/` |
| E | 仓库内文件 | `policy.json`（git 追踪的安全规则，随仓库走） | `/home/ubuntu/zhu/pi/policy.json` |
| F | 仓库外文件 | CA bundle（也在 `~/.pi-py/` 下，与 D 同 base） | `~/.pi-py/cube-ca-bundle.pem` |

> docker 数据卷的物理位置：本机 docker data-root 是 `/root/data/disk/var-lib-docker`
> （曾迁移过，不在默认 `/var/lib/docker`）。目标机若用默认 data-root，拷卷时注意路径不同。

---

## 2. 目标机前置检查

```bash
# 系统与资源（当前服务器 3.6GB 内存是 CubeSandbox 的最小规格，目标机建议 ≥8G）
uname -a && free -h && df -h
# docker + compose v2
docker --version && docker compose version
# 端口占用：CubeSandbox 会占 3306/6379，pi-py 因此上移到 13306/16379，先确认干净
ss -tlnp | grep -E ":(3000|3306|6379|8300|13306|16379|19531|19000|5000|443)\b"
```

---

## 3. 迁移步骤

### 步骤 1：目标机装 CubeSandbox（先装，它先占端口/网络）

CubeSandbox 是独立平台，用自己的 one-click installer 装（**不要手动拆**）：

```bash
# 拿到 CubeSandbox 发行包（与旧机同版本 v0.7.2）后：
cp env.example .env && sudo ./install.sh
# 装完用它的 quickcheck 验证（install.sh 默认会跑）
```

装完应看到：`cube-sandbox-*.service` 一批 systemd 单元 active、`cube-*` 一批容器 up、
`resolvectl status` 里有 `~cube.app` 域路由、`ss -tlnp | grep :3000` 有 CubeAPI 监听。

> CubeSandbox 的配置（模板、API key）在其自身的 `.env`/平台里，与 pi-py 的
> `.env.local` 无关。装好后在平台侧建好模板（对应 `PI_SANDBOX_TEMPLATE` 的值）。

### 步骤 2：装 pi-py 基础设施（docker）

```bash
cd /home/ubuntu/zhu/pi
docker compose -f deploy/docker-compose.local.yml up -d
```

这起 3 个：`pi-py-mysql`（13306）、`pi-py-redis`（16379）、`pi-py-milvus`（19531）。

### 步骤 3：迁基础设施数据

**MySQL 用 mysqldump**（比停容器拷卷安全，跨版本兼容）：

```bash
# 旧机导出
docker exec pi-py-mysql sh -c 'mysqldump -uroot -ppi_root_local --single-transaction pi_py' > pi_py.sql
scp pi_py.sql <目标机>:~/
# 目标机导入（先等 pi-py-mysql 起来）
docker exec -i pi-py-mysql sh -c 'mysql -uroot -ppi_root_local pi_py' < ~/pi_py.sql
```

**Milvus / Redis / MinIO 停容器后拷卷**（Milvus standalone 是 embedded etcd + local
storage，运行中拷会不一致，必须先停）：

```bash
# 旧机：停 → 打包卷
docker stop pi-py-milvus pi-py-redis pi-minio
tar -C /root/data/disk/var-lib-docker/volumes -czf volumes.tar.gz \
    deploy_milvus-data deploy_redis-data pi-minio-data
scp volumes.tar.gz <目标机>:~/

# 目标机：解包到对应 data-root 的 volumes 目录（路径按目标机 data-root 调整）
tar -C $(docker info -f '{{.DockerRootDir}}')/volumes -xzf ~/volumes.tar.gz
docker compose -f deploy/docker-compose.local.yml up -d milvus redis
```

> MinIO 不在 compose 里（见 §6.6），且**当前未启用**（`PI_S3_*` 未配，文件管线关闭），
> 若目标机也不启用文件管线，可跳过 MinIO 的搬迁。

### 步骤 4：迁代码 + 配置

```bash
cd /home/ubuntu/zhu/pi
# 方式 A：git（.env.local 不入库，另行拷）
git clone <仓库地址> .
# 方式 B：整目录打包
# tar --exclude='__pycache__' --exclude='.git' -czf pi.tar.gz . ; scp 后解包

# 配置：用已同步的模板填密钥
cp deploy/env.local.example .env.local && chmod 600 .env.local
vi .env.local   # 填密钥；只有 policy / CA 两处路径要按项目根/home 改，其余免改
ln -sf ../.env.local deploy/.env

# venv
python3 -m venv /opt/pi-venv && /opt/pi-venv/bin/pip install -e .
```

### 步骤 5：迁运行时数据（`~/.pi-py/`）+ 仓库内 policy

```bash
# workspace/audit/trajectory 默认都在 ~/.pi-py/ 下（home 相关，迁移零路径改动）
scp -r <旧机>:~/.pi-py/workspaces ~/.pi-py/
scp <旧机>:~/.pi-py/audit*.jsonl <旧机>:~/.pi-py/trajectories*.jsonl ~/.pi-py/ 2>/dev/null
# policy.json 随仓库走（git 追踪），已在步骤 4 的 git clone 里带上
```

### 步骤 6：仓库外文件（CA）+ systemd

```bash
# CA 合并包（数据面 HTTPS 验证；若目标机 CubeSandbox 重装生成了新 CA，则重新合并生成）
# 它也在 ~/.pi-py/ 下，可与步骤 5 一起搬
scp <旧机>:~/.pi-py/cube-ca-bundle.pem ~/.pi-py/

# systemd unit（唯一"项目根相关"的路径：WorkingDirectory / EnvironmentFile；PYTHONPATH 已是相对的 src）
scp <旧机>:/etc/systemd/system/pi-py.service /etc/systemd/system/
# 改完路径后：
sudo systemctl daemon-reload && sudo systemctl enable --now pi-py.service
```

### 步骤 7：验证（见 §5）

---

## 4. 数据卷备份/恢复速查

| 服务 | 备份方式 | 恢复方式 |
|---|---|---|
| MySQL | `docker exec pi-py-mysql sh -c 'mysqldump -uroot -ppi_root_local --single-transaction pi_py'` | `docker exec -i pi-py-mysql sh -c 'mysql -uroot -ppi_root_local pi_py'` |
| Redis | 停容器拷卷（AOF） | 停容器还原卷 |
| Milvus | 停容器拷卷 | 停容器还原卷 |
| MinIO | 停容器拷卷 | 停容器还原卷 |

---

## 5. 迁移后验证清单

```bash
# 1) 服务健康（db/cache/milvus/rag/sandbox 全 ok，sandbox 是硬检查）
curl -s http://127.0.0.1:8300/readyz
# 期望 {"status":"ready","checks":{"db":"ok","cache":"ok","milvus":"ok","rag":"ok","sandbox":"ok"}}

# 2) CubeSandbox 控制面 + 数据面 DNS
curl -s http://127.0.0.1:3000/health
resolvectl query test.cube.app   # 期望 10.0.0.8

# 3) 端到端跑一轮 agent：起一个 session，让 bash 工具执行 `echo ok`，
#    确认 sandbox 能创建/执行/销毁（这是 CubeSandbox 对接的最终证明）

# 4) RAG：上传一个 PDF，确认 ready，检索能命中
```

---

## 6. 关键坑位（务必逐条确认）

### 6.1 端口上移是必须的
CubeSandbox 占 3306/6379，pi-py 因此用 13306/16379。**先装 CubeSandbox 再装 pi-py**，
顺序反了会端口冲突。改端口只改 `.env.local` 三组值（`PI_MYSQL_PORT`/`PI_REDIS_PORT`/
`PI_MILVUS_PORT` + 下方 URL），测试会自动跟随。

### 6.2 CA bundle：平台只生成"平台 CA"，"合并 bundle"要手动一步

CubeSandbox 安装时用 mkcert 生成**平台自己的根 CA**（`/root/.local/share/mkcert/rootCA.pem`，
用于签发 `*.cube.app` 的 HTTPS 证书）。**但 pi-py 需要的合并 bundle 平台不会生成**，必须手动合并：

```bash
# 平台装完后，手动执行一次（合并"平台 CA + 系统 CA"）：
sudo cat /root/.local/share/mkcert/rootCA.pem > ~/.pi-py/cube-ca-bundle.pem
cat /etc/ssl/certs/ca-certificates.crt >> ~/.pi-py/cube-ca-bundle.pem
# 结果 = 1 个平台 CA + 122 个系统 CA = 123 个证书
```

**为什么必须合并、不能只放平台 CA**：`PI_SANDBOX_CA_FILE` 会被设成 `SSL_CERT_FILE`，
Python 的 ssl 把它当作**唯一的信任根**。单放平台 CA 会丢掉全部 122 个公共 CA（Amazon/
Google 等），模型与 embedding 的 HTTPS 调用（走公共 CA）会全部失败。所以两边都要信任。

**为什么平台 CA 在 `/root` 下**：mkcert 默认生成到 `$HOME/.local/share/mkcert/`，CubeSandbox
以 root 安装，故落在 `/root`。而 `/root` 是 700，服务以 ubuntu 运行读不到——这也是必须
"读出 + 合并到 `~/.pi-py/`"的原因。

### 6.3 rollback 依赖宿主机二进制 cubemastercli
`PI_CUBE_CLI` 默认 `/usr/local/services/cubetoolbox/CubeMaster/bin/cubemastercli`。
这是 CubeSandbox 装的，pi-py 快照回滚（`snapshot rollback`）用 `subprocess` 调它。
CubeSandbox 装好后它自然存在；pi-py 保持裸机才能直接调。

### 6.4 数据面 DNS 是 systemd-resolved 的域路由
`*.cube.app` 不是 hosts 文件条目，是 systemd-resolved 的 `cube-dns0` link（
`DNS Domain: ~cube.app`，权威 DNS `169.254.254.53`）。这是 CubeSandbox 的 coredns 装的。
**别手动往 `/etc/hosts` 写 `10.0.0.8 cube.app`**（那是通配域名，hosts 不支持通配）。

### 6.5 docker data-root 不在默认位置
本机 `Docker Root Dir: /root/data/disk/var-lib-docker`（磁盘扩容时迁过）。拷卷、打包
时路径以 `docker info -f '{{.DockerRootDir}}'` 为准，别硬编码 `/var/lib/docker`。

### 6.6 pi-minio 当前"起了但未启用"
`pi-minio` 是 `docker run` 起的（不在 compose），但 `.env.local` 里 `PI_S3_*` **没配**，
文件管线（预签名直传）功能实际是关闭的。迁移时：
- 不启用文件管线 → 跳过 MinIO 搬迁；
- 要启用 → 目标机起 MinIO 并配 `PI_S3_ENDPOINT` / `PI_S3_ACCESS_KEY` /
  `PI_S3_SECRET_KEY` / `PI_S3_BUCKET_*`（见 `src/pi/server/config.py`）。

MinIO 的启动命令还原（供参考，密钥用 `<minio 密码>` 替换）：

```bash
docker run -d --name pi-minio --restart unless-stopped \
  -p 19000:9000 -p 127.0.0.1:19001:9001 \
  -e MINIO_ROOT_USER=pi_minio -e MINIO_ROOT_PASSWORD=<minio 密码> \
  -v pi-minio-data:/data \
  minio/minio server /data --console-address :9001
```

### 6.7 换 embedding 模型必须先重建向量投影
`.env.local` 里的 `PI_EMBEDDING_MODEL` 若换了模型，Milvus 里的旧向量会静默空间漂移。
迁移到新机若顺手换了模型，务必先重建（见 `ROADMAP.md` §3，`tools/rebuild_eval_index.py`）。
