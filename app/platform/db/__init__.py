"""PostgreSQL 接入底座。

业务模块从这里取 ``Database`` 与两个 Repository 基类：它们只依赖访问面，
不感知 asyncpg 与 schema 命名规则。
"""

from app.platform.db.pool import Database
from app.platform.db.repository import BaseRepository, Repository
from app.platform.db.schema import (
    ORG_SCHEMA,
    render_migration,
    safe_identifier,
    search_path,
    ws_role,
    ws_schema,
)

__all__ = [
    "ORG_SCHEMA",
    "BaseRepository",
    "Database",
    "Repository",
    "render_migration",
    "safe_identifier",
    "search_path",
    "ws_role",
    "ws_schema",
]
