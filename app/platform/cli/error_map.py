"""错误归一表与处置动作（doc2 §5.2）。

三个 CLI 的报错文本各不相同，业务层不该认识它们。本文件把 stderr / 退出码
归一成 ``CliErrorCode``，再由 :func:`action_for` 映射到处置动作。

**安全纪律**：``AUTH_FAILED`` / ``PERMISSION_DENIED`` 一律 **不重试** 并走
安全告警——整体方案把 DB/权限类错误定义为「疑似越权」安全事件，静默重试
会把一次配置错误变成一串告警噪音，掩盖真正的越权尝试。
"""

from __future__ import annotations

import re
from enum import StrEnum

from app.platform.cli.result import CliErrorCode


class CliAction(StrEnum):
    """处置动作。与 doc2 §5.2 的「处理」列一一对应。"""

    RETRY_BACKOFF = "RETRY_BACKOFF"
    ALERT_NO_RETRY = "ALERT_NO_RETRY"
    DEGRADE = "DEGRADE"
    CLARIFY = "CLARIFY"
    RECOMPUTE_RETRY_ONCE = "RECOMPUTE_RETRY_ONCE"
    CONTRACT_TEST = "CONTRACT_TEST"
    SWITCH_IMPL = "SWITCH_IMPL"


# 归一表。**顺序有意义**：先判最具体的（配额 / 鉴权），再落到宽泛的 5xx。
# 把 429 放在 5xx 之前，否则 "HTTP 429 upstream error" 会被误判成 UPSTREAM_5XX。
_RULES: tuple[tuple[re.Pattern[str], CliErrorCode], ...] = (
    (re.compile(r"(?i)\b(command not found|no such file or directory|"
                r"executable file not found|ENOENT)\b"), CliErrorCode.BINARY_MISSING),
    # 两个坑：\bexceed\b 匹配不上 "exceeded"（用 \w* 收尾）；
    # \b 对 CJK 无效（中日韩字符本身是 word char，故中文分支不加 \b）。
    (re.compile(r"(?i)((?:\bquota\b|配额).{0,40}?(?:exceed\w*|exhaust\w*|用尽|不足|超限)|"
                r"(?:exceed\w*|exhaust\w*).{0,40}?(?:\bquota\b|配额)|"
                r"remaining\s*[:=]\s*0\b)"), CliErrorCode.QUOTA_EXCEEDED),
    (re.compile(r"(?i)(\b429\b|too many requests|rate.?limit|频率限制|限流)"),
     CliErrorCode.RATE_LIMITED),
    # lark-cli 缺 scope 报 "unauthorized: ... does not cover the required scope(s)"，
    # 须在鉴权规则之前认走，否则落成 AUTH_FAILED；企微机器人缺授权报 "no authorization"
    (re.compile(r"(?i)(missing[_ ]scope|missing required scope|"
                r"does not cover the required scope|no authori(?:ty|[sz]ation))"),
     CliErrorCode.PERMISSION_DENIED),
    (re.compile(r"(?i)(\b401\b|unauthorized|invalid[_ ]?token|token.*(expired|invalid)|"
                r"not logged in|未登录|认证失败|auth\w*[_ ]fail)"),
     CliErrorCode.AUTH_FAILED),
    (re.compile(r"(?i)(\b403\b|forbidden|permission denied|access denied|"
                r"scope\b.*\b(missing|required|insufficient)|无权限|权限不足)"),
     CliErrorCode.PERMISSION_DENIED),
    # 「已删除」的英文形态也要认：飞书文档删除报 "has been deleted"（code
    # 3380003），认不出来会把永久性失败当瞬时故障， stale 慢路径永远走不到归档。
    (re.compile(r"(?i)(\b404\b|not[_ ]found|does not exist|has been deleted|"
                r"no longer exists|已删除|被删除|不存在)"),
     CliErrorCode.NOT_FOUND),
    (re.compile(r"(?i)(\b409\b|conflict|revision\b.*\b(mismatch|changed|stale)|"
                r"version\b.*\b(conflict|mismatch)|乐观锁|版本冲突)"),
     CliErrorCode.CONFLICT),
    # tapd-cli 参数错报 "API 错误 422"；token 失效同为 422，但已被上面的鉴权规则先认走
    (re.compile(r"(?i)(\b400\b|\b422\b|invalid[_ ]argument|bad request|"
                r"invalid[_ ]param|参数错误|参数无效)"), CliErrorCode.INVALID_ARGUMENT),
    (re.compile(r"(?i)(\b5\d\d\b|internal server error|bad gateway|"
                r"service unavailable|gateway timeout|服务不可用)"),
     CliErrorCode.UPSTREAM_5XX),
    (re.compile(r"(?i)(\btimed? ?out\b|deadline exceeded|超时)"), CliErrorCode.TIMEOUT),
)

