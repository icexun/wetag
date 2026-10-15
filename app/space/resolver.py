"""aibotid → workspace 静态映射，进程 botId 一致性校验（§4.2 等式）。

``aibotid`` 是业务线归属的唯一权威来源：群不需要人工选择业务线，但部署时必须
配置 Bot 与空间的静态映射。

不建立用户到业务线、群名到业务线、国家字段到群的映射——那些都会产生第二份
归属状态并随时间漂移。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional

from app.contracts.errors import IsolationViolation
from app.contracts.ids import WorkspaceId
from config.properties import WecomProperties, WorkspaceProperties


class ScopeType(str, Enum):
    ORGANIZATION = "ORGANIZATION"
    WORKSPACE = "WORKSPACE"


class UnknownBotError(IsolationViolation):
    """收到未登记 aibotid 的回调。属安全事件，直接拒绝处理。"""


@dataclass(frozen=True)
class BotBinding:
    bot_id: str
    scope_type: ScopeType
    workspace_id: WorkspaceId
    """物理隔离单元。WeTag-公共虽绑定 Organization，仍有自己的 schema 与进程。"""
    enabled: bool = True

    @property
    def is_organization_scoped(self) -> bool:
        """组织级 Bot 只装配组织级能力与记忆，不加载任何业务线私有资源。"""
        return self.scope_type is ScopeType.ORGANIZATION


class BotBindingRegistry:
    """部署期确定、运行期只读的绑定表。"""

    def __init__(self, bindings: Dict[str, BotBinding], current_bot_id: str):
        if current_bot_id not in bindings:
            raise UnknownBotError(
                f"current bot {current_bot_id} is not present in BOT_BINDINGS"
            )
        self._bindings = bindings
        self._current_bot_id = current_bot_id

    @classmethod
    def from_config(cls) -> "BotBindingRegistry":
        wecom_props = WecomProperties.instance()
        workspace_props = WorkspaceProperties.instance()

        current_bot_id = wecom_props.bot_id
        if not current_bot_id:
            raise UnknownBotError("WECOM_BOT_ID is not configured")

        bindings = _parse_bindings(workspace_props.bot_bindings)
        if not bindings:
            # 单业务线部署无需显式配 BOT_BINDINGS，进程自身的两个变量即完整绑定。
            workspace_id = workspace_props.workspace_id
            if not workspace_id:
                raise UnknownBotError(
                    "either BOT_BINDINGS or WORKSPACE_ID must be configured"
                )
            bindings = {
                current_bot_id: BotBinding(
                    bot_id=current_bot_id,
                    scope_type=ScopeType.WORKSPACE,
                    workspace_id=workspace_id,
                )
            }

        registry = cls(bindings, current_bot_id)

        configured_workspace = workspace_props.workspace_id
        resolved = registry.current.workspace_id
        if configured_workspace and configured_workspace != resolved:
            raise UnknownBotError(
                f"WORKSPACE_ID={configured_workspace} contradicts BOT_BINDINGS "
                f"which maps {current_bot_id} to {resolved}"
            )
        return registry

    @property
    def current(self) -> BotBinding:
        return self._bindings[self._current_bot_id]

    def require(self, bot_id: str) -> BotBinding:
        """校验回调的 aibotid 属于本进程。

        隔离的第二道保险（裁决 #3）：长连接由持有该 Bot 凭据的进程自建，
        理论上不会收到他人的回调，但仍显式校验，不做「找个替代目标」的兜底。
        """
        binding = self._bindings.get(bot_id)
        if binding is None:
            raise UnknownBotError(f"unknown aibotid: {bot_id}")
        if not binding.enabled:
            raise UnknownBotError(f"bot {bot_id} is disabled")
        if bot_id != self._current_bot_id:
            raise UnknownBotError(
                f"aibotid {bot_id} does not belong to this process "
                f"(current={self._current_bot_id})"
            )
        return binding

    def get(self, bot_id: str) -> Optional[BotBinding]:
        return self._bindings.get(bot_id)


def _parse_bindings(raw: Optional[str]) -> Dict[str, BotBinding]:
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise UnknownBotError("BOT_BINDINGS must be a JSON object keyed by bot id")

    bindings: Dict[str, BotBinding] = {}
    for bot_id, cfg in data.items():
        if not isinstance(cfg, dict):
            raise UnknownBotError(f"BOT_BINDINGS[{bot_id}] must be an object")
        scope_type = ScopeType(cfg.get("scope_type", ScopeType.WORKSPACE.value))
        workspace_id = cfg.get("workspace_id") or cfg.get("scope_id")
        if not workspace_id:
            raise UnknownBotError(f"BOT_BINDINGS[{bot_id}] misses workspace_id")
        bindings[bot_id] = BotBinding(
            bot_id=bot_id,
            scope_type=scope_type,
            workspace_id=workspace_id,
            enabled=bool(cfg.get("enabled", True)),
        )
    return bindings
