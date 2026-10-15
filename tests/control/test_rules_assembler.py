"""covers: TC-044, TC-048, TC-049

TC-044 四层规则按顺序叠加且下层覆盖同名项 (US6-1)
TC-048 缺失规则层不阻断其余规则生效 (US6-5)
TC-049 空白规则层不产生空片段 (US6-6)
"""
from __future__ import annotations

import pytest

from app.control.rules.assembler import (
    AssembledRules,
    RuleLayer,
    RulesAssembler,
    VersionedCache,
    merge_layers,
    rule_version,
)


class FakeLoader:
    """内存 RulesLoader 替身：org/biz 各一份，groups 按 chat_id 分发。"""

    def __init__(
        self,
        org: RuleLayer | None = None,
        biz: RuleLayer | None = None,
        groups: dict[str, RuleLayer] | None = None,
    ) -> None:
        self._org = org
        self._biz = biz
        self._groups = groups or {}

    async def org_version(self) -> int | None:
        return self._org.version if self._org else None

    async def load_org_rules(self) -> RuleLayer | None:
        return self._org

    async def biz_version(self) -> int | None:
        return self._biz.version if self._biz else None

    async def load_biz_rules(self) -> RuleLayer | None:
        return self._biz

    async def load_group_rules(self, chat_id: str) -> RuleLayer | None:
        return self._groups.get(chat_id)


def _assembler(
    org=None, biz=None, groups=None, topic: str | None = None
) -> RulesAssembler:
    async def topic_rules(chat_id, topic_id):
        return topic

    return RulesAssembler(
        FakeLoader(org, biz, groups),
        cache=VersionedCache(),
        topic_rules=topic_rules if topic is not None else None,
    )


class TestMergeOrder:
    """TC-044: 四层按 组织→业务线→群→事项 叠加，下层覆盖同名项。"""

    async def test_four_layers_stack_and_lower_overrides(self):
        assembler = _assembler(
            org=RuleLayer("org", "语言: 中文\n审批: 全部需要\n范围: 全公司", version=1),
            biz=RuleLayer("biz", "审批: 低风险免审\n工具: TAPD", version=2),
            groups={"c1": RuleLayer("group", "语言: 英文", version=3)},
            topic="范围: 本事项",
        )

        result = await assembler.assemble(chat_id="c1", topic_id="t1")

        lines = result.prompt.splitlines()
        # 同名项覆盖：语言←群，审批←业务线，范围←事项；位置保持首次出现处
        assert lines == ["语言: 英文", "审批: 低风险免审", "范围: 本事项", "工具: TAPD"]
        # 复合版本指纹（US6-4）
        assert result.rule_version == "org:1|biz:2|group:3|topic:0"

    def test_merge_layers_pure_function(self):
        merged = merge_layers(
            [
                RuleLayer("group", "超时: 30s", 1),
                RuleLayer("org", "超时: 600s\n审计: 开启", 1),
            ]
        )
        assert merged.splitlines() == ["超时: 30s", "审计: 开启"]

    def test_rule_version_missing_layers_are_zero(self):
        assert rule_version([RuleLayer("org", "x", 7)]) == "org:7|biz:0|group:0|topic:0"
        assert AssembledRules.empty().rule_version == "org:0|biz:0|group:0|topic:0"


class TestMissingLayer:
    """TC-048: 任一层缺失，其余层照常生效，不报错。missing_layer 全量。"""

    @pytest.mark.parametrize("missing", ["org", "biz", "group", "topic"])
    async def test_missing_layer_does_not_block_others(self, missing: str):
        assembler = _assembler(
            org=None if missing == "org" else RuleLayer("org", "组织规", 1),
            biz=None if missing == "biz" else RuleLayer("biz", "业务规", 1),
            groups={}
            if missing == "group"
            else {"c1": RuleLayer("group", "群规", 1)},
            topic=None if missing == "topic" else "事项规",
        )

        result = await assembler.assemble(chat_id="c1", topic_id="t1")

        present = {
            "org": "组织规",
            "biz": "业务规",
            "group": "群规",
            "topic": "事项规",
        }
        for name, text in present.items():
            if name == missing:
                assert text not in result.prompt
            else:
                assert text in result.prompt
        # 缺失层指纹记 0，存在层保留版本
        expected = {
            name: (0 if name == missing else 1) for name in ("org", "biz", "group")
        }
        expected["topic"] = 0  # 事项层无版本号
        assert result.rule_version == "|".join(
            f"{name}:{expected[name]}" for name in ("org", "biz", "group", "topic")
        )

    async def test_all_layers_missing_gives_empty(self):
        assembler = _assembler()
        result = await assembler.assemble(chat_id="c1", topic_id="t1")
        assert result.prompt == ""
        assert result.rule_version == "org:0|biz:0|group:0|topic:0"


class TestBlankLayer:
    """TC-049: 空字符串或仅空白的层等同无约束，不产生空片段。"""

    @pytest.mark.parametrize("blank", ["", "   ", "\n\n  \n"])
    async def test_blank_layer_yields_no_fragment(self, blank: str):
        assembler = _assembler(
            org=RuleLayer("org", "组织规", 1),
            biz=RuleLayer("biz", blank, 5),
            groups={"c1": RuleLayer("group", "群规", 1)},
        )

        result = await assembler.assemble(chat_id="c1")

        assert result.prompt.splitlines() == ["组织规", "群规"]
        assert "" not in result.prompt.splitlines()
        # 空白层不进指纹（等同缺失）
        assert result.rule_version == "org:1|biz:0|group:1|topic:0"
