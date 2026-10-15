"""模型与 Agent 装配（``app/capability/agents/factory.py``）。

两条纪律，两种"失效了但看不出来"：

1. **全进程共享一个 LLM 信号量。** D-2 是一业务线一进程。散落各处各建一个等于没有
   上限——真实后果是并发打爆网关额度后**所有**语义节点一起失败，于是所有检测都变成
   ``completed=False``，能力整体下线。这种失效在单元测试里永远不出现，只在压测和线上
   出现，所以只能在这一层把"是同一个对象"钉住。
2. **语义判断 temperature=0。** 同一份输入两次运行给出不同结论，就没法做 §10.3 的
   精确率抽样，也没法向用户解释"为什么昨天报了今天不报"。而这条是靠改 AgentScope
   模型对象的 ``generate_kwargs`` 实现的——属性名一变就静默失效。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.capability.agents import factory


class _Props:
    def __init__(self, limit: Any) -> None:
        self.llm_max_concurrency = limit
        self.deterministic_temperature = 0


@pytest.fixture(autouse=True)
def _reset() -> Any:
    """信号量是模块级全局，测试之间必须清掉。"""
    factory._llm_semaphore = None
    yield
    factory._llm_semaphore = None


def _with_limit(monkeypatch: pytest.MonkeyPatch, limit: Any) -> None:
    monkeypatch.setattr(
        factory.CapabilityProperties, "instance", classmethod(lambda cls: _Props(limit))
    )


# ---------------------------------------------------------------------------
# 一、共享一个闸门
# ---------------------------------------------------------------------------


def test_the_semaphore_is_one_object_for_the_whole_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """每次调用返回同一个对象。

    各处各建一个的话，四个语义节点每个都能跑 4 并发，实际上限是 16——配置项写着 4，
    额度按 16 烧。而且这个错只在并发真的起来时才发作。
    """
    _with_limit(monkeypatch, 4)

    assert factory.llm_semaphore() is factory.llm_semaphore()


def test_the_limit_comes_from_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """闸门的宽度就是配置项的值，不是代码里写死的那个默认。"""
    _with_limit(monkeypatch, 2)
    sem = factory.llm_semaphore()

    assert sem._value == 2


@pytest.mark.parametrize("limit", [0, -1, None, ""])
def test_a_useless_limit_is_clamped_to_one(
    monkeypatch: pytest.MonkeyPatch, limit: Any
) -> None:
    """0 或负数的上限夹到 1。

    ``Semaphore(0)`` 会让每个语义节点永远 await——症状是"模型很慢"，排查方向完全跑偏，
    而真相是一个配置项打错了。宁可退化成串行，也不能变成死锁。
    """
    _with_limit(monkeypatch, limit)

    assert factory.llm_semaphore()._value >= 1


async def test_the_gate_actually_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """闸门是真的会挡住第二个调用者，不只是一个摆设对象。"""
    _with_limit(monkeypatch, 1)
    sem = factory.llm_semaphore()

    await sem.acquire()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sem.acquire(), timeout=0.05)
    sem.release()


# ---------------------------------------------------------------------------
# 二、temperature=0 真的落到了模型对象上
# ---------------------------------------------------------------------------


def test_the_real_model_is_deterministic_and_not_streaming() -> None:
    """建的是真 AgentScope 模型对象（不联网），断言两个字段真的被设上了。

    这条是故意不打桩的：``temperature=0`` 靠改 ``generate_kwargs`` 这个具体属性实现，
    属性名一变，``isinstance`` 守卫就静默跳过，语义判断悄悄回到配置的默认温度。打了桩
    的测试会一直绿。

    非流式同样是硬要求：``response_format`` / ``structured_model`` 两条路都要一次性
    拿到完整 JSON 才能校验，流式增量对 schema 校验没有意义。
    """
    model = factory.create_deterministic_model()

    assert model.generate_kwargs["temperature"] == 0
    assert model.stream is False


def test_configuration_cannot_put_the_temperature_back() -> None:
    """``MODEL_PARAMETER_RULES`` 里的 temperature 盖不回来——覆盖发生在建模之后。

    可复现性只让位于网关兼容性（``CAPABILITY_TEMPERATURE``），不让位于通用模型
    参数配置。
    """
    model = factory.create_deterministic_model(
        model_parameter_rules='{"temperature": 0.9}'
    )

    assert model.generate_kwargs["temperature"] == 0


def test_gateway_forced_temperature_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    """部分推理模型网关拒绝 temperature≠1.0（间歇 400）——此时可用性优先。

    推理模型的 temperature 被引擎忽略，配 1.0 不损失任何确定性；但不配的话
    语义判断会间歇 400，节点整体 ``completed=False``。
    """
    monkeypatch.setenv("CAPABILITY_TEMPERATURE", "1.0")

    model = factory.create_deterministic_model()

    assert model.generate_kwargs["temperature"] == 1.0


def test_a_model_without_writable_kwargs_says_so(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """设不上 temperature 时要留一条 WARNING。

    静默跳过的话语义判断按默认温度跑，症状是"昨天报了今天不报"，而抽样精确率也随之
    失去意义——却没有任何日志痕迹能把人指到这里。
    """
    import utils

    class Odd:
        generate_kwargs = "not-a-dict"

    monkeypatch.setattr(utils, "create_model", lambda **kw: Odd())

    with caplog.at_level("WARNING"):
        factory.create_deterministic_model()

    assert any(
        "temperature" in r.getMessage() for r in caplog.records if r.levelname == "WARNING"
    ), f"静默跑成了不可复现：{[r.getMessage() for r in caplog.records]}"


def test_the_stream_flag_is_not_overridable_by_accident(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``stream=False`` 是这个工厂的主张，传进来的 overrides 里不该出现它。"""
    seen: dict[str, Any] = {}
    import utils

    monkeypatch.setattr(
        utils, "create_model", lambda **kw: seen.update(kw) or type("M", (), {})()
    )

    factory.create_deterministic_model(custom_headers='{"a": "b"}')

    assert seen["stream"] is False
    assert seen["custom_headers"] == '{"a": "b"}'


