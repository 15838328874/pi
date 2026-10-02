"""Shared test configuration.

测试与生产同构（2026-09-27 起统一）：DB 走本地 MySQL `pi_py_test` 库、缓存走本地
Redis（db1）。每个测试前清空共享测试库实现隔离（替代一次性 SQLite 文件）。
LLM / embedding / Milvus 在单测中用测试替身（FakeProvider / FakeEmbedder /
FakeVectorStore）——这是测试分层，不是 demo 环境；真实链路由 integration/ 验证
（PI_INTEGRATION=1）。基础设施未启动时单测会失败，先：
    docker compose -f deploy/docker-compose.local.yml up -d
"""

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# 默认值即 local-dev.md 的约定。可用环境变量覆盖，供 3306/6379 已被别的服务
# 占用的机器（如与 CubeSandbox 同机部署，其 MySQL/Redis 就在默认端口上）：
#   PI_TEST_DB_URL=mysql+aiomysql://pi:pi_py_local@127.0.0.1:13306/pi_py_test
#   PI_TEST_REDIS_URL=redis://127.0.0.1:16379/1
TEST_DB_URL = os.environ.get(
    "PI_TEST_DB_URL", "mysql+aiomysql://pi:pi_py_local@127.0.0.1:3306/pi_py_test"
)
TEST_REDIS_URL = os.environ.get("PI_TEST_REDIS_URL", "redis://127.0.0.1:6379/1")

# pi/__init__.py auto-loads ./.env on import, and existing environment variables
# win. Pin these before any test module imports pi so a production .env sitting in
# the repo root cannot point the suite at real Redis/Docker or write outside tmp.
os.environ["PI_DATABASE_URL"] = TEST_DB_URL
os.environ["PI_REDIS_URL"] = TEST_REDIS_URL
os.environ["PI_REDIS_NS"] = ""
os.environ["PI_JWT_SECRET"] = "test-secret-key"
os.environ["PI_SANDBOX"] = ""
os.environ["PI_POLICY"] = ""
os.environ["PI_TRACER"] = "noop"
os.environ["PI_EMBEDDING_URL"] = ""
os.environ["PI_EMBEDDING_API_KEY"] = ""
os.environ["PI_EMBEDDING_MODEL"] = ""
os.environ["PI_MILVUS_URI"] = ""
os.environ["PI_MCP_SERVERS"] = ""
os.environ["PI_SKILLS_DIR"] = ""
# trajectory persistence default would write raw runs to the real ~/.pi-py;
# "" = feature off (ServerSettings.trajectory_path is None)
os.environ["PI_TRAJECTORY_PATH"] = ""
# 审计/工作区同样钉到 /tmp，测试绝不写真实 home
os.environ["PI_AUDIT_PATH"] = "/tmp/pi-py-test-audit.jsonl"
os.environ["PI_WORKSPACE_ROOT"] = "/tmp/pi-py-test-ws"
os.environ["PI_METRICS"] = "1"
os.environ["PI_METRICS_TOKEN"] = ""


def _clean_tables() -> None:
    """清空共享 MySQL 测试库（测试间隔离）。连接失败时直接报错——按新纪律，
    基础设施没起就是测试环境错误，不静默跳过。

    连接信息**从 TEST_DB_URL 解析**，不再单写一份 host/port/user/password：
    原先这里硬编码 port=3306，而建表走的是 TEST_DB_URL 的端口。一旦两者不一致
    （例如本机 3306 已被别的服务占用、测试库挪到 13306），清表就连到了**另一个
    库**且不报错——测试库永远不清，残留数据让后续用例随机失败（表现为注册类
    测试 409 "username already exists"，且单跑正常、连跑才挂）。
    """
    import aiomysql
    from sqlalchemy.engine import make_url

    url = make_url(TEST_DB_URL)

    async def clean() -> None:
        conn = await aiomysql.connect(
            host=url.host or "127.0.0.1",
            port=url.port or 3306,
            user=url.username or "pi",
            password=url.password or "",
            db=url.database or "pi_py_test",
            charset="utf8mb4",
        )
        try:
            cur = await conn.cursor()
            # 表可能还没建（cleanup 可能跑在 app fixture 的 create_all 之前）：
            # 只清存在的表；下一个测试的 cleanup 自然会清掉本轮遗留的数据。
            await cur.execute("SHOW TABLES")
            existing = {row[0] for row in await cur.fetchall()}
            await cur.execute("SET FOREIGN_KEY_CHECKS=0")
            for table in ("usage_records", "messages", "compactions", "sessions", "memories", "users"):
                if table in existing:
                    await cur.execute(f"DELETE FROM {table}")
            await cur.execute("SET FOREIGN_KEY_CHECKS=1")
            # 播种 fixture 行：repo 直测用例以任意 user_id/session_id 写入，
            # MySQL 强制外键（SQLite 不强制）——给每个测试一个"有 20 个空用户
            # + 9 个空会话"的库。注册类测试的 AUTO_INCREMENT 会从 21 起，无断言依赖具体 id。
            await cur.execute(
                "INSERT INTO users (id, username, password_hash, is_admin, is_active, quota_tokens, created_at) VALUES "
                + ",".join(
                    f"({i},'u{i}','x',0,1,1000000,'2026-09-27 00:00:00')" for i in range(1, 21)
                )
            )
            await cur.execute(
                "INSERT INTO sessions (id, user_id, title, model, cwd, created_at) VALUES "
                + ",".join(
                    f"('s{i}',1,'fixture','fake/demo','/tmp','2026-09-27 00:00:00')" for i in range(1, 10)
                )
            )
            await conn.commit()
        finally:
            conn.close()

    asyncio.run(clean())


@pytest.fixture(autouse=True)
def _clean_shared_db():
    """每个测试前清表：每个测试都从空库开始（create_all 幂等，不插数据，
    与 app fixture 的执行顺序无关）。"""
    _clean_tables()
