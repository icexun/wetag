"""外部写操作的幂等护栏（``app/capability/integration/idempotency.py``）。

这一层的三条主张，各对应一种线上事故：

1. **外部调用在事务之外。** 一次 TAPD 建卡是网络往返，p99 好几秒。包在事务里就等于
   在整个往返期间攥着一个池连接；对端一慢，池被耗干，**其他所有业务线的所有操作**
   一起卡住——一次外部抖动被放大成全局故障。``idle_in_transaction_session_timeout``
   是最后的自杀开关，但它触发时事务被强杀、settle 写不进去，幂等记录停在 in_flight。
   所以纪律必须在这一层守住，没有第二道。
2. **键只由业务标识决定。** 掺一个时间戳或 trace_id 进去，每次重试都是新键，幂等表
   形同虚设——用户看到两张一样的 TAPD 卡片，而所有日志都显示"幂等生效"。
3. **结果未知一律不自动重试。** TAPD 侧没有幂等键可以让我们安全重放，所以宁可抛
   ``IdempotencyConflict`` 让人确认，也不猜。唯一的自动通路是"已过期的**已知失败**"。
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.capability.integration.idempotency import (
    GuardOutcome,
    Idempotency,
    IdempotencyConflict,
    _RetryAllowed,
    make_key,
)
from app.contracts.ids import WorkspaceId


class FakeDB:
    """记录事务进出。``events`` 是"外部调用在事务之外"唯一的证明方式。"""

    def __init__(self) -> None:
        self.depth = 0
        self.max_depth = 0
        self.tx_count = 0
        self.events: list[str] = []

    @asynccontextmanager
    async def tx(self) -> Any:
        self.depth += 1
        self.tx_count += 1
        self.max_depth = max(self.max_depth, self.depth)
        self.events.append("tx_enter")
        try:
            yield object()
        finally:
            self.depth -= 1
            self.events.append("tx_exit")


class FakeRepo:
    def __init__(self, existing: Any = None) -> None:
        self.existing = existing
        self.settled: list[tuple[str, Any, Any]] = []
        self.claims: list[tuple[str, str]] = []

    async def claim(self, conn: Any, *, key: str, operation: str) -> tuple[bool, Any]:
        self.claims.append((key, operation))
        if self.existing is None:
            return True, {"attempt": 1}
        return False, self.existing

    async def settle(
        self, conn: Any, *, key: str, status: str, result: Any = None, error_code: Any = None
    ) -> None:
        self.settled.append((status, result, error_code))

    async def lookup(self, key: str) -> Any:
        return self.existing


def _idem(existing: Any = None) -> tuple[Idempotency, FakeDB, FakeRepo]:
    db, repo = FakeDB(), FakeRepo(existing)
    return Idempotency(db, repo), db, repo  # type: ignore[arg-type]


def _record(status: str, **kw: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {"status": status, "attempt": 2}
    rec.setdefault("expires_at", datetime.now(UTC) + timedelta(days=7))
    rec.update(kw)
    return rec


KEY = make_key(WorkspaceId("alpha"), "create_story")


# ---------------------------------------------------------------------------
# 一、幂等键
# ---------------------------------------------------------------------------


def test_the_same_business_identity_yields_the_same_key() -> None:
    """同一业务标识两次调用给出同一个键——补偿任务复用同一键就靠这点。"""
    a = make_key(WorkspaceId("alpha"), "create_story", parts={"topic": "t1"})
    b = make_key(WorkspaceId("alpha"), "create_story", parts={"topic": "t1"})

    assert a == b


def test_dictionary_order_does_not_change_the_key() -> None:
    """``parts`` 的书写顺序不影响键。

    不排序的话，同一次操作在两处代码里因为字面量顺序不同就成了两个键，于是重试变成
    重复建卡。而这种差异在 code review 里几乎看不出来。
    """
    a = make_key(WorkspaceId("alpha"), "op", parts={"a": 1, "b": 2})
    b = make_key(WorkspaceId("alpha"), "op", parts={"b": 2, "a": 1})

    assert a == b


def test_different_business_identities_yield_different_keys() -> None:
    assert make_key(WorkspaceId("alpha"), "op", parts={"topic": "t1"}) != make_key(
        WorkspaceId("alpha"), "op", parts={"topic": "t2"}
    )


def test_the_workspace_is_part_of_the_key() -> None:
    """跨业务线不能撞键。撞了就是一条业务线的重试读到另一条的结果。"""
    assert make_key(WorkspaceId("alpha"), "op") != make_key(WorkspaceId("beta"), "op")


def test_the_operation_is_part_of_the_key() -> None:
    assert make_key(WorkspaceId("alpha"), "create") != make_key(WorkspaceId("alpha"), "update")


@pytest.mark.parametrize("bad", ["", "   ", "\n"])
def test_an_empty_operation_is_refused(bad: str) -> None:
    """空 operation 会让所有操作共用一个键——宁可现在就炸。"""
    with pytest.raises(ValueError):
        make_key(WorkspaceId("alpha"), bad)


def test_the_digest_has_room_for_many_distinct_objects() -> None:
    """两百个不同的业务对象必须得到两百个不同的键。

    摘要截太短就是"不同需求撞成同一个键"：第二条需求的 guard 会命中第一条的 succeeded
    记录，直接重放——用户看到"此前已创建"并被指到**别人的**那张 TAPD 卡片上，而链路里
    每一步都显示成功。一个群里一天几十个话题，两位十六进制（256 个桶）撞上是必然。
    """
    keys = {
        make_key(WorkspaceId("alpha"), "create_story", parts={"topic": f"t{i}"})
        for i in range(200)
    }

    assert len(keys) == 200


def test_no_parts_means_no_digest_suffix() -> None:
    """不带 parts 时键是可读的，运维能直接照着查表。"""
    assert make_key(WorkspaceId("alpha"), "poll_docs") == "alpha:poll_docs"


# ---------------------------------------------------------------------------
# 二、外部调用必须在事务之外
# ---------------------------------------------------------------------------


async def test_the_external_call_happens_with_no_transaction_held() -> None:
    """三段结构：tx1 抢占 → 无事务的外部调用 → tx2 记账。

    ``events`` 的顺序就是短事务纪律本身。断言事件序而不只断言 ``depth==0``，是因为
    后者挡不住"把 action 挪到 tx1 里、再开一个空 tx"这类改法。
    """
    idem, db, repo = _idem()

    async def action() -> dict[str, str]:
        db.events.append(f"ACTION(depth={db.depth})")
        return {"tapd_id": "1001"}

    out = await idem.guard(KEY, "create_story", action, to_record=lambda v: dict(v))

    assert db.events == ["tx_enter", "tx_exit", "ACTION(depth=0)", "tx_enter", "tx_exit"]
    assert db.tx_count == 2
    assert db.max_depth == 1, "事务嵌套了"
    assert out == GuardOutcome(value={"tapd_id": "1001"}, replayed=False, attempt=1)
    assert repo.settled == [("succeeded", {"tapd_id": "1001"}, None)]


async def test_a_failing_external_call_is_also_settled_outside_its_transaction() -> None:
    """失败路径同样不能把外部调用圈进事务，而且必须落 failed 留痕。

    不落痕的话 attempt 永远停在 1，"这个键试过几次了"这个信息没了，R10 的人工确认
    就没有依据。
    """
    idem, db, repo = _idem()

    async def boom() -> None:
        db.events.append(f"BOOM(depth={db.depth})")
        raise TimeoutError("对端超时")

    with pytest.raises(TimeoutError):
        await idem.guard(KEY, "create_story", boom)

    assert db.events == ["tx_enter", "tx_exit", "BOOM(depth=0)", "tx_enter", "tx_exit"]
    assert repo.settled == [("failed", None, "TimeoutError")]


async def test_the_original_exception_is_not_swallowed() -> None:
    """原异常原样抛出。

    包装成别的异常（或吞掉返回 None）会让上层的错误分类失效：``TimeoutError`` 可重试，
    ``PermissionError`` 不可，两者被抹平后要么白重试要么白放弃。
    """
    idem, _db, _repo = _idem()

    async def boom() -> None:
        raise PermissionError("没权限")

    with pytest.raises(PermissionError, match="没权限"):
        await idem.guard(KEY, "op", boom)


async def test_the_error_code_recorded_is_the_exception_type() -> None:
    idem, _db, repo = _idem()

    class TapdRejected(RuntimeError):
        pass

    async def boom() -> None:
        raise TapdRejected()

    with pytest.raises(TapdRejected):
        await idem.guard(KEY, "op", boom)

    assert repo.settled[0][2] == "TapdRejected"


async def test_a_failure_leaves_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    """失败要有日志，且带上键与 operation。

    静默失败的症状是"这条需求建不出来且没人知道为什么"。异常会被上层转成群内文案，
    但文案里不会有幂等键——没有这条日志，运维手上就没有任何能拿去查表的东西。
    """
    idem, _db, _repo = _idem()

    async def boom() -> None:
        raise TimeoutError()

    with caplog.at_level("WARNING"):
        with pytest.raises(TimeoutError):
            await idem.guard(KEY, "create_story", boom)

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any(str(KEY) in m and "create_story" in m for m in warnings), (
        f"失败了没留下能查表的痕迹：{warnings}"
    )


async def test_nothing_is_recorded_when_there_is_nothing_to_record() -> None:
    """不给 ``to_record`` 就不落 payload。

    §8：文档原文与代码原文一律不落库。默认落整个返回值会把原文带进表里。
    """
    idem, _db, repo = _idem()

    async def action() -> str:
        return "一大段文档原文……"

    await idem.guard(KEY, "op", action)

    assert repo.settled == [("succeeded", None, None)]


async def test_a_reclaim_reports_which_attempt_this_is() -> None:
    """重新抢占到的键要带出真实的 attempt。

    过期失败记录被重新 claim 时 attempt 会递增。永远报第 1 次的话，"这条需求已经试了
    三轮都没成"这个事实就消失了——外部系统一直在拒绝，而我们的每条日志都写着"第一次
    尝试"，于是没人会去查对端。
    """
    db, repo = FakeDB(), FakeRepo()

    async def claim(conn: Any, *, key: str, operation: str) -> tuple[bool, Any]:
        return True, {"attempt": 3}

    repo.claim = claim  # type: ignore[method-assign]

    async def action() -> int:
        return 1

    out = await Idempotency(db, repo).guard(KEY, "op", action)  # type: ignore[arg-type]

    assert out.attempt == 3
    assert out.replayed is False


async def test_the_key_and_operation_reach_the_repository() -> None:
    idem, _db, repo = _idem()

    async def action() -> int:
        return 1

    await idem.guard(KEY, "create_story", action)

    assert repo.claims == [(str(KEY), "create_story")]


# ---------------------------------------------------------------------------
# 三、重放
# ---------------------------------------------------------------------------


async def test_a_succeeded_record_replays_without_calling_out() -> None:
    """已成功的键不再调外部系统，返回上次的结果。"""
    called = False
    idem, _db, _repo = _idem(_record("succeeded", result={"tapd_id": "999"}))

    async def never() -> None:
        nonlocal called
        called = True

    out = await idem.guard(KEY, "op", never, from_record=lambda d: d["tapd_id"])

    assert out.replayed is True
    assert out.value == "999"
    assert called is False, "重放路径调了外部系统——这就是第二张卡片"


async def test_a_replay_does_not_settle_again() -> None:
    """重放不写库。再 settle 一次会把 attempt 推高，看上去像又试了一次。"""
    idem, db, repo = _idem(_record("succeeded", result={"tapd_id": "999"}))

    async def never() -> None:
        raise AssertionError("不该被调用")

    await idem.guard(KEY, "op", never)

    assert repo.settled == []


async def test_the_replayed_attempt_is_the_recorded_one() -> None:
    idem, _db, _repo = _idem(_record("succeeded", attempt=4, result={"a": 1}))

    async def never() -> None:
        raise AssertionError

    assert (await idem.guard(KEY, "op", never)).attempt == 4


async def test_a_json_text_result_is_still_replayable() -> None:
    """``result`` 有时回来是 jsonb 的文本形态。当不成 mapping 会让重放静默变成空结果。"""
    idem, _db, _repo = _idem(_record("succeeded", result=json.dumps({"tapd_id": "777"})))

    async def never() -> None:
        raise AssertionError

    assert (await idem.guard(KEY, "op", never, from_record=lambda d: d["tapd_id"])).value == "777"


@pytest.mark.parametrize("raw", [None, "不是 JSON", b"\xff\xfe", "[1, 2]", 42])
async def test_an_unreadable_result_replays_as_empty_not_as_a_crash(raw: Any) -> None:
    """读不懂的 ``result`` 退化成空 mapping。

    这里不能抛：抛了就等于"上次明明成功了，现在因为一条读不出来的记录而彻底卡死"。
    返回空让上层走"此前已创建、详情未知"的文案。
    """
    idem, _db, _repo = _idem(_record("succeeded", result=raw))

    async def never() -> None:
        raise AssertionError

    out = await idem.guard(KEY, "op", never)

    assert out.replayed is True
    assert out.value == {}


async def test_an_empty_result_does_not_go_through_from_record() -> None:
    """空结果不喂给 ``from_record``——它通常会直接 ``d["tapd_id"]`` KeyError。"""
    idem, _db, _repo = _idem(_record("succeeded", result=None))

    async def never() -> None:
        raise AssertionError

    def strict(d: Any) -> str:
        return d["tapd_id"]

    assert (await idem.guard(KEY, "op", never, from_record=strict)).value == {}


# ---------------------------------------------------------------------------
# 四、结果未知 → 不自动重试
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["in_flight", "failed", "", "weird"])
async def test_an_unfinished_record_refuses_to_retry(status: str) -> None:
    """in_flight / 未过期的 failed / 认不出的状态，一律拒绝。

    默认拒绝而不是默认重试：TAPD 侧没有幂等键能让我们安全重放，猜错的代价是用户看到
    两张一样的卡片。认不出的状态也走这条——"不认识"绝不能落到宽松那侧。
    """
    called = False
    idem, _db, repo = _idem(_record(status))

    async def never() -> None:
        nonlocal called
        called = True

    with pytest.raises(IdempotencyConflict):
        await idem.guard(KEY, "op", never)

    assert called is False, "被拒绝的路径调用了外部系统"
    assert repo.settled == []


async def test_a_claim_that_lost_but_reads_back_nothing_is_a_conflict() -> None:
    """claim 说键已存在、回读却查不到：并发窗口或跨业务线撞键。两种都不猜。"""
    db, repo = FakeDB(), FakeRepo(existing={})
    repo.existing = None  # claim 返回 (False, None)

    async def claim(conn: Any, *, key: str, operation: str) -> tuple[bool, Any]:
        return False, None

    repo.claim = claim  # type: ignore[method-assign]

    async def never() -> None:
        raise AssertionError

    with pytest.raises(IdempotencyConflict) as e:
        await Idempotency(db, repo).guard(KEY, "op", never)  # type: ignore[arg-type]

    assert e.value.status == "unknown"


async def test_an_expired_failure_is_the_only_automatic_retry_path() -> None:
    """过期的**已知失败**可以重来，而且信号仍是 ``IdempotencyConflict`` 的子类。

    子类化是有意的：默认行为是"不要自动重试"，只有明确 ``except _RetryAllowed`` 的
    调用方才走重试。反过来做（独立异常 + 默认重试）会让每一个忘了处理的调用点都变成
    重复建卡的入口。
    """
    idem, _db, _repo = _idem(
        _record("failed", attempt=3, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )

    async def never() -> None:
        raise AssertionError

    with pytest.raises(_RetryAllowed) as e:
        await idem.guard(KEY, "op", never)

    assert isinstance(e.value, IdempotencyConflict)
    assert e.value.attempt == 3


async def test_an_expired_in_flight_still_refuses() -> None:
    """过期也救不了 in_flight——"结果未知"不会因为时间流逝变成"已知失败"。"""
    idem, _db, _repo = _idem(
        _record("in_flight", expires_at=datetime.now(UTC) - timedelta(days=1))
    )

    async def never() -> None:
        raise AssertionError

    with pytest.raises(IdempotencyConflict) as e:
        await idem.guard(KEY, "op", never)

    assert not isinstance(e.value, _RetryAllowed)


@pytest.mark.parametrize("exp", [None, "2020-01-01", 0])
async def test_an_unparseable_expiry_is_treated_as_not_expired(exp: Any) -> None:
    """读不出过期时间就当没过期。失效方向必须朝"拒绝"那侧。"""
    idem, _db, _repo = _idem(_record("failed", expires_at=exp))

    async def never() -> None:
        raise AssertionError

    with pytest.raises(IdempotencyConflict) as e:
        await idem.guard(KEY, "op", never)

    assert not isinstance(e.value, _RetryAllowed)


def test_the_conflict_message_names_the_key_and_says_what_to_do() -> None:
    """异常文案要能直接抄进群里：哪个键、什么状态、第几次、该干什么。"""
    exc = IdempotencyConflict("alpha:create_story", "in_flight", 2)

    text = str(exc)
    assert "alpha:create_story" in text
    assert "in_flight" in text
    assert "人工" in text


# ---------------------------------------------------------------------------
# 五、只读查询
# ---------------------------------------------------------------------------


async def test_peek_does_not_claim() -> None:
    """``peek`` 只读。它要是抢占了，一次"查查建过没"就把键占掉了。"""
    idem, db, repo = _idem(_record("succeeded", result={"tapd_id": "1"}))

    row = await idem.peek(KEY)

    assert row is not None and row["status"] == "succeeded"
    assert repo.claims == []
    assert db.tx_count == 0


async def test_peek_returns_none_for_an_unknown_key() -> None:
    idem, _db, _repo = _idem()

    assert await idem.peek(KEY) is None
