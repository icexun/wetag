"""四层 Rules 装配：组织 → 业务线 → 群 → 事项，下层覆盖上层同名项（US6）。

存储面（plan「三层 Rules 装配」）：
- 组织级：org.org_config key `rules.org`（版本号列，进程内缓存 —— US6-3）
- 业务线级：org.org_config key `rules.biz.{workspace}`（同上）
- 群级：ws_{workspace}.runtime_state key `rules:group:{chat_id}`（每次装配直读 —— US6-2）
- 事项级：只读 Space 的 topic 上下文，经 topic_rules 提供者注入（无版本号，指纹记 0）

装配语义：
- 缺失层（None）不阻断其余层生效（US6-5）；空白层不产生空片段（US6-6）
- 同名项按下层覆盖：行首 `key:` 前缀（可带 `- ` 项目符号）为项名，无冒号整行为项名
- rule_version = 四层版本号复合指纹 `org:3|biz:5|group:12|topic:0`，写入 task/audit（US6-4）
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

LAYER_ORDER = ("org", "biz", "group", "topic")


@dataclass(frozen=True)
class RuleLayer:
    """一层规则的内容与版本。"""

    name: str  # org / biz / group / topic
    content: str
    version: int = 0


@dataclass(frozen=True)
class AssembledRules:
    """装配产物：提示词片段 + 复合版本指纹。"""

    prompt: str
    rule_version: str
    layers: tuple[RuleLayer, ...] = field(default_factory=tuple)

    @classmethod
    def empty(cls) -> AssembledRules:
        return cls(prompt="", rule_version=rule_version(()), layers=())


class RulesLoader(Protocol):
    """装配器的读取面（生产为 RulesRepository，测试可为内存替身）。"""

    async def org_version(self) -> int | None: ...
    async def load_org_rules(self) -> RuleLayer | None: ...
    async def biz_version(self) -> int | None: ...
    async def load_biz_rules(self) -> RuleLayer | None: ...
    async def load_group_rules(self, chat_id: str) -> RuleLayer | None: ...


def _item_key(line: str) -> str:
    """规则行的项名：`key: value` 取冒号前（可带 `- ` 符号），否则整行。"""
    head = line.split(":", 1)[0] if ":" in line else line
    return head.strip().lstrip("- ").strip().lower()


def merge_layers(layers: list[RuleLayer] | tuple[RuleLayer, ...]) -> str:
    """按 组织→业务线→群→事项 叠加；下层同名项覆盖上层，位置保持首次出现处。

    空行跳过（US6-6）；None 层由调用方过滤，不参与合并（US6-5）。
    """
    order = {name: idx for idx, name in enumerate(LAYER_ORDER)}
    items: dict[str, str] = {}
    for layer in sorted(layers, key=lambda la: order.get(la.name, len(LAYER_ORDER))):
        for raw in layer.content.splitlines():
            line = raw.strip()
            if not line:
                continue
            items[_item_key(line)] = line
    return "\n".join(items.values())


def rule_version(layers: list[RuleLayer] | tuple[RuleLayer, ...]) -> str:
    """四层版本号复合指纹；缺失层记 0（US6-4）。"""
    versions = {layer.name: layer.version for layer in layers}
    return "|".join(f"{name}:{versions.get(name, 0)}" for name in LAYER_ORDER)


class VersionedCache:
    """上层规则（组织/业务线）进程内缓存：版本未变不重读内容（US6-3）。

    缓存键是版本号而非 TTL：每次装配仅一次轻量版本查询，
    版本变化才全量重读，保证改动下一次装配即生效。
    """

    def __init__(self) -> None:
        self._versions: dict[str, int] = {}
        self._layers: dict[str, RuleLayer] = {}

    async def get(
        self,
        name: str,
        *,
        fetch_version: Callable[[], Awaitable[int | None]],
        fetch_layer: Callable[[], Awaitable[RuleLayer | None]],
    ) -> RuleLayer | None:
        version = await fetch_version()
        if version is None:  # 层被删除：清缓存按缺失处理
            self._versions.pop(name, None)
            self._layers.pop(name, None)
            return None
        if self._versions.get(name) == version and name in self._layers:
            return self._layers[name]
        layer = await fetch_layer()
        if layer is None:  # 版本在但内容消失（并发删除）：按缺失处理
            self._versions.pop(name, None)
            self._layers.pop(name, None)
            return None
        self._versions[name] = version
        self._layers[name] = layer
        return layer

    def invalidate(self, name: str | None = None) -> None:
        if name is None:
            self._versions.clear()
            self._layers.clear()
        else:
            self._versions.pop(name, None)
            self._layers.pop(name, None)


class RulesAssembler:
    """四层装配编排：上层走版本缓存，群级直读，事项级经提供者注入。"""

    def __init__(
        self,
        loader: RulesLoader,
        *,
        cache: VersionedCache | None = None,
        topic_rules: Callable[[str | None, str | None], Awaitable[str | None]] | None = None,
    ) -> None:
        self._loader = loader
        self._cache = cache or VersionedCache()
        self._topic_rules = topic_rules

    async def assemble(
        self,
        *,
        chat_id: str | None = None,
        topic_id: str | None = None,
    ) -> AssembledRules:
        layers: list[RuleLayer] = []
        org = await self._cache.get(
            "org",
            fetch_version=self._loader.org_version,
            fetch_layer=self._loader.load_org_rules,
        )
        if org is not None:
            layers.append(org)
        biz = await self._cache.get(
            "biz",
            fetch_version=self._loader.biz_version,
            fetch_layer=self._loader.load_biz_rules,
        )
        if biz is not None:
            layers.append(biz)
        if chat_id:
            group = await self._loader.load_group_rules(chat_id)
            if group is not None:
                layers.append(group)
        if self._topic_rules is not None:
            topic_content = await self._topic_rules(chat_id, topic_id)
            if topic_content is not None:
                layers.append(RuleLayer("topic", topic_content, version=0))
        # 空白层等同无约束：过滤后不参与合并与指纹（US6-6）
        layers = [la for la in layers if la.content.strip()]
        return AssembledRules(
            prompt=merge_layers(layers),
            rule_version=rule_version(layers),
            layers=tuple(layers),
        )
