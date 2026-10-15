"""schema / role 命名与标识符白名单。

**本文件是全仓唯一允许把标识符拼进 SQL 文本的地方。**其他任何地方要动态 schema
名或表名，必须先过这里的 :func:`safe_identifier`；不匹配白名单立即抛
``IsolationViolation``——把"拼串导致越界"这类事故收口到一个可 review 的函数。

命名约定（D-4）::

    workspace_id = "demo"  →  schema  ws_demo
                              role    wetag_ws_demo
                              search_path  ws_demo, org        （**不含 public**）
"""

from __future__ import annotations

import re

from app.contracts.errors import IsolationViolation

# 只允许小写字母开头的蛇形短标识符。长度 ≤31 留出 PG 63 字节上限的前缀空间。
#
# 结尾用 ``\Z`` 而不是 ``$``：Python 的 ``$`` 会在**末尾换行之前**也匹配，于是
# ``"demo\n"`` 能过闸门，拼出 ``SET search_path TO ws_demo\n, org``。而 workspace_id
# 常常来自配置文件或环境变量——末尾带个换行是最容易发生、也最难看出来的一种。
# ``\Z`` 只匹配真正的字符串结尾。
_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}\Z")

ORG_SCHEMA = "org"
WS_SCHEMA_PREFIX = "ws_"
WS_ROLE_PREFIX = "wetag_ws_"
ADMIN_ROLE = "wetag_admin"

# PG 保留字/易混淆名，不许当 workspace_id
_RESERVED = frozenset({"public", "org", "postgres", "pg_catalog", "information_schema"})


def safe_identifier(value: str, *, where: str = "schema.identifier") -> str:
    """校验并返回可安全拼接的 SQL 标识符。

    :raises IsolationViolation: 不匹配 ``^[a-z][a-z0-9_]{1,30}$`` 或落在保留名里。
    """
    if not isinstance(value, str) or not _IDENT_RE.match(value):
        raise IsolationViolation(where, _IDENT_RE.pattern, value)
    if value in _RESERVED:
        raise IsolationViolation(where, "non-reserved identifier", value)
    return value


def ws_schema(workspace_id: str) -> str:
    """业务线 schema 名。"""
    return WS_SCHEMA_PREFIX + safe_identifier(workspace_id, where="schema.ws_schema")


def ws_role(workspace_id: str) -> str:
    """业务线专属 DB 角色名。连接串里不存在访问他人 schema 的权限。"""
    return WS_ROLE_PREFIX + safe_identifier(workspace_id, where="schema.ws_role")


def search_path(workspace_id: str) -> str:
    """连接初始化用的 ``search_path``。

    **刻意不含 ``public``**：漏写 schema 前缀的 SQL 会直接报错而不是静默落到
    public，把配置疏漏变成启动期可见的失败。
    """
    return f"{ws_schema(workspace_id)}, {ORG_SCHEMA}"


def render_migration(sql: str, workspace_id: str, role: str | None = None) -> str:
    """把迁移文件里的占位符替换为本业务线的真实标识符。

    占位符只有两个，且值都过了白名单：

    * ``@ws_schema@`` → ``ws_demo``
    * ``@ws_role@``   → ``wetag_ws_demo``

    校验计算 checksum 用的是**替换前**的原文，所以同一份文件在不同业务线的
    checksum 一致，``init_workspace.py`` 才能拦住"改老文件"。

    ``role`` 允许显式传入非默认角色名；省略时按业务线推导。
    """
    return sql.replace("@ws_schema@", ws_schema(workspace_id)).replace(
        "@ws_role@", role if role is not None else ws_role(workspace_id)
    )


# ``validate_workspace`` 是 ``safe_identifier`` 在 workspace_id 这一处的具名用法。
# 保留它是因为调用点读起来是「校验业务线 id」而不是「校验某个标识符」。
def validate_workspace(workspace_id: str) -> str:
    return safe_identifier(workspace_id, where="schema.validate_workspace")


__all__ = [
    "ADMIN_ROLE",
    "ORG_SCHEMA",
    "render_migration",
    "safe_identifier",
    "search_path",
    "validate_workspace",
    "ws_role",
    "ws_schema",
]
