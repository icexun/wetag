"""当前期望 schema 版本号常量，被 platform/db/migration_check.py 读取。

版本号 = 已发布 **workspace** 迁移文件的最大序号——``migration_check`` 与
``init_workspace --verify-version`` 都只筛 ``scope='workspace'`` 的行来比对。
org 迁移另记一行且 workspace_id 为空串，不进这个比较，所以新增 org 迁移**不抬**
本常量。启动时不匹配即拒绝——独立部署的 DB 不能靠删文件重建。

**已发布文件只增不改**：``init_workspace.py`` 会校验 checksum，改动老文件直接拒绝执行。
需要调整已有表结构时新增 ``0008_*.sql``。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent

EXPECTED_SCHEMA_VERSION = 7

# 只在 org schema 上跑一次的迁移（--apply-org）
ORG_MIGRATIONS: tuple[str, ...] = ("0001_init_org_schema.sql",)

# 每个业务线 schema 各跑一次的迁移，顺序即依赖顺序
WORKSPACE_MIGRATIONS: tuple[str, ...] = (
    "0002_init_workspace_schema.sql",
    "0003_space_tables.sql",
    "0004_channel_tables.sql",
    "0005_capability_tables.sql",
    "0006_control_tables.sql",
    "0007_roles_and_grants.sql",
)

ALL_MIGRATIONS: tuple[str, ...] = ORG_MIGRATIONS + WORKSPACE_MIGRATIONS

# Runtime 侧代码沿用的旧名，指向同一组 org 迁移
SHARED_MIGRATIONS: tuple[str, ...] = ORG_MIGRATIONS


def migration_checksum(raw: str) -> str:
    """对**替换占位符之前**的原文做 checksum，同一文件在各业务线一致。

    定义在这里而非 init_workspace.py：写入台账的一方（建库脚本）与校验台账的一方
    （启动期 migration_check）必须用同一个算法，各写一份迟早会各自漂移——而这个
    函数的全部意义就是发现漂移。
    """
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def expected_checksums(filenames: tuple[str, ...] = WORKSPACE_MIGRATIONS) -> dict[str, str]:
    """磁盘上这批迁移文件的 checksum，供与 ``org.schema_migration`` 比对。"""
    return {
        name: migration_checksum((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
        for name in filenames
    }


__all__ = [
    "ALL_MIGRATIONS",
    "EXPECTED_SCHEMA_VERSION",
    "MIGRATIONS_DIR",
    "ORG_MIGRATIONS",
    "SHARED_MIGRATIONS",
    "WORKSPACE_MIGRATIONS",
    "expected_checksums",
    "migration_checksum",
]
