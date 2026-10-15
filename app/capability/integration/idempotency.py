"""外部写操作的幂等护栏（doc2 §5.2 / §4.6-1）。

``CapabilityRepository`` 已经提供了 ``claim`` / ``settle`` / ``lookup`` 三条 SQL。
本模块提供**使用纪律**——因为这三条单独用起来太容易用错，而用错的后果是用户看到
两张一样的 TAPD 卡片，或者一条永远卡在 in_flight 再也不敢重试的记录。

核心是 ``guard()`` 的三段结构，每段一个独立短事务，**外部调用在事务之外**：

    tx1: claim           ← 抢占，拿到就出事务
    ——  外部写（TAPD / 企微 / 飞书），无事务持有 ——
    tx2: settle          ← 记录结果

为什么必须这样：一次 TAPD 建卡是网络往返，p99 可能好几秒。把它包在事务里，这个
连接就在整个往返期间被占着不放，而连接池只有那么大；对端一慢，池被耗干，**其他
所有业务线的所有操作**一起卡住——一次外部抖动被放大成全局故障。
``idle_in_transaction_session_timeout=15s`` 是最后一道自杀开关，但那意味着事务被
强杀、settle 写不进去、幂等记录停在 in_flight。所以纪律必须在上层就守住。

## 崩溃后 in_flight 的处理（R10 已知缺口，刻意保留）

claim 成功后进程崩溃，记录停在 ``in_flight``。本模块的处理是：在 ``expires_at``
之前**拒绝重试**，抛 ``IdempotencyConflict`` 让上层报"上一次执行结果未知，请人工
确认"。这比两种替代做法都好：

- 直接重试 → 可能建出第二张卡。TAPD 侧没有幂等键可以让我们安全重试。
- 直接放弃 → 一条需求永远建不出来，而且没人知道为什么。

"重试前回查外部对象实际状态"是正确解法，但依赖各 CLI 的查询能力（TAPD 侧今日
连写都还没跑通），记为已知缺口。``stale_in_flight()`` 供运维盘点这类记录。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, TypeVar

from app.capability.repository import CapabilityRepository
from app.contracts.ids import IdemKey, WorkspaceId
from app.platform.db.pool import Database

logger = logging.getLogger(__name__)

T = TypeVar("T")


class IdempotencyConflict(RuntimeError):
    """幂等键被占用且结果未知——**不可自动重试**。

    与"上次失败了可以重试"是两件不同的事，所以是独立异常而不是返回值：
    返回值容易被 ``if not result: retry()`` 这类写法吞掉。
    """

    def __init__(self, key: str, status: str, attempt: int) -> None:
        self.key = key
        self.status = status
        self.attempt = attempt
        super().__init__(
            f"幂等键 {key} 处于 {status}（第 {attempt} 次尝试），"
            "上一次执行结果未知，需人工确认后才能重试"
        )


@dataclass(frozen=True, slots=True)
class GuardOutcome:
    """一次受保护执行的结果。

    ``replayed=True`` 表示这次没有真正调用外部系统，``value`` 来自上次成功的
    记录。调用方据此决定文案："已创建" 还是 "此前已创建"。
    """

    value: Any
    replayed: bool = False
    attempt: int = 1


def make_key(
    workspace_id: WorkspaceId,
    operation: str,
    *,
    parts: Mapping[str, Any] | None = None,
) -> IdemKey:
    """构造幂等键。

    键必须**只由业务标识决定**，不含时间戳、不含 trace_id、不含重试次数——否则
    每次重试都是一个新键，幂等表形同虚设。补偿任务复用同一个键正是靠这点
    （计划 §阶段 5：requirement_create 的补偿任务复用同一幂等键）。

    ``parts`` 走排序后的 JSON 再 hash，保证字典顺序不影响键。
    """
    if not operation.strip():
        raise ValueError("operation 不得为空")
    digest = ""
    if parts:
        blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
        digest = ":" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]
    return IdemKey(f"{workspace_id}:{operation}{digest}")


class Idempotency:
    """幂等护栏。``guard()`` 是唯一入口。"""

    def __init__(self, db: Database, repo: CapabilityRepository) -> None:
        self._db = db
        self._repo = repo

    async def guard(
        self,
        key: IdemKey,
        operation: str,
        action: Callable[[], Awaitable[T]],
        *,
        to_record: Callable[[T], dict[str, Any]] | None = None,
        from_record: Callable[[Mapping[str, Any]], T] | None = None,
    ) -> GuardOutcome:
        """在幂等保护下执行一次外部写。

        :param action: 真正的外部调用。**在事务之外**被 await（见模块 docstring）。
        :param to_record: 把结果转成可落库的 dict（如 ``{"tapd_id": "..."}``）。
            落库的是**引用与标识，不是原文**（§8：文档原文与代码原文一律不落库）。
        :param from_record: 重放时把 ``result`` jsonb 还原成返回值。不给则直接
            返回 dict。
        :raises IdempotencyConflict: 键被占用且结果未知。
        """
        # --- tx1: 抢占 ---
        async with self._db.tx() as conn:
            claimed, record = await self._repo.claim(
                conn, key=str(key), operation=operation
            )

        if not claimed:
            return self._replay(key, record, from_record)

        attempt = int(record["attempt"]) if record is not None else 1

        # --- 外部调用：无事务持有 ---
        try:
            value = await action()
        except Exception as exc:  # noqa: BLE001
            # 失败也要落库：attempt 递增、error_code 留痕。
            # 用独立事务而不是复用上面那个——上面那个早已提交，而且异常路径上
            # 更不该假设连接还健康。
            async with self._db.tx() as conn:
                await self._repo.settle(
                    conn,
                    key=str(key),
                    status="failed",
                    error_code=type(exc).__name__,
                )
            logger.warning(
                "幂等操作失败 key=%s operation=%s：%s", key, operation, exc
            )
            raise

        # --- tx2: 记账 ---
        payload = to_record(value) if to_record is not None else None
        async with self._db.tx() as conn:
            await self._repo.settle(
                conn, key=str(key), status="succeeded", result=payload
            )
        return GuardOutcome(value=value, replayed=False, attempt=attempt)

    async def peek(self, key: IdemKey) -> Mapping[str, Any] | None:
        """只读查询。给"这张卡是不是已经建过了"这类判断用，不抢占。"""
        row = await self._repo.lookup(str(key))
        return dict(row) if row is not None else None

    # -- 内部 -------------------------------------------------------------
    @staticmethod
    def _replay(
        key: IdemKey,
        record: Mapping[str, Any] | None,
        from_record: Callable[[Mapping[str, Any]], Any] | None,
    ) -> GuardOutcome:
        if record is None:
            # claim 说键已存在，回读却查不到。并发窗口（另一进程刚 claim 又回滚）
            # 或跨业务线键冲突。两种都不该猜，交人工。
            raise IdempotencyConflict(str(key), "unknown", 0)

        status = str(record.get("status") or "")
        attempt = int(record.get("attempt") or 1)

        if status == "succeeded":
            raw = record.get("result")
            data = _as_mapping(raw)
            value = from_record(data) if from_record is not None and data else data
            logger.info("幂等重放 key=%s，复用上次结果", key)
            return GuardOutcome(value=value, replayed=True, attempt=attempt)

        if status == "failed":
            expired = _expired(record)
            if expired:
                # 过期的失败记录：允许重来。这是唯一的自动重试通路，
                # 且只对**已知失败**开放——in_flight 不走这里。
                logger.info("幂等键 %s 的失败记录已过期，允许重试", key)
                raise _RetryAllowed(str(key), attempt)
            raise IdempotencyConflict(str(key), "failed", attempt)

        # in_flight：结果未知。见模块 docstring 的 R10 说明。
        raise IdempotencyConflict(str(key), status or "in_flight", attempt)


class _RetryAllowed(IdempotencyConflict):
    """内部信号：失败记录已过期，上层可以换一个键重来。

    仍然继承 ``IdempotencyConflict``——默认行为是"不要自动重试"，只有明确
    ``except _RetryAllowed`` 的调用方才会走重试路径。默认安全。
    """

    def __init__(self, key: str, attempt: int) -> None:
        super().__init__(key, "failed(expired)", attempt)


def _as_mapping(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, (str, bytes)):
        try:
            loaded = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _expired(record: Mapping[str, Any]) -> bool:
    from datetime import datetime, timezone

    exp = record.get("expires_at")
    if not isinstance(exp, datetime):
        return False
    now = datetime.now(timezone.utc) if exp.tzinfo else datetime.now()
    return exp < now


__all__ = [
    "GuardOutcome",
    "Idempotency",
    "IdempotencyConflict",
    "make_key",
]
