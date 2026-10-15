"""三层配置继承装配，产出 effectiveConfigVersion。

装配本身（组织 / 业务线 / 群三层继承）归 Control 的 ``rules/assembler``
（§10.2 第 12 项）。Space 只消费版本号：SpaceContext 必须带上它，事后才能
回溯「这次执行用的是哪一版规则」。
"""

from __future__ import annotations

from typing import Protocol

from app.contracts.ids import ChatId


class ConfigVersionProvider(Protocol):
    """三层配置装配的版本号来源，实现方为 Control。"""

    async def effective_version(self, chat_id: ChatId) -> int: ...
