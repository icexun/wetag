"""模型与 Agent 装配（``app/capability/agents/factory.py``）。

两条纪律，都没有崩溃形态：

1. **信号量全进程共享**（D-2 / R7）。散落各处各建一个等于没有上限。真实后果不是
   "慢一点"，而是并发打爆网关额度后**所有**语义节点一起失败，于是每一次检测都
   变成 ``completed=False``——能力整体下线，而日志里只看到一片 429。
2. **``temperature=0``**。同一份输入两次运行给出不同结论，§10.3 的精确率抽样就
   没有意义，也没法向用户解释"为什么昨天报了今天不报"。温度漏设不会报错，只会
   让结论随机抖动。
"""

from __future__ import annotations

import asyncio
from typing import Any, Iterator

import pytest

from pydantic import BaseModel

from app.capability.agents import factory
from app.capability.agents.structured import StructuredAgent


class _Tiny(BaseModel):
    n: int


class _Resp:
    def __init__(self, metadata=None) -> None:
        self.content = None
        self.metadata = metadata
        self.usage = None


@pytest.fixture(autouse=True)
def _reset_shared_semaphore() -> Iterator[None]:
    """模块级单例必须在每个测试前后归零，否则测试之间会串味。"""
    factory._llm_semaphore = None
    yield
    factory._llm_semaphore = None


class FakeModel:
    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        self.generate_kwargs: dict[str, Any] = {"temperature": 0.7, "top_p": 1}


@pytest.fixture
def stub_create_model(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """替掉 ``utils.create_model``——它会去读模型配置并建真实客户端。"""
    import utils

    seen: list[dict[str, Any]] = []

    def _fake(**kwargs: Any) -> FakeModel:
        seen.append(kwargs)
        return FakeModel(**kwargs)

    monkeypatch.setattr(utils, "create_model", _fake)
    return seen


# ---------------------------------------------------------------------------
# 一、共享信号量
# ---------------------------------------------------------------------------


def test_the_semaphore_is_one_shared_object() -> None:
    assert factory.llm_semaphore() is factory.llm_semaphore()


def test_the_limit_comes_from_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "2")
    from config.properties import CapabilityProperties

    CapabilityProperties._instance = None  # type: ignore[attr-defined]
    try:
        assert factory.llm_semaphore()._value == 2
    finally:
        CapabilityProperties._instance = None  # type: ignore[attr-defined]


@pytest.mark.parametrize("configured, expected", [("0", 4), ("-1", 1), ("", 4)])
def test_a_nonsense_limit_never_yields_an_unusable_gate(
    monkeypatch: pytest.MonkeyPatch, configured: str, expected: int
) -> None:
    """0 / 负数 / 空串三种手抖配置，都不能产出一个不可用的闸门。

    三条走的是**两段不同的代码**，所以必须分别钉：

    * ``0`` 与 ``""`` 是 falsy，被 ``or 4`` 接住，回落默认值；
    * ``-1`` 是 **truthy**，穿过 ``or 4`` 直接进构造函数——``asyncio.Semaphore(-1)``
      会抛 ``ValueError``，把进程拦在启动期。挡住它的是 ``max(1, limit)``。

    而 ``Semaphore(0)`` 才是最坏的形态：它不报错，只是让第一个 ``async with``
    永久挂住。表现是能力静默卡死——没有异常、没有超时日志，只是再也不产出结论。
    """
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", configured)
    from config.properties import CapabilityProperties

    CapabilityProperties._instance = None  # type: ignore[attr-defined]
    try:
        assert factory.llm_semaphore()._value == expected
    finally:
        CapabilityProperties._instance = None  # type: ignore[attr-defined]


async def test_two_agents_are_bounded_together_not_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个 agent 实例并发跑，总在飞请求数受**一个**上限约束。

    这条必须真的穿过 ``StructuredAgent.complete``：只测 ``llm_semaphore()`` 本身
    并发正确，测不出"装配时忘了把它传进 agent"——那种版本下每个 agent 自建
    ``Semaphore(4)``，两个 agent 的实际上限是 8。
    """
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "2")
    from config.properties import CapabilityProperties

    CapabilityProperties._instance = None  # type: ignore[attr-defined]

    inflight = 0
    peak = 0

    class SlowModel:
        generate_kwargs: dict[str, Any] = {}

        async def __call__(self, messages, **kwargs):
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            try:
                await asyncio.sleep(0.02)
                return _Resp(metadata={"n": 1})
            finally:
                inflight -= 1

    import utils

    monkeypatch.setattr(utils, "create_model", lambda **kw: SlowModel())
    try:
        a = factory.create_structured_agent()
        b = factory.create_structured_agent()
        await asyncio.gather(
            *(
                agent.complete(_Tiny, system="s", user=f"u{i}")
                for i, agent in enumerate([a, b] * 4)
            )
        )
    finally:
        CapabilityProperties._instance = None  # type: ignore[attr-defined]

    assert peak <= 2, f"两个 agent 各自计量了并发：peak={peak}，上限本该是 2"


# ---------------------------------------------------------------------------
# 二、确定性模型
# ---------------------------------------------------------------------------


def test_deterministic_model_forces_temperature_zero(stub_create_model) -> None:
    model = factory.create_deterministic_model()
    assert model.generate_kwargs["temperature"] == 0
    assert model.generate_kwargs["top_p"] == 1, "只该覆盖 temperature，别顺手改掉别的采样参数"


def test_deterministic_model_is_not_streaming(stub_create_model) -> None:
    """``response_format`` / ``structured_model`` 都要一次性拿到完整 JSON 才能校验。"""
    factory.create_deterministic_model()
    assert stub_create_model[0]["stream"] is False


def test_overrides_reach_create_model(stub_create_model) -> None:
    factory.create_deterministic_model(custom_headers='{"a":"b"}')
    assert stub_create_model[0]["custom_headers"] == '{"a":"b"}'


# ---------------------------------------------------------------------------
# 三、装配出来的 agent 必须挂在共享闸门上
# ---------------------------------------------------------------------------


def test_structured_agent_is_wired_to_the_shared_semaphore(stub_create_model) -> None:
    """两个 agent 的 ``_sem`` 必须是同一个对象，且就是 ``llm_semaphore()``。"""
    a = factory.create_structured_agent()
    b = factory.create_structured_agent()

    assert isinstance(a, StructuredAgent)
    assert a._sem is b._sem is factory.llm_semaphore()


def test_structured_agent_gets_a_zero_temperature_model(stub_create_model) -> None:
    agent = factory.create_structured_agent()
    assert agent._model.generate_kwargs["temperature"] == 0
