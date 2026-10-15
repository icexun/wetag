"""消息回复 Loop（loop3_push）测试：聚合 / 提炼 / 写入 / 护栏。"""

from __future__ import annotations

from app.analytics.loops.loop3_push import (
    MAX_RULES_PER_RUN,
    MIN_DOWN_VOTES,
    RuleDraft,
    apply_prompt_rule,
    distill_rule,
    group_down_votes,
    render_push_notice,
    run_push_loop,
    upsert_biz_rule,
)


def _dv(reason, comment="", actor="u1") -> dict:
    return {
        "actor_user_id": actor,
        "reason": [reason] if reason else [],
        "comment": comment,
        "created_at": None,
    }


# -- group_down_votes --------------------------------------------------------


def test_group_down_votes_by_reason_with_general_fallback() -> None:
    rows = [
        _dv("inaccurate", "说错了"),
        _dv("inaccurate", "又错"),
        _dv(None, "就是不好"),  # 无 reason → general
        _dv("spam"),
    ]
    groups = group_down_votes(rows)
    assert len(groups["inaccurate"]) == 2
    assert len(groups["general"]) == 1
    assert len(groups["spam"]) == 1


# -- upsert_biz_rule ----------------------------------------------------------


def test_upsert_appends_and_never_touches_manual_entries() -> None:
    content = "只给结论: 不预告工具调用\nloop3-old: 旧规约"
    draft = RuleDraft(key="no-narration", rule="回复不夹工具旁白")

    out = upsert_biz_rule(content, draft)

    assert "只给结论: 不预告工具调用" in out  # 人工条目原样保留
    assert "loop3-old: 旧规约" in out
    assert "loop3-no-narration: 回复不夹工具旁白" in out


def test_upsert_overrides_same_key_in_namespace() -> None:
    content = "loop3-no-narration: 旧表述"
    draft = RuleDraft(key="no-narration", rule="新表述")

    out = upsert_biz_rule(content, draft)

    assert "新表述" in out
    assert "旧表述" not in out
    assert out.count("loop3-no-narration") == 1


# -- distill_rule -------------------------------------------------------------


class _FakeAgent:
    def __init__(self, draft=None, ok=True):
        self._draft = draft
        self._ok = ok

    async def complete(self, schema, *, system, user, trace_id=None):
        from app.capability.agents.structured import StructuredResult

        if not self._ok:
            return StructuredResult(ok=False, reason="网关 500")
        return StructuredResult(ok=True, data=self._draft)


async def test_distill_rule_sanitizes_key_and_truncates() -> None:
    agent = _FakeAgent(RuleDraft(key="No Narration!!", rule="x" * 300, rationale="r"))

    draft = await distill_rule(agent, "spam", ["刷屏了"])

    assert draft is not None
    assert draft.key == "no-narration"
    assert len(draft.rule) == 200


async def test_distill_rule_returns_none_on_failure_or_bad_key() -> None:
    assert await distill_rule(_FakeAgent(ok=False), "spam", []) is None
    assert await distill_rule(None, "spam", []) is None  # 无 agent 不硬写
    bad = _FakeAgent(RuleDraft(key="！！！", rule="规约"))
    assert await distill_rule(bad, "spam", []) is None


# -- apply_prompt_rule / run_push_loop ----------------------------------------


class _FakeRulesRepo:
    def __init__(self, content="", version=3):
        self._content = content
        self._version = version

    async def load_biz_rules(self):
        if self._content == "" and self._version == 0:
            return None
        from app.control.rules.assembler import RuleLayer

        return RuleLayer("biz", self._content, self._version)

    async def save_biz_rules(self, content, *, updated_by=None):
        self._content = content
        self._version += 1
        return self._version


class _FakeCapRepo:
    def __init__(self, down_rows=()):
        self._down = list(down_rows)
        self.changes = []

    async def loop_down_votes(self, *, window_days=7):
        return self._down

    async def record_rule_change(self, **kw):
        self.changes.append(kw)


async def test_apply_prompt_rule_bumps_version_and_archives() -> None:
    rules = _FakeRulesRepo("人工条目: 保留我", version=3)
    cap = _FakeCapRepo()
    draft = RuleDraft(key="no-at", rule="回复不 @ 裸 userid", rationale="👎×3")

    applied = await apply_prompt_rule(
        rules, cap, draft=draft, down_votes=3, reason="format"
    )

    assert applied.biz_version == 4
    assert "人工条目: 保留我" in rules._content
    assert "loop3-no-at: 回复不 @ 裸 userid" in rules._content
    assert cap.changes[0]["kind"] == "prompt"
    assert cap.changes[0]["rule_version_old"] == "biz:3"
    assert cap.changes[0]["rule_version_new"] == "biz:4"


async def test_run_push_loop_thresholds_and_quota() -> None:
    rows = (
        [_dv("spam", f"刷屏{i}") for i in range(4)]       # 达标
        + [_dv("inaccurate") for _ in range(MIN_DOWN_VOTES - 1)]  # 不达标
        + [_dv("format") for _ in range(5)]                # 达标但超周名额
    )
    cap = _FakeCapRepo(rows)
    rules = _FakeRulesRepo()
    agent = _FakeAgent(RuleDraft(key="k", rule="规约"))

    report = await run_push_loop(cap, rules, agent)

    assert len(report.applied) == MAX_RULES_PER_RUN  # spam、format 两条
    assert any("inaccurate" in s for s in report.skipped)  # 未达阈值被记下
    assert report.worth_pushing


async def test_run_push_loop_silent_below_threshold() -> None:
    cap = _FakeCapRepo([_dv("spam"), _dv("spam")])
    report = await run_push_loop(cap, _FakeRulesRepo(), _FakeAgent())
    assert not report.applied
    assert not report.worth_pushing


def test_render_push_notice_shows_rules_and_rollback() -> None:
    from app.analytics.loops.loop3_push import AppliedRule, PushLoopReport

    report = PushLoopReport(
        window_days=7,
        down_votes=4,
        applied=(AppliedRule(key="loop3-no-at", rule="不 @ 裸 userid", down_votes=4, biz_version=5),),
        skipped=(),
    )
    text = render_push_notice(report)
    assert "loop3-no-at" in text and "biz 层 v5" in text and "rule_change_log" in text
