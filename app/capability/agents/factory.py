"""模型与 Agent 装配。``container.py` 的 LLM 段唯一依赖这里。

两条关键纪律：

1. **共享一个信号量。** D-2 是一业务线一进程，进程内所有语义节点（结构化、
   话题分类、规约提炼）共用 ``LLM_MAX_CONCURRENCY``。散落各处各建信号量等于
   没有上限——真实后果是并发打爆网关额度后**全部**语义节点一起失败，于是所有
   任务都变成 ``completed=False``，能力整体下线。
2. **语义判断默认 temperature=0。** 结构化抽取必须可复现：同一份输入两次运行给出
   不同结论，就没法做 §10.3 的精确率抽样，也没法向用户解释"为什么昨天报了
   今天不报"。创作类场景才需要温度。网关拒绝低温时（部分推理模型
   间歇 400）由 ``CAPABILITY_TEMPERATURE`` 外置覆盖——推理模型的 temperature
   被引擎忽略，配 1.0 不损可复现性。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from config.properties import CapabilityProperties

logger = logging.getLogger(__name__)

_llm_semaphore: asyncio.Semaphore | None = None


def llm_semaphore() -> asyncio.Semaphore:
    """全进程共享的 LLM 并发闸门。"""
    global _llm_semaphore
    if _llm_semaphore is None:
        limit = int(CapabilityProperties.instance().llm_max_concurrency or 4)
        _llm_semaphore = asyncio.Semaphore(max(1, limit))
        logger.info("LLM 并发上限 = %d", limit)
    return _llm_semaphore


def create_deterministic_model(**overrides: Any) -> Any:
    """建一个确定性温度、非流式的模型，专供结构化语义判断。

    非流式是必须的：``response_format`` / ``structured_model`` 两条路都要一次性
    拿到完整 JSON 才能校验，流式增量对 schema 校验没有意义。

    温度默认 0（可复现），由 ``CAPABILITY_TEMPERATURE`` 外置——有的网关（部分
    推理模型）拒绝 temperature≠1.0，此时可用性优先；推理模型的 temperature
    被引擎忽略，可复现性不受影响。
    """
    import utils

    model = utils.create_model(stream=False, **overrides)
    kwargs = getattr(model, "generate_kwargs", None)
    if isinstance(kwargs, dict):
        # 在 create_model 之后覆盖，所以 MODEL_PARAMETER_RULES 里的 temperature
        # 也盖不回来——可复现性只让位于网关兼容性（CAPABILITY_TEMPERATURE），
        # 不让位于通用模型参数配置。
        kwargs["temperature"] = float(
            CapabilityProperties.instance().deterministic_temperature or 0
        )
    else:
        # 设不上就说出来。静默跳过的话语义判断会按配置的默认温度跑，症状是"昨天报了
        # 今天不报"，而抽样精确率也随之失去意义——而这一切没有任何日志痕迹。
        logger.warning(
            "模型对象上没有可写的 generate_kwargs（%s），确定性 temperature 未生效："
            "语义判断将不可复现",
            type(model).__name__,
        )
    return model


def create_structured_agent(
    *, timeout_s: float | None = None, **overrides: Any
):
    """结构化输出客户端。语义判断的唯一入口。

    ``timeout_s`` 留成参数而不是写死：各语义节点的输出规模差一个量级。分类判定只回
    一个枚举加一句引文，需求结构化要回十几个字段外加验收标准，实测后者在网关上会把
    默认的 90 秒跑穿。而超时的后果不是慢——是 ``ok=False`` → ``completed=False``，
    整个节点没有结论。给调用方一个旋钮，比让它静默失败好。

    其余 kwargs 一律透传给 ``utils.create_model``（模型名、网关、参数规则）。
    """
    from app.capability.agents.structured import StructuredAgent

    kwargs: dict[str, Any] = {"semaphore": llm_semaphore()}
    if timeout_s is not None:
        kwargs["timeout_s"] = timeout_s
    return StructuredAgent(create_deterministic_model(**overrides), **kwargs)


__all__ = ["llm_semaphore", "create_deterministic_model", "create_structured_agent"]
