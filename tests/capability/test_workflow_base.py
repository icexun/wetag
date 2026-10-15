"""Workflow 基座（``app/capability/workflows/base.py``）。

这层没有业务，只有纪律。它被三条链路共用，所以它的缺陷不会表现成"某个功能不对"，
而是**同时表现在三条链路上，还各自换一副面孔**——建卡少了一步、检测卡片没发出去、
文档更新报成功但什么也没改。

四条主张：

1. **顺序由声明决定。** 用 ReAct 自由规划，同一份输入这次是"读旧版本 → diff →
   校验 → 更新"，下次可能先更新再校验——对带外部写的链路，那等于把"先校验再落
   盘"这条保证直接取消。
2. **失败绝不退化成空的成功。** 空的成功结果会被渲染层说成"没有发现问题"。
3. **`execute` 自己永不开事务。** 它全程 await 外部调用，持一个事务就是整条链路
   期间占着连接——这正是 `Step.__post_init__` 在单步层面禁止的事。
4. **补偿永不抛异常。** 补偿失败覆盖掉原始失败原因，日志里就只剩"补偿时 XX 异常"，
   真正的根因丢了。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.capability.workflows.base import Step, StepOutcome, Workflow
from app.contracts.envelope import Envelope, MsgType
from app.contracts.ids import (
    BotId,
    ChatId,
    IdemKey,
    OrganizationId,
    TaskId,
    WorkspaceId,
)
from app.contracts.space_context import SpaceContext
from app.contracts.task import AgenticTask, WorkflowKind

WS = WorkspaceId("alpha")


def _context() -> SpaceContext:
    return SpaceContext(
        organization_id=OrganizationId("org1"),
        workspace_id=WS,
        bot_id=BotId("bot1"),
        chat_id=ChatId("chat1"),
    )


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _envelope(text: str = "hi") -> Envelope:
    return Envelope(chat_id="chat1", msg_type=MsgType.MARKDOWN, payload={"text": text})


def _task(name: str = "t1") -> AgenticTask:
    return AgenticTask(
        task_id=TaskId(name),
        kind=WorkflowKind.REQUIREMENT_UPDATE,
        context=_context(),
        payload={},
        idem_key=IdemKey(name),
    )


class Ctx:
    """链路上下文。step 往 ``trace`` 里记名字，断言顺序与补偿都看它。"""

    def __init__(self) -> None:
        self.trace: list[str] = []


def _wf(*steps: Step[Ctx], name: str = "wf") -> Workflow[Ctx]:
    class W(Workflow[Ctx]):
        pass

    W.name = name
    W.steps = lambda self, ctx: steps  # type: ignore[assignment,method-assign]
    return W()


def _step(
    name: str,
    outcome: Any = None,
    *,
    compensate: bool = False,
    compensate_boom: bool = False,
    **kw: Any,
) -> Step[Ctx]:
    async def run(ctx: Ctx) -> StepOutcome:
        ctx.trace.append(name)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome if outcome is not None else StepOutcome()

    async def undo(ctx: Ctx) -> None:
        ctx.trace.append(f"undo:{name}")
        if compensate_boom:
            raise RuntimeError(f"{name} 补偿失败")

    if compensate or compensate_boom:
        kw["compensate"] = undo
    return Step(name, run, **kw)


# ---------------------------------------------------------------------------
# 一、顺序由声明决定
# ---------------------------------------------------------------------------


async def test_steps_run_in_declaration_order() -> None:
    """声明的顺序就是执行的顺序，一个不落。"""
    ctx = Ctx()
    result = await _wf(_step("a"), _step("b"), _step("c")).execute(ctx)

    assert ctx.trace == ["a", "b", "c"]
    assert result.completed is True


async def test_a_failing_step_stops_the_rest() -> None:
    """失败即停。

    继续往后跑是最危险的选项：后面的步骤往往是外部写（建卡、改文档），而它们的
    前置条件已经不成立了。
    """
    ctx = Ctx()
    result = await _wf(_step("a"), _step("b", StepOutcome.fail("b 挂了")), _step("c")).execute(ctx)

    assert ctx.trace == ["a", "b"], "失败之后还在往下跑"
    assert result.completed is False
    assert result.incomplete_reason == "b 挂了"


async def test_an_exception_also_stops_the_rest() -> None:
    ctx = Ctx()
    result = await _wf(_step("a", RuntimeError("炸了")), _step("b")).execute(ctx)

    assert ctx.trace == ["a"]
    assert result.completed is False
    assert "RuntimeError" in (result.incomplete_reason or ""), "异常类型没留痕，事后无法排查"
    assert "炸了" in (result.incomplete_reason or "")
    assert result.metrics["failed_step"] == "a"


# ---------------------------------------------------------------------------
# 二、失败绝不退化成空的成功
# ---------------------------------------------------------------------------


async def test_a_failure_is_never_an_empty_success() -> None:
    """未完成必须带 ``completed=False`` 和一个原因。

    退化成一个空的成功结果，渲染层会把它说成"没有发现问题"——这是整个项目最不能
    出的那类错。
    """
    result = await _wf(_step("a", StepOutcome.fail("端口不可用"))).execute(Ctx())

    assert result.completed is False
    assert result.incomplete_reason == "端口不可用"


async def test_a_failure_without_a_reason_still_gets_one() -> None:
    """``fail()`` 时忘了写原因，也不能让 reason 是 None。

    ``incomplete_reason=None`` 加 ``completed=False`` 在渲染层就是一句"未完成"
    后面什么都没有，等于告诉用户"出了点事但我不说"。
    """
    result = await _wf(_step("a", StepOutcome(completed=False))).execute(Ctx())

    assert result.completed is False
    assert result.incomplete_reason and "a" in result.incomplete_reason


async def test_a_failure_still_carries_what_was_already_produced() -> None:
    """失败前已经产出的卡片、补偿任务、状态都要带出来。

    丢掉它们，"部分成功"就变成了"什么也没发生"：已经建好的卡没人知道，
    该入队的补偿任务永远不会跑——而外部系统里那半条记录还在。
    """
    env, comp = _envelope("已经建好的卡"), _task("补偿")
    result = await _wf(
        _step(
            "a",
            StepOutcome(
                envelopes=(env,),
                derived_tasks=(comp,),
                state_patch={"cursor": "7"},
                metrics={"created": "1001"},
            ),
        ),
        _step("b", StepOutcome.fail("b 挂了")),
    ).execute(Ctx())

    assert result.completed is False
    assert result.envelopes == [env], "卡片丢了"
    assert [t.idem_key for t in result.derived_tasks] == [comp.idem_key], "补偿任务丢了：再也不会入队"
    assert result.state_patch["cursor"] == "7", "状态丢了：台账停在原地"
    assert result.metrics["created"] == "1001"


async def test_an_exception_also_carries_what_was_already_produced() -> None:
    """异常路径与显式失败路径要一致——异常更容易被写成"直接返回一个裸的失败"。"""
    env, comp = _envelope(), _task()
    result = await _wf(
        _step("a", StepOutcome(envelopes=(env,), derived_tasks=(comp,), state_patch={"k": "v"})),
        _step("b", RuntimeError("炸了")),
    ).execute(Ctx())

    assert result.envelopes == [env]
    assert [t.idem_key for t in result.derived_tasks] == [comp.idem_key]
    assert result.state_patch == {"k": "v"}


# ---------------------------------------------------------------------------
# 三、跳过 ≠ 失败；非关键失败 ≠ 失败
# ---------------------------------------------------------------------------


async def test_a_skipped_step_does_not_block_the_chain() -> None:
    """"条件不满足所以没跑"不是失败。

    今日大量步骤会跳过（PG 未装配、幂等台账不可用、TAPD 不可写）。把跳过当失败，
    则今天没有任何一条链路能跑完——连能做的那部分也做不了。
    """
    ctx = Ctx()
    result = await _wf(
        _step("a", StepOutcome.skip("PG 未装配")), _step("b")
    ).execute(ctx)

    assert ctx.trace == ["a", "b"]
    assert result.completed is True
    assert "PG 未装配" in str(result.metrics["notes"]), "跳过的原因没留下，事后看不出少做了什么"


async def test_a_non_critical_failure_degrades_instead_of_stopping() -> None:
    """锦上添花的步骤失败只降级。

    影响分析是典型：它挂了不该让**已经算出来的结果**发不出去。但必须留下一条
    note——否则卡片看起来是完整的，而其实少了一块。
    """
    ctx = Ctx()
    result = await _wf(
        _step("impact", StepOutcome.fail("模型超时"), critical=False), _step("render")
    ).execute(ctx)

    assert ctx.trace == ["impact", "render"], "非关键步骤失败却停了整条链路"
    assert result.completed is True
    assert "impact 降级：模型超时" in str(result.metrics["notes"])


async def test_a_critical_failure_is_the_default() -> None:
    """``critical`` 默认为真：忘了写就按"会阻塞"处理。

    反过来默认成 False，任何一步失败都只会留一条 note，链路一路报成功——这正是
    "空的成功结果"最容易溜进来的方式。
    """
    assert Step("x", _step("x").run).critical is True


# ---------------------------------------------------------------------------
# 四、补偿：逆序、只补成功的、永不抛
# ---------------------------------------------------------------------------


async def test_compensation_runs_in_reverse_order() -> None:
    """逆序补偿。正序会先撤销最早那步，而后面那步可能还依赖它。"""
    ctx = Ctx()
    await _wf(
        _step("a", compensate=True),
        _step("b", compensate=True),
        _step("c", StepOutcome.fail("挂了")),
    ).execute(ctx)

    assert ctx.trace == ["a", "b", "c", "undo:b", "undo:a"]


async def test_only_completed_steps_are_compensated() -> None:
    """失败的那一步自己不补偿——它没做成，没什么可撤销的。

    补偿一个没做成的外部写，撤销的可能是别人的东西。
    """
    ctx = Ctx()
    await _wf(
        _step("a", compensate=True),
        _step("b", StepOutcome.fail("挂了"), compensate=True),
    ).execute(ctx)

    assert ctx.trace == ["a", "b", "undo:a"], f"补偿了没做成的那一步：{ctx.trace}"


async def test_a_skipped_step_is_not_compensated() -> None:
    """跳过的步骤也不补偿：它压根没动外部系统。"""
    ctx = Ctx()
    await _wf(
        _step("a", StepOutcome.skip("没装配"), compensate=True),
        _step("b", StepOutcome.fail("挂了")),
    ).execute(ctx)

    assert "undo:a" not in ctx.trace


async def test_compensation_runs_after_an_exception_too() -> None:
    ctx = Ctx()
    await _wf(_step("a", compensate=True), _step("b", RuntimeError("炸了"))).execute(ctx)

    assert ctx.trace == ["a", "b", "undo:a"]


async def test_a_failing_compensation_never_hides_the_real_reason() -> None:
    """补偿失败不能覆盖原始失败原因。

    覆盖掉之后日志里只剩"补偿时 XX 异常"，真正的根因丢了——而根因才是要修的那个。
    """
    ctx = Ctx()
    result = await _wf(
        _step("a", compensate_boom=True),
        _step("b", StepOutcome.fail("真正的原因")),
    ).execute(ctx)

    assert result.incomplete_reason == "真正的原因", "补偿的异常盖住了根因"
    assert "undo:a" in ctx.trace


async def test_a_failing_compensation_does_not_stop_the_others() -> None:
    """一个补偿挂了，剩下的照样跑——否则最早那步的外部写永远撤不掉。"""
    ctx = Ctx()
    await _wf(
        _step("a", compensate=True),
        _step("b", compensate_boom=True),
        _step("c", StepOutcome.fail("挂了")),
    ).execute(ctx)

    assert "undo:a" in ctx.trace, f"前一个补偿被后一个的异常带走了：{ctx.trace}"


async def test_a_successful_chain_compensates_nothing() -> None:
    ctx = Ctx()
    await _wf(_step("a", compensate=True), _step("b", compensate=True)).execute(ctx)

    assert ctx.trace == ["a", "b"]


async def test_steps_without_a_compensation_make_no_noise(
    caplog: pytest.LogCaptureFixture
) -> None:
    """没声明 ``compensate`` 的步骤直接跳过，不能"试着调一下再吞异常"。

    吞掉的异常仍然会打一条 ERROR。一条 6 步的链路失败时就是 5 条假的"补偿失败"
    堆栈，而其中唯一那条真的补偿失败被埋在里面——正是本模块最开头要避免的
    "真正的根因丢了"，只是换到了日志这一侧。
    """
    caplog.set_level("INFO")
    ctx = Ctx()
    await _wf(
        _step("a"),  # 无补偿
        _step("b"),  # 无补偿
        _step("c", compensate=True),
        _step("d", StepOutcome.fail("挂了")),
    ).execute(ctx)

    assert ctx.trace.count("undo:c") == 1, "该补偿的那步没补"
    failures = [r for r in caplog.records if r.levelname == "ERROR"]
    assert not failures, f"给没声明补偿的步骤打了假的失败堆栈：{[r.message for r in failures]}"


# ---------------------------------------------------------------------------
# 五、短事务纪律的装配期保证
# ---------------------------------------------------------------------------


def test_a_step_cannot_both_open_a_tx_and_call_out() -> None:
    """``opens_tx`` 与 ``calls_external`` 同时为真，**装配期**就报错。

    留到运行期检查等于不检查："事务里套了个 LLM 调用"往往只在压测时暴露，而那时
    它已经上线了。表现也不像 bug：对端变慢 → 事务持有时间变长 → 连接池耗干 →
    一次外部抖动被放大成全局故障。
    """
    async def run(ctx: Ctx) -> StepOutcome:
        return StepOutcome()

    with pytest.raises(ValueError) as e:
        Step("bad", run, opens_tx=True, calls_external=True)

    msg = str(e.value)
    assert "bad" in msg, "报错里没说是哪个 step"
    assert "连接池" in msg, "报错没说清后果，下一个人会以为这只是个风格约束"


@pytest.mark.parametrize(
    ("tx", "ext"), [(True, False), (False, True), (False, False)]
)
def test_the_other_three_combinations_are_fine(tx: bool, ext: bool) -> None:
    """只有"同时为真"被禁。开事务的短 step 和调外部的 step 各自都是正常的。"""
    async def run(ctx: Ctx) -> StepOutcome:
        return StepOutcome()

    step = Step("ok", run, opens_tx=tx, calls_external=ext)
    assert step.opens_tx is tx and step.calls_external is ext


def test_a_step_is_immutable() -> None:
    """声明位不可改：能在运行期把 ``opens_tx`` 改掉，装配期那道检查就形同虚设。"""
    step = _step("a")
    with pytest.raises(Exception):
        step.opens_tx = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 六、结果收敛
# ---------------------------------------------------------------------------


async def test_outputs_from_all_steps_are_accumulated() -> None:
    """每一步的产出都累加，不是最后一步说了算。"""
    e1, e2 = _envelope("一"), _envelope("二")
    t1, t2 = _task("t1"), _task("t2")
    result = await _wf(
        _step("a", StepOutcome(envelopes=(e1,), derived_tasks=(t1,),
                               state_patch={"a": 1}, metrics={"ma": 1})),
        _step("b", StepOutcome(envelopes=(e2,), derived_tasks=(t2,),
                               state_patch={"b": 2}, metrics={"mb": 2})),
    ).execute(Ctx())

    assert result.envelopes == [e1, e2]
    assert [t.idem_key for t in result.derived_tasks] == [t1.idem_key, t2.idem_key]
    assert result.state_patch == {"a": 1, "b": 2}
    assert result.metrics["ma"] == 1 and result.metrics["mb"] == 2


async def test_each_step_is_timed() -> None:
    """每步耗时单独记：链路慢下来时要看得出是哪一步慢。"""
    result = await _wf(_step("a"), _step("b")).execute(Ctx())

    assert "step.a.ms" in result.metrics and "step.b.ms" in result.metrics
    assert "workflow.ms" in result.metrics


async def test_the_failed_step_is_named_in_the_metrics() -> None:
    """哪一步挂的要落在 metrics 里——reason 是给人看的，这个是给聚合看的。"""
    result = await _wf(_step("a"), _step("b", StepOutcome.fail("挂了"))).execute(Ctx())

    assert result.metrics["failed_step"] == "b"


async def test_a_clean_run_leaves_no_notes_key() -> None:
    """一切正常时不塞一个空的 notes——空列表在卡片上会渲染出一个空的"说明"区块。"""
    result = await _wf(_step("a")).execute(Ctx())

    assert "notes" not in result.metrics


async def test_an_empty_chain_is_a_success_with_nothing_in_it() -> None:
    result = await _wf().execute(Ctx())

    assert result.completed is True
    assert result.envelopes == [] and result.derived_tasks == []


async def test_the_space_context_is_only_used_for_logging() -> None:
    """``context`` 只进日志字段，不参与链路逻辑。

    基座不该碰隔离判定——归属校验在 router 入口一次做完（那里有测试）。这里再做
    一次，两处规则就会漂移。
    """
    calls: list[int] = []

    class Spy(SpaceContext):  # type: ignore[misc]
        def log_fields(self) -> dict[str, Any]:
            calls.append(1)
            return {"ws": "alpha"}

    ctx = Ctx()
    result = await _wf(_step("a")).execute(
        ctx,
        context=Spy(
            organization_id=OrganizationId("org1"),
            workspace_id=WS,
            bot_id=BotId("bot1"),
            chat_id=ChatId("chat1"),
        ),
    )

    assert result.completed is True
    assert calls, "context 压根没被用"


async def test_no_context_is_fine() -> None:
    """``context=None`` 也能跑——单测与离线回放都走这条路。"""
    result = await _wf(_step("a")).execute(Ctx(), context=None)
    assert result.completed is True
