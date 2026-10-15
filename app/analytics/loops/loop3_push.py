"""消息回复 Loop（Loop-3）：👎 反馈 → 规约提炼 → biz 层规则自动更新。

热更新通道是**既有**的四层 Rules 装配（``control/rules``）：biz 层存
``org.org_config``、版本号单调递增，``RulesMiddleware`` 在每次 Agent 执行前
装配进 system_prompt——写入后下一条消息即生效，无需发版。本模块只是给这条
通道接上 Loop 的自动写入端。

护栏（自动改 prompt 是 2026-10-09 裁决放行的，约束不能少）：

- 只写 biz 层，规则行 key 一律 ``loop3-`` 前缀命名空间——绝不覆盖人工条目；
  同名 key 重复提炼是**更新**（下层覆盖语义由装配器保证）
- 同一踩因窗口内 ≥ ``MIN_DOWN_VOTES`` 才提炼——单次吐槽不配变成规则
- 每周最多 ``MAX_RULES_PER_RUN`` 条，单条 ≤ ``MAX_RULE_CHARS`` 字
- 每次变更落 ``rule_change_log``（kind='prompt'）+ 群内通知；
  ``RulesMiddleware`` 的超长回退（US6-7）是既有兜底
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

MIN_DOWN_VOTES = 3
MAX_RULES_PER_RUN = 2
MAX_RULE_CHARS = 200

# 规则行的 key 命名空间：loop3 自动写入的条目与人手工条目（无此前缀）隔离，
# 覆盖只发生在命名空间内部。
KEY_PREFIX = "loop3-"


class RuleDraft(BaseModel):
    """LLM 提炼产出：一条可写进 biz 层规则的规约。"""

    key: str = Field(description="规约短名，小写字母数字连字符，不带 loop3- 前缀")
    rule: str = Field(description="一句可执行的回复规约，中文，200 字内")
    rationale: str = Field(default="", description="提炼依据（留痕用）")


@dataclass(frozen=True)
class AppliedRule:
    key: str
    rule: str
    down_votes: int
    biz_version: int


@dataclass(frozen=True)
class PushLoopReport:
    """一圈消息回复 Loop 的产出。"""

    window_days: int
    down_votes: int
    applied: tuple[AppliedRule, ...]
    skipped: tuple[str, ...]  # 未达阈值/提炼失败的踩因

    @property
    def worth_pushing(self) -> bool:
        return bool(self.applied)


def group_down_votes(rows: Iterable[Any]) -> dict[str, list[Any]]:
    """按踩因分组。无 reason 的 👎 归入 ``general``——模糊不满也是信号。"""
    groups: dict[str, list[Any]] = {}
    for row in rows:
        reasons = list(row["reason"] or ()) or ["general"]
        for reason in reasons:
            groups.setdefault(str(reason), []).append(row)
    return groups


async def distill_rule(
    agent: Any, reason: str, comments: Sequence[str], *, trace_id: str | None = None
) -> RuleDraft | None:
    """把一组同因 👎 提炼成一条规约。提炼失败返回 None（本轮跳过，不硬写）。"""
    if agent is None:
        return None
    sampled = [c for c in comments if c][:5]
    result = await agent.complete(
        RuleDraft,
        system=(
            "你是企微协作机器人的回复规约提炼器。用户点踩的反馈会给你一类问题，"
            "你要产出一条**可执行的回复规约**（key + rule）。规约写给生成回复的模型看，"
            "要具体、可判定，不要正确的废话。key 用小写字母/数字/连字符。"
        ),
        user=(
            f"踩因：{reason}\n"
            f"代表性评论：{sampled or '（无）'}\n"
            "请提炼一条规约，避免这类回复问题再次出现。"
        ),
        trace_id=trace_id,
    )
    if not result.ok or result.data is None:
        logger.warning("规约提炼失败 reason=%s：%s", reason, result.reason)
        return None
    draft = result.data
    # key 规范化：非法字符一律压成连字符，进不了命名空间的草稿不落库
    slug = re.sub(r"[^a-z0-9]+", "-", draft.key.lower()).strip("-")
    if not slug:
        logger.warning("规约草稿 key 非法：%r", draft.key)
        return None
    draft = RuleDraft(
        key=slug[:40], rule=draft.rule.strip()[:MAX_RULE_CHARS], rationale=draft.rationale
    )
    if not draft.rule:
        return None
    return draft


def upsert_biz_rule(content: str, draft: RuleDraft) -> str:
    """把 ``loop3-<key>: <rule>`` 写进 biz 层内容：同名覆盖、异名追加。

    与装配器的项名语义（行首 ``key:`` 前缀）对齐；只碰 ``loop3-`` 命名空间，
    人工条目原样保留。
    """
    line = f"{KEY_PREFIX}{draft.key}: {draft.rule}"
    full_key = f"{KEY_PREFIX}{draft.key}"
    out: list[str] = []
    replaced = False
    for raw in content.splitlines():
        head = raw.split(":", 1)[0].strip().lstrip("- ").strip().lower()
        if head == full_key:
            out.append(line)
            replaced = True
        else:
            out.append(raw)
    if not replaced:
        out.append(line)
    return "\n".join(out)


async def apply_prompt_rule(
    rules_repo: Any,
    cap_repo: Any,
    *,
    draft: RuleDraft,
    down_votes: int,
    reason: str,
) -> AppliedRule:
    """写 biz 层（版本递增，下次装配即生效）+ rule_change_log 留痕。"""
    current = await rules_repo.load_biz_rules()
    content = current.content if current is not None else ""
    new_content = upsert_biz_rule(content, draft)
    version = await rules_repo.save_biz_rules(new_content, updated_by="loop_digest")
    await cap_repo.record_rule_change(
        kind="prompt",
        rule_version_old=f"biz:{current.version}" if current is not None else None,
        rule_version_new=f"biz:{version}",
        detail={
            "key": f"{KEY_PREFIX}{draft.key}",
            "rule": draft.rule,
            "rationale": draft.rationale,
            "down_reason": reason,
        },
        sample_size=down_votes,
    )
    logger.info(
        "Loop-3 自动更新 biz 层规约 %s（biz:%s，踩因 %s ×%d）",
        draft.key,
        version,
        reason,
        down_votes,
    )
    return AppliedRule(
        key=f"{KEY_PREFIX}{draft.key}", rule=draft.rule, down_votes=down_votes,
        biz_version=version,
    )


async def run_push_loop(
    cap_repo: Any,
    rules_repo: Any,
    agent: Any,
    *,
    window_days: int = 7,
    trace_id: str | None = None,
) -> PushLoopReport:
    """消息回复 Loop 一圈：聚合 👎 → 达阈值的踩因提炼规约 → 自动写 biz 层。"""
    rows = await cap_repo.loop_down_votes(window_days=window_days)
    groups = group_down_votes(rows)
    applied: list[AppliedRule] = []
    skipped: list[str] = []
    # 按踩数降序，最多的问题优先；每周限量防规则爆炸
    for reason, members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(applied) >= MAX_RULES_PER_RUN:
            skipped.append(f"{reason}（本周名额已满）")
            continue
        if len(members) < MIN_DOWN_VOTES:
            skipped.append(f"{reason}（{len(members)} 条 < {MIN_DOWN_VOTES}）")
            continue
        draft = await distill_rule(
            agent,
            reason,
            [str(m["comment"] or "") for m in members],
            trace_id=trace_id,
        )
        if draft is None:
            skipped.append(f"{reason}（提炼失败）")
            continue
        applied.append(
            await apply_prompt_rule(
                rules_repo, cap_repo, draft=draft, down_votes=len(members), reason=reason
            )
        )
    return PushLoopReport(
        window_days=window_days,
        down_votes=len(rows),
        applied=tuple(applied),
        skipped=tuple(skipped),
    )


def render_push_notice(report: PushLoopReport) -> str:
    """群内通知：自动写入了哪些规约、依据多少踩、怎么回退。"""
    lines = [
        f"【WeTag Loop 周报】近 {report.window_days} 天消息回复反馈："
        f"👎 {report.down_votes} 条，已自动更新 {len(report.applied)} 条回复规约",
    ]
    for a in report.applied:
        lines.append(f"- {a.key}：{a.rule}（依据 👎×{a.down_votes}，biz 层 v{a.biz_version}）")
    lines.append("变更已存档 rule_change_log；回退：删除 biz 层规则中对应 loop3- 条目即恢复")
    return "\n".join(lines)


__all__ = [
    "KEY_PREFIX",
    "MAX_RULES_PER_RUN",
    "MIN_DOWN_VOTES",
    "AppliedRule",
    "PushLoopReport",
    "RuleDraft",
    "apply_prompt_rule",
    "distill_rule",
    "group_down_votes",
    "render_push_notice",
    "run_push_loop",
    "upsert_biz_rule",
]
