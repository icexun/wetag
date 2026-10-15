"""Rules 中间件：注入执行前置约束（US6）。

apply(task) 在建 agent 之前调用，产出 system_prompt 片段与规则版本指纹。
超长保护（US6-7）：任一层或总长度超 RULES_MAX_CHARS → 拒绝本次装配 + 告警，
回退该群（无群则本业务线）上一版本有效规则，不静默截断。
"""
from __future__ import annotations

import logging

from app.control.rules.assembler import AssembledRules, RulesAssembler
from app.contracts.task import Task
from app.platform.observability.alerting import alert, AlertKind

logger = logging.getLogger(__name__)

RULES_MAX_CHARS = 8000


def _default_max_chars() -> int:
    """配置链读取 RULES_MAX_CHARS；config 不可用时落模块常量。"""
    try:
        from config.properties import ControlProperties

        return int(ControlProperties.instance().rules_max_chars)
    except Exception:  # noqa: BLE001 - 配置链不可达时用默认
        return RULES_MAX_CHARS


class RulesMiddleware:
    """规则装配的执行前置闸门：长度护栏 + 上一版本回退。"""

    def __init__(self, assembler: RulesAssembler, *, max_chars: int | None = None) -> None:
        self._assembler = assembler
        self._max_chars = int(max_chars) if max_chars else _default_max_chars()
        # 每个作用域（群 / 业务线）最近一次有效装配，供超长回退（US6-7）
        self._last_good: dict[str, AssembledRules] = {}

    async def apply(self, task: Task) -> AssembledRules:
        assembled = await self._assembler.assemble(
            chat_id=task.chat_id, topic_id=task.topic_id
        )
        scope = task.chat_id or task.workspace_id
        oversized = self._oversized_layer(assembled) or len(assembled.prompt) > self._max_chars
        if oversized:
            fallback = self._last_good.get(scope, AssembledRules.empty())
            alert(
                AlertKind.DEGRADE,
                "规则装配超长，本次已拒绝并回退上一版本",
                f"scope={scope} 长度超限（上限 {self._max_chars} 字符），不截断",
                workspace_id=task.workspace_id,
                chat_id=task.chat_id,
                rejected_rule_version=assembled.rule_version,
                fallback_rule_version=fallback.rule_version,
            )
            return fallback
        self._last_good[scope] = assembled
        return assembled

    def _oversized_layer(self, assembled: AssembledRules) -> bool:
        """任一层内容超上限即视为超长（US6-7「某一层规则内容超出可承载长度」）。"""
        return any(len(layer.content) > self._max_chars for layer in assembled.layers)
