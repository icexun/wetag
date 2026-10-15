"""``CliResult<T>`` 与摘要脱敏（doc2 §5.2）。

**业务层永不接触 stdout 原文**——它只拿到按 schema 解析后的 ``data``，
或归一化的 ``error_code``。原文只以脱敏截断后的 ``stdout_digest`` 形式
进入日志与 ``audit_log``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, TypeVar

from app.contracts.errors import PortUnavailable

T = TypeVar("T")


class CliErrorCode(StrEnum):
    """归一化错误码。doc2 §5.2 的表加两项本地必需：

    * ``BINARY_MISSING``——CLI 没装（今日 ``tapd-cli`` / ``wecom-cli`` 就是这个）。
      它与 ``AUTH_FAILED`` 必须区分：前者只是端口不可用，后者触发安全告警。
    * ``OUTPUT_TOO_LARGE``——超出 ``max_output_bytes``，防一条命令把内存吃掉。
    """

    OK = "OK"
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    UPSTREAM_5XX = "UPSTREAM_5XX"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    AUTH_FAILED = "AUTH_FAILED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NOT_FOUND = "NOT_FOUND"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    CONFLICT = "CONFLICT"
    OUTPUT_UNPARSABLE = "OUTPUT_UNPARSABLE"
    BINARY_MISSING = "BINARY_MISSING"
    OUTPUT_TOO_LARGE = "OUTPUT_TOO_LARGE"
    UNKNOWN = "UNKNOWN"


# 脱敏规则。宁可多掩一点——摘要只用于排障，丢掉信息远好过泄露凭据。
# **顺序有意义**：先掩最具体的形态，再兜底长串。否则具体规则会被兜底规则的
# 替换结果吃掉（例如 Authorization 头只掩到 "Bearer" 而放过真 token）。
_SENSITIVE = (
    r"token|secret|password|passwd|app_?secret|access_?token|refresh_?token|"
    r"api[_-]?key|apikey|cookie|authorization|session_?key|credential"
)

_MASK_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # 1) HTTP 头形态：值可能含空格，掩到行尾
    (
        re.compile(r"(?im)^(\s*(?:authorization|cookie|set-cookie)\s*:\s*).+$"),
        r"\1***",
    ),
    # 2) Bearer / Basic 令牌
    (re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9._\-+/=]{8,})"), r"\1 ***"),
    # 3) CLI flag 形态：--token xxx / --api-key=xxx（最容易漏的一种）
    (
        re.compile(rf"(?i)(--?(?:{_SENSITIVE})[=\s]+)(\S+)"),
        r"\1***",
    ),
    # 4) key=value / JSON "key": "value"
    (
        re.compile(rf"(?i)\b({_SENSITIVE})(\"?\s*[:=]\s*\"?)([^\s\",;&}}]+)"),
        r"\1\2***",
    ),
    # 5) URL query 里的敏感参数
    (
        re.compile(r"(?i)([?&](?:token|access_token|key|sig|signature)=)([^&\s]+)"),
        r"\1***",
    ),
    # 6) 兜底：连续 40+ 的疑似凭据串
    (re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"), "***"),
)

_DIGEST_MAX = 800


def mask(text: str) -> str:
    """掩码敏感片段。日志、摘要、审计一律先过这里。"""
    out = text
    for pattern, repl in _MASK_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def digest(text: str | bytes | None, *, limit: int = _DIGEST_MAX) -> str:
    """脱敏 + 截断，生成可安全落库的摘要。

    截断保留头尾：报错信息常在末尾，只留开头等于丢掉最有用的部分。
    """
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    masked = mask(text.strip())
    if len(masked) <= limit:
        return masked
    head = masked[: limit * 2 // 3]
    tail = masked[-(limit // 3) :]
    return f"{head}…[truncated {len(masked) - limit} chars]…{tail}"


@dataclass(frozen=True, slots=True)
class CliResult(Generic[T]):
    """一次 CLI 调用的结果（doc2 §5.2）。"""

    ok: bool
    data: T | None = None
    error_code: CliErrorCode = CliErrorCode.OK
    retryable: bool = False
    stdout_digest: str = ""
    stderr_digest: str = ""
    elapsed_ms: int = 0
    exit_code: int | None = None
    attempt: int = 1
    command_key: str = ""

    @classmethod
    def success(cls, data: T, **kw: object) -> "CliResult[T]":
        return cls(ok=True, data=data, error_code=CliErrorCode.OK, **kw)  # type: ignore[arg-type]

    @classmethod
    def failure(
        cls, error_code: CliErrorCode, *, retryable: bool = False, **kw: object
    ) -> "CliResult[T]":
        return cls(ok=False, error_code=error_code, retryable=retryable, **kw)  # type: ignore[arg-type]

    def unwrap(self) -> T:
        """取数据，失败即抛 ``PortUnavailable``。

        让"忘记检查 ok"变成显式异常而不是拿到 ``None`` 继续往下跑——
        后者会一路变成"没查到 ⇒ 没问题"，正是失败语义诚实要禁止的。
        """
        if not self.ok or self.data is None:
            raise PortUnavailable(
                self.command_key or "cli",
                self.error_code.value,
                self.stderr_digest or self.stdout_digest or "无输出",
                retryable=self.retryable,
            )
        return self.data

    def audit_fields(self) -> dict[str, object]:
        """写入 ``audit_log`` 的字段（裁决 #9：并入 audit_log，不建 tool_execution 表）。"""
        # 失败时 stderr 优先、stdout 兜底：很多 CLI 把业务错误打在 stdout 的信封里
        # （``{"ok": false, "error": {...}}`` 正是本仓认定的权威失败信号），此时
        # stderr 是空的。只取 stderr 会让**最常见的那条失败路径**在审计里只剩一个
        # 错误码、没有任何细节。两者都已过 mask，兜底不引入新的泄漏面。
        return {
            "action": self.command_key,
            "outcome": "ok" if self.ok else self.error_code.value,
            "result_digest": (
                self.stdout_digest
                if self.ok
                else (self.stderr_digest or self.stdout_digest)
            ),
            "elapsed_ms": self.elapsed_ms,
            "exit_code": self.exit_code,
            "attempt": self.attempt,
        }


__all__ = ["CliErrorCode", "CliResult", "digest", "mask"]