# ---------------------------------------------------------------------------
# 三、结构化客户端拿到的是那个共享闸门
# ---------------------------------------------------------------------------


def test_the_structured_agent_gets_the_shared_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """注入的必须是共享信号量，不是 ``StructuredAgent`` 自己兜底新建的那个。

    ``StructuredAgent.__init__`` 有 ``semaphore or asyncio.Semaphore(4)`` 的兜底——
    忘了传就会静默变成"每个 agent 各 4 并发"，而构造照样成功、测试照样绿。
    """
    _with_limit(monkeypatch, 2)

    agent = factory.create_structured_agent()

    assert agent._sem is factory.llm_semaphore()
    assert agent._sem._value == 2


def test_the_structured_agent_is_deterministic_too(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_limit(monkeypatch, 4)

    agent = factory.create_structured_agent()

    assert agent._model.generate_kwargs["temperature"] == 0


def test_the_timeout_can_be_raised_per_node(monkeypatch: pytest.MonkeyPatch) -> None:
    """各语义节点的输出规模差一个量级，超时要能分别给。

    需求结构化要回十几个字段外加验收标准，实测会把默认 90 秒跑穿；而跑穿的后果不是慢，
    是 ``ok=False`` → ``completed=False``，那个节点直接没有结论。
    """
    _with_limit(monkeypatch, 4)

    agent = factory.create_structured_agent(timeout_s=240.0)

    assert agent._timeout_s == 240.0


def test_not_asking_leaves_the_default_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """不传就用 ``StructuredAgent`` 自己的默认值，工厂不在这里另立一套。"""
    _with_limit(monkeypatch, 4)
    from app.capability.agents.structured import StructuredAgent

    assert factory.create_structured_agent()._timeout_s == StructuredAgent(
        object()
    )._timeout_s


def test_the_timeout_is_not_passed_on_to_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """``timeout_s`` 是客户端的参数，不能混进 ``create_model`` 的 overrides。

    混进去就是 ``TypeError``——而它只在有人第一次真的传这个参数时才出现。
    """
    seen: dict[str, Any] = {}
    import utils

    monkeypatch.setattr(
        utils,
        "create_model",
        lambda **kw: seen.update(kw) or type("M", (), {"generate_kwargs": {}})(),
    )
    _with_limit(monkeypatch, 4)

    factory.create_structured_agent(timeout_s=5.0)

    assert "timeout_s" not in seen
