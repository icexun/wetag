"""跨模块异常基类：IsolationViolation / PortUnavailable / ApprovalRequired。

``IsolationViolation`` 单独成类是为了 §9.2 的告警规则：DB 权限错误与 ``aibotid``
校验失败都要按「疑似跨业务线越权」的安全事件上报，**不能混进通用重试路径**。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID


class WeTagError(Exception):
    """本仓所有自定义异常的根。"""


class IsolationViolation(WeTagError):
    """疑似跨业务线越权（§9.2）。

    触发点：``aibotid`` 与进程 ``botId`` 不等、DB 权限错误、Repository 回读到
    他人 ``workspace_id`` 的行。

    处理纪律：按安全事件立即告警，**禁止静默重试**，禁止降级为普通错误。

    两种构造形式都接受：三参（``where`` / 期望值 / 实际值）用于能指出具体哪一环
    不等的校验点；单参用于等式涉及三方以上、单独列出期望值反而说不清的场景
    （如 ``event.aibotid == process.botId == workspace.botId``）。
    """

    _UNSET = object()

    def __init__(
        self,
        where: str,
        expected: object = _UNSET,
        actual: object = _UNSET,
    ) -> None:
        self.where = where
        if expected is self._UNSET and actual is self._UNSET:
            self.expected = None
            self.actual = None
            super().__init__(where)
            return
        self.expected = expected
        self.actual = actual
        super().__init__(f"isolation violation at {where}: expected {expected!r}, got {actual!r}")


class SpaceMismatchError(IsolationViolation):
    """§4.2 归属等式不成立：botId 或 workspaceId 与上下文不符。

    单列一类是为了让 Channel / Space 的校验失败能被单独 catch——它们发生在入口
    处、尚未触达 DB，与「Repository 回读到他人 workspace 的行」需要区分对待。
    """


class ChatOwnershipConflict(WeTagError):
    """该群已登记给另一条业务线（§3.2）。

    当前 Bot 不初始化私有记忆、不执行任务，只在群内提示保留一个业务线 Bot。
    跨模块可见是因为判定在 Space、提示在 Channel。
    """

    def __init__(self, chat_id: str, owner_workspace_id: str, current: str):
        super().__init__(
            f"chat {chat_id} belongs to workspace {owner_workspace_id}, "
            f"current workspace is {current}"
        )
        self.chat_id = chat_id
        self.owner_workspace_id = owner_workspace_id
        self.current_workspace_id = current


class PortUnavailable(WeTagError):
    """Integration Port 不可用。

    ``retryable`` 由 ``platform.cli.error_map`` 的归一化错误码决定，调用方据此
    决定退避重试还是立即告警；业务层不自行判断 stderr 文本。
    """

    def __init__(
        self,
        port: str,
        error_code: object = None,
        reason: str | None = None,
        *,
        retryable: bool = False,
    ) -> None:
        self.port = port
        self.error_code = error_code
        self.reason = reason
        self.retryable = retryable
        # 接线缺失这类内部错误只有一句话可说，此时 port 本身就是消息
        if error_code is None and reason is None:
            super().__init__(port)
        else:
            super().__init__(f"port {port} unavailable [{error_code}]: {reason}")


class PoolExhausted(PortUnavailable):
    """连接池耗尽（§9.3）。

    获取连接等待超过 ``PG_ACQUIRE_TIMEOUT_S`` 即抛出，调用方把任务回 ``PENDING``
    并告警，而不是无限等待——跨机 PG 下无限等待会把整个进程拖死。
    """


class ApprovalRequired(WeTagError):
    """高危写操作需要卡片确认（§4.6 / 裁决 #13）。

    今日范围内 Auto Mode 全通（§10.3），此异常保留给补做期的异步审批 handler。
    """

    def __init__(self, approval_id: UUID, reason: str | None = None) -> None:
        self.approval_id = approval_id
        self.reason = reason
        super().__init__(reason or f"approval required: {approval_id}")


class RateLimited(WeTagError):
    """外部限流：触发更长退避基数（US3-3）。"""

    def __init__(
        self, message: str = "rate limited", retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class StorageUnavailable(WeTagError):
    """存储不可达（连接级错误）。

    新请求显式拒绝，既有任务退回 PENDING 待恢复（US1-5/US1-6）——与
    ``PoolExhausted`` 的区别是后者库还活着、只是本进程的池排不上队。
    """


class MemoryUnavailable(WeTagError):
    """语义记忆服务不可用（无平台凭据 / 托管 collection 不可达）。

    显式降级——返回空并置 degraded 标记，任务不整体失败（US8-9）。
    """


def error_digest(exc: BaseException, max_len: int = 200) -> str:
    """异常摘要，供 task.error / audit_log 使用，不含堆栈与敏感上下文。"""
    text = f"{type(exc).__name__}: {exc}"
    return text[:max_len]


def payload_digest(payload: Any, max_len: int = 200) -> str:
    """参数摘要：只保留长度与首尾片段，不存明文凭据与完整正文（NFR-008）。"""
    text = repr(payload)
    if len(text) <= max_len:
        return text
    head = max_len // 2
    return f"{text[:head]}...<{len(text)} chars>...{text[-head:]}"


__all__ = [
    "WeTagError",
    "IsolationViolation",
    "ChatOwnershipConflict",
    "SpaceMismatchError",
    "PortUnavailable",
    "PoolExhausted",
    "ApprovalRequired",
    "RateLimited",
    "StorageUnavailable",
    "MemoryUnavailable",
    "error_digest",
    "payload_digest",
]