# 可重试集合。**只有平台抖动类可重试**——鉴权、参数、配额重试只会放大问题。
RETRYABLE: frozenset[CliErrorCode] = frozenset(
    {
        CliErrorCode.TIMEOUT,
        CliErrorCode.RATE_LIMITED,
        CliErrorCode.UPSTREAM_5XX,
    }
)

_ACTIONS: dict[CliErrorCode, CliAction] = {
    CliErrorCode.OK: CliAction.RETRY_BACKOFF,  # 不会被用到，占位保证字典全覆盖
    CliErrorCode.TIMEOUT: CliAction.RETRY_BACKOFF,
    CliErrorCode.RATE_LIMITED: CliAction.RETRY_BACKOFF,
    CliErrorCode.UPSTREAM_5XX: CliAction.RETRY_BACKOFF,
    # 配额打光不重试，切「仅用文档更新时间判定进行中」的降级路径（doc2 §5.4）
    CliErrorCode.QUOTA_EXCEEDED: CliAction.DEGRADE,
    CliErrorCode.AUTH_FAILED: CliAction.ALERT_NO_RETRY,
    CliErrorCode.PERMISSION_DENIED: CliAction.ALERT_NO_RETRY,
    # 文档被删 / 参数错：回业务层澄清，不是技术故障
    CliErrorCode.NOT_FOUND: CliAction.CLARIFY,
    CliErrorCode.INVALID_ARGUMENT: CliAction.CLARIFY,
    # 乐观锁冲突：重算 diff 后重试**一次**
    CliErrorCode.CONFLICT: CliAction.RECOMPUTE_RETRY_ONCE,
    # CLI 版本变化：告警并触发契约测试，不是重试能解决的
    CliErrorCode.OUTPUT_UNPARSABLE: CliAction.CONTRACT_TEST,
    CliErrorCode.OUTPUT_TOO_LARGE: CliAction.ALERT_NO_RETRY,
    # CLI 没装：端口按不可用装配，不是告警
    CliErrorCode.BINARY_MISSING: CliAction.SWITCH_IMPL,
    CliErrorCode.UNKNOWN: CliAction.ALERT_NO_RETRY,
}

# 走安全告警通道（AlertSink.security）的错误码。
# 整体方案把权限类错误定义为「疑似跨业务线越权」安全事件，不静默重试。
SECURITY_CODES: frozenset[CliErrorCode] = frozenset(
    {CliErrorCode.AUTH_FAILED, CliErrorCode.PERMISSION_DENIED}
)


def classify(stderr: str, stdout: str = "", exit_code: int | None = None) -> CliErrorCode:
    """把 CLI 输出归一成错误码。

    先看 stderr 再看 stdout——有些 CLI 把业务错误打在 stdout 的 JSON 里。
    都认不出来时按退出码兜底：非 0 即 ``UNKNOWN``。
    """
    for blob in (stderr or "", stdout or ""):
        if not blob:
            continue
        for pattern, code in _RULES:
            if pattern.search(blob):
                return code
    if exit_code not in (0, None):
        return CliErrorCode.UNKNOWN
    return CliErrorCode.OK


def is_retryable(code: CliErrorCode) -> bool:
    return code in RETRYABLE


def action_for(code: CliErrorCode) -> CliAction:
    return _ACTIONS.get(code, CliAction.ALERT_NO_RETRY)


def is_security_event(code: CliErrorCode) -> bool:
    return code in SECURITY_CODES


__all__ = [
    "RETRYABLE",
    "SECURITY_CODES",
    "CliAction",
    "action_for",
    "classify",
    "is_retryable",
    "is_security_event",
]
