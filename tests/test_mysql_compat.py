"""MySQL-dialect compatibility: engine kwargs and portable DDL (no server needed)."""

from __future__ import annotations

from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.schema import CreateTable

from pi.server.db import User, engine_kwargs


class TestEngineKwargs:
    def test_mysql_url_gets_charset_and_recycle(self):
        kw = engine_kwargs("mysql+aiomysql://pi:p@h:3306/pi_py")
        assert kw["connect_args"] == {"charset": "utf8mb4"}
        assert kw["pool_recycle"] == 280

    def test_pg_and_sqlite_get_nothing(self):
        assert engine_kwargs("postgresql+asyncpg://u:p@h/db") == {}
        assert engine_kwargs("sqlite+aiosqlite:///x.db") == {}


class TestPortableDDL:
    def test_boolean_defaults_portable(self):
        """server_default must render valid DDL on all three dialects.

        A bare "true" string is invalid as a TINYINT default on MySQL
        (error 1067 at CREATE TABLE time), so the portable form is "1":
        accepted by PG (boolean literal cast), MySQL ('1' -> 1) and SQLite.
        """
        for dialect in (mysql.dialect(), postgresql.dialect(), sqlite.dialect()):
            ddl = str(CreateTable(User.__table__).compile(dialect=dialect))
            assert "'true'" not in ddl, f"dialect {dialect.name} emitted DEFAULT 'true'"
            assert "DEFAULT '1'" in ddl, f"dialect {dialect.name} lost is_active default"
