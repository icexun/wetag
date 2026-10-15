"""covers: TC-045, TC-046

TC-045 群规则下一任务生效且不影响同线其他群 (US6-2)
TC-046 上层规则缓存按版本失效并被下一次装配使用 (US6-3)
"""
from __future__ import annotations

from app.control.repository import RulesRepository
from app.control.rules.assembler import RulesAssembler, VersionedCache


class CountingRulesRepository(RulesRepository):
    """统计全量读取次数，验证版本未变时不重读内容（TC-046）。"""

    def __init__(self, pool):
        super().__init__(pool)
        self.org_loads = 0
        self.biz_loads = 0
        self.org_version_probes = 0

    async def load_org_rules(self):
        self.org_loads += 1
        return await super().load_org_rules()

    async def load_biz_rules(self):
        self.biz_loads += 1
        return await super().load_biz_rules()

    async def org_version(self):
        self.org_version_probes += 1
        return await super().org_version()


class TestGroupRulesImmediateEffect:
    """TC-045: 群规则修改后下一任务立即生效；同线其他群不受影响；无需重启。"""

    async def test_group_rules_immediate_and_isolated(self, rules_repo):
        # 同一业务线两个群；同一装配器实例（进程不重启）
        assembler = RulesAssembler(rules_repo, cache=VersionedCache())

        await rules_repo.save_group_rules("chat-a", "群规A: 第一版")
        first_a = await assembler.assemble(chat_id="chat-a")
        first_b = await assembler.assemble(chat_id="chat-b")

        assert "群规A: 第一版" in first_a.prompt
        assert "群规A" not in first_b.prompt  # 另一群无群级规则
        group_layer_a = [la for la in first_a.layers if la.name == "group"]
        assert group_layer_a and group_layer_a[0].version == 1

        # 群成员修改本群规则 → 下一次装配立即生效（群级直读，无缓存）
        await rules_repo.save_group_rules("chat-a", "群规A: 第二版")
        second_a = await assembler.assemble(chat_id="chat-a")
        second_b = await assembler.assemble(chat_id="chat-b")

        assert "群规A: 第二版" in second_a.prompt
        assert "第一版" not in second_a.prompt
        assert second_a.rule_version == first_a.rule_version.replace("group:1", "group:2")
        # 同线其他群行为不变
        assert second_b.prompt == first_b.prompt
        assert second_b.rule_version == first_b.rule_version


class TestVersionedCacheInvalidation:
    """TC-046: 版本未变不重复全量读取；版本变化后下一次装配使用新内容。"""

    async def test_cache_by_version_and_invalidate_on_change(self, pool):
        repo = CountingRulesRepository(pool)
        assembler = RulesAssembler(repo, cache=VersionedCache())

        v1 = await repo.save_org_rules("组织规: 第一版")
        first = await assembler.assemble(chat_id="c1")
        second = await assembler.assemble(chat_id="c1")

        assert "组织规: 第一版" in first.prompt
        assert second.prompt == first.prompt
        # 版本未变：两次装配只做轻量版本探测，全量读取仅一次
        assert repo.org_version_probes == 2
        assert repo.org_loads == 1

        # 版本更新 → 下一次装配感知并使用新内容
        v2 = await repo.save_org_rules("组织规: 第二版")
        assert v2 > v1
        third = await assembler.assemble(chat_id="c1")

        assert "组织规: 第二版" in third.prompt
        assert "第一版" not in third.prompt
        assert repo.org_loads == 2
        assert third.rule_version == first.rule_version.replace(f"org:{v1}", f"org:{v2}")

    async def test_biz_layer_cached_independently(self, pool):
        repo = CountingRulesRepository(pool)
        assembler = RulesAssembler(repo, cache=VersionedCache())

        await repo.save_biz_rules("业务规: 初版")
        await assembler.assemble(chat_id="c1")
        await assembler.assemble(chat_id="c2")
        assert repo.biz_loads == 1  # 业务线级缓存跨群共享

        await repo.save_biz_rules("业务规: 新版")
        updated = await assembler.assemble(chat_id="c1")
        assert "业务规: 新版" in updated.prompt
        assert repo.biz_loads == 2
