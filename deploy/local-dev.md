# 本地测试环境指南

> 目标：在 WSL/本机搭一套**与生产同构**的完整环境（MySQL + Redis + Milvus + 云模型/embedding + Docker 沙箱
> ——生产沙箱是 CubeSandbox microVM，本地用 Docker 形态验证协议），
> 用真实模型跑通全链路并把数据落到本地库。**绝不连生产的 MySQL/Redis/Milvus，不污染生产数据。**

## 1. 生产 vs 本地测试（区分表）

| 组件 | 生产环境 | 本地测试环境 |
|---|---|---|
| 数据库 | 云端托管 **MySQL**（`.env` 里 `mysql+aiomysql://...@mysql...ivolces.com`） | Docker `mysql:8`，`127.0.0.1:3306`，库 `pi_py`，用户 `pi/pi_py_local` |
| 缓存/锁 | 云端托管 Redis（`.env` 里 `redis-cngz...ivolces.com`） | 本机 Redis（apt 的 `redis-server` 或 compose），`127.0.0.1:6379` |
| 向量库 | 云端 Milvus | Docker `milvusdb/milvus:v2.5.4` standalone（embedded 模式），`127.0.0.1:19531` |
| 模型 | `openai/qwen3.8-flash`（阿里云 MaaS，本地默认；生产可在 .env 里另配） | **同一云模型**（本地无 GPU，只把 endpoint/key 指过去） |
| embedding | 云 embedding（qwen3.7-text-embedding） | **同一云 embedding**（向量写入本地 Milvus，不进生产） |
| 沙箱 | CubeSandbox microVM（`PI_SANDBOX=cubesandbox`，见 `docs/production-deployment.md`）；Docker 亦可 | 本机 Docker（`PI_SANDBOX=docker`） |
| 配置文件 | `.env`（生产，gitignored） | `deploy/env.local.example` → 复制为 `.env.local` |
| 基础设施编排 | `docker-compose.cloud.yml`（app+caddy，DB/Redis 外部） | `docker-compose.local.yml`（mysql+redis+milvus，app 跑宿主机） |
| 数据文件 | 服务器磁盘（`/root/...`） | 本机：`workspaces/`、`audit.jsonl`、`trajectories.jsonl` 都在项目目录下 |

**铁律**：两个环境唯一的交集是**云模型/embedding 的 API 调用**（无状态）。任何状态数据
（用户、会话、消息、用量、向量、轨迹、审计）都不跨环境。

## 2. 一次性初始化

```bash
# 1) 基础设施（已有本地 Redis/Milvus 时只起 mysql 即可）
docker compose -f deploy/docker-compose.local.yml up -d

# 2) 环境变量
cp deploy/env.local.example .env.local
#    编辑 .env.local：把 OPENAI_API_KEY/OPENAI_BASE_URL/PI_EMBEDDING_URL/PI_EMBEDDING_API_KEY
#    填成生产 .env 里的值（云 API 是两环境共用的部分）

# 3) 建表（alembic 迁移 0001~0004）
set -a; source .env.local; set +a
pi-py migrate
```

### 2.1 端口冲突：让 `.env.local` 成为唯一来源

若本机 3306/6379 已被别的服务占用（**与 CubeSandbox 同机部署就是这种情况**——它自己的
MySQL/Redis 正好在默认端口上），把本地栈端口整体上移即可，**只改 `.env.local` 一处**：

```bash
PI_MYSQL_PORT=13306      # compose 端口映射
PI_REDIS_PORT=16379
PI_MILVUS_PORT=19531
PI_DATABASE_URL=mysql+aiomysql://pi:pi_py_local@127.0.0.1:13306/pi_py   # 应用
PI_REDIS_URL=redis://127.0.0.1:16379/0
PI_MILVUS_URI=http://127.0.0.1:19531
```

再把它接到 compose 的 env 文件位置，三处自动一致：

```bash
ln -sf ../.env.local deploy/.env      # compose 读 deploy/.env；软链接后读同一份
```

之后：

| 消费方 | 怎么拿到端口 |
|---|---|
| 应用（`pi-py serve` / systemd） | 直接读 `.env.local` 的 `PI_*_URL` |
| 基础设施（compose） | 通过 `deploy/.env` 软链接读到同一份的 `PI_*_PORT` |
| 测试（`tests/conftest.py`） | 从 `PI_DATABASE_URL` / `PI_REDIS_URL` **推导**（换库名 `pi_py_test`、Redis db1）|

**为什么要有这层统一**（实测踩过）：三处各写一份端口时，一旦不一致就会出现
"建表连一套、清表连另一套"的静默错位——清理作用在别的库上且不报错，测试库永远不清，
用例随机失败（注册类 409、redis 认证错误，单跑正常、连跑才挂），极难定位。
`deploy/docker-compose.local.yml` 的端口默认值仍是 `3306/6379`（与生产文档一致），
所以没软链接的机器行为不变。

## 3. 启动

```bash
set -a; source .env.local; set +a
pi-py serve            # http://localhost:8300（WSL2 mirrored 模式下 Windows 直接访问）
```

Windows 浏览器打开 `http://localhost:8300/ui/app.html`（用户端）/ `ui/admin.html`（管理台）。

## 4. 账号

本地库是全新的，第一个账号注册后由管理员直接写库提权（无自助提权路由）：

```bash
curl -X POST http://localhost:8300/v1/auth/register \
  -H 'Content-Type: application/json' -d '{"username":"zhu","password":"<你的密码>"}'
docker exec -i pi-py-mysql mysql -uroot -ppi_root_local pi_py \
  -e "UPDATE users SET is_admin=1 WHERE username='zhu';"
```

## 5. 常用操作

| 操作 | 命令 |
|---|---|
| 看本地 MySQL 数据 | `docker exec -i pi-py-mysql mysql -uroot -ppi_root_local pi_py` |
| 看 Redis | `redis-cli -h 127.0.0.1` |
| 看 Milvus 集合 | `python -c "from pymilvus import MilvusClient; c=MilvusClient(uri='http://127.0.0.1:19531'); print(c.list_collections())"` |
| 跑测试（用 SQLite，与本地环境无关） | `.venv/bin/python -m pytest` |
| 真实模型 eval/rollout | `pi-py eval rollout --tasks evals/tasks --model openai/qwen3.8-max` |

## 6. 故障排查

- **MySQL 容器没起来**：`docker logs pi-py-mysql`；首次启动要等 30~60 秒（初始化+建库）。
- **Milvus 连不上**：`docker ps | grep milvus`；URI 是 `http://127.0.0.1:19531`（宿主端口，不是 19530）。
- **embedding 报错**：检查 `.env.local` 里三个 `PI_EMBEDDING_*` 是否从生产 `.env` 复制了。
- **登录 500**：说明 Redis 没通（`redis-cli ping` 应回 PONG）；本地 Redis 不在时用 compose 起。
- **切回假模型**（省钱调试 UI）：`PI_MODEL=fake/demo`（fake 模型只用于 UI/协议调试，响应是脚本化的）。
