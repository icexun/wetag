"""四级分档判定与 Auto Mode 规则匹配（UT，无 DB）。

covers: TC-051, TC-055, TC-060
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.control.approval.auto_mode import AutoModeMatcher
from app.control.approval.classifier import RiskLevel, classify
from app.control.approval.gate import ApprovalGate, GateContext
from app.control.approval.pending import (
    ApprovalStatus,
    PendingApproval,
    call_digest,
)
from app.contracts.errors import ApprovalRequired


def _chunk(text: str):
    return SimpleNamespace(content=[{"type": "text", "text": text}])


def _next_handler(executed: list[str]):
    """仿 agentscope：next_handler 是协程，返回 async generator。"""

    async def _gen(**kwargs):
        executed.append(kwargs["tool_call"]["name"])
        yield _chunk("ok")

    async def _handler(**kwargs):
        return _gen(**kwargs)

    return _handler


async def _drive(gate: ApprovalGate, tool_call: dict, next_handler) -> list:
    chunks = []
    async for chunk in gate.middleware({"tool_call": tool_call}, next_handler):
        chunks.append(chunk)
    return chunks


class _FakeApprovalRepo:
    """内存版 pending_approval，供 UT 驱动闸门（不落库）。"""

    def __init__(self) -> None:
        self.rows: dict = {}

    async def find_by_call(self, task_id, digest):
        return next(
            (
                r
                for r in self.rows.values()
                if r.task_id == task_id and r.tool_call_digest == digest
            ),
            None,
        )

    async def create(self, **kw):
        approval = PendingApproval(
            id=uuid4(), workspace_id="t", status=ApprovalStatus.PENDING, **kw
        )
        self.rows[approval.id] = approval
        return approval

    async def set_card_msg_id(self, approval_id, card_msg_id):
        self.rows[approval_id].card_msg_id = card_msg_id


# ---- TC-051 只读调用直接执行不创建审批 (US7-1) ----


def test_classify_read_tools():
    """covers: TC-051 —— 读文档 / 检索代码 / 召回记忆均为 READ。"""
    assert classify("doc.get", {"doc_id": "d1"}) is RiskLevel.READ
    assert classify("repo.search", {"query": "q"}) is RiskLevel.READ
    assert classify("memory.recall", {"query": "q"}) is RiskLevel.READ


def test_classify_mcp_underscore_read_tools():
    """外接 MCP 工具名为下划线 / 驼峰形态：读操作必须命中 READ，否则被审批闸门误拦。"""
    assert classify("FooController_searchFoo", {}) is RiskLevel.READ
    assert classify("FooController_getFoo", {}) is RiskLevel.READ
    assert classify("FooController_batchGetFoos", {}) is RiskLevel.READ
    # 未登记的写操作默认高危写
    assert classify("FooController_updateFoo", {}) is RiskLevel.HIGH_WRITE


def test_classify_tapd_tools():
    for name in (
        "tapd_story_list", "tapd_bug_list", "tapd_task_list", "tapd_comment_list",
        "tapd_iteration_list", "tapd_member_list", "tapd_fields",
        "tapd_story_changes", "tapd_story_related_bugs",
    ):
        assert classify(name, {}) is RiskLevel.READ, name
    # TAPD 写操作显式低危写：直通，只留审计
    for name in (
        "tapd_story_create", "tapd_story_update", "tapd_bug_create", "tapd_bug_update",
        "tapd_task_create", "tapd_task_update", "tapd_comment_add",
    ):
        assert classify(name, {}) is RiskLevel.LOW_WRITE, name


def test_every_office_tool_has_explicit_risk_level():
    """飞书 / 企微协作工具全部显式登记，不靠名称启发式（如 feishu_base_tables 会被兜成高危写）。"""
    from app.capability.agents.lark_office_tools import AGENT_LARK_OFFICE_TOOLS
    from app.capability.agents.wecom_office_tools import AGENT_WECOM_OFFICE_TOOLS
    from app.control.approval.classifier import DEFAULT_RISK_TABLE

    names = [fn.__name__ for fn in (*AGENT_LARK_OFFICE_TOOLS, *AGENT_WECOM_OFFICE_TOOLS)]
    assert [n for n in names if n not in DEFAULT_RISK_TABLE] == []
    for name in ("wecom_message_send", "wecom_mail_send", "feishu_base_upsert",
                 "feishu_sheet_write_csv", "wecom_smartsheet_update"):
        assert classify(name, {}) is RiskLevel.HIGH_WRITE, name
    for name in ("feishu_task_create", "wecom_meeting_create"):
        assert classify(name, {}) is RiskLevel.LOW_WRITE, name


def test_classify_delete_is_forbidden():
    """covers: TC-055 —— 删除类操作判 FORBIDDEN。"""
    assert classify("doc.delete", {"doc_id": "d1"}) is RiskLevel.FORBIDDEN
    assert classify("requirement.remove_card", {}) is RiskLevel.FORBIDDEN


async def test_forbidden_rejected_even_with_auto_rule():
    """covers: TC-055 —— 存在匹配的自动批准规则时删除仍被拒绝，不执行、不建审批。"""
    repo = _FakeApprovalRepo()
    matcher = AutoModeMatcher([{"tool": "doc.*"}], rule_version="v1")
    gate = ApprovalGate(repo, auto_matcher=matcher)  # type: ignore[arg-type]
    executed: list[str] = []
    chunks = await _drive(
        gate, {"name": "doc.delete", "input": {"doc_id": "d1"}}, _next_handler(executed)
    )
    assert executed == []  # 未执行
    assert repo.rows == {}  # 未创建确认请求
    assert "拒绝" in chunks[-1].content[0]["text"]


# ---- TC-060 无法解析的自动规则安全地转入审批 (US7-10) ----


def test_unparseable_rules_match_miss_with_alert(alerts):
    """covers: TC-060 —— 规则不可解析 → 未命中 + 配置错误告警，不放行。"""
    matcher = AutoModeMatcher("{not a json", workspace_id="t")
    decision = matcher.match("doc.update", {"doc_id": "d1"})
    assert decision.hit is False
    assert decision.error is not None
    assert any("自动批准规则" in a.title for a in alerts)


async def test_unparseable_rules_fall_into_approval(alerts):
    """covers: TC-060 —— 高危写到达时按未命中进入确认流程（挂起），不直接放行。"""
    repo = _FakeApprovalRepo()
    matcher = AutoModeMatcher("[1, 2]", workspace_id="t")  # 合法 JSON 但形态非法
    gate = ApprovalGate(repo, auto_matcher=matcher)  # type: ignore[arg-type]
    ctx = GateContext(task_id=uuid4(), chat_id="chat-1")
    token = gate.bind(ctx)
    executed: list[str] = []
    try:
        with pytest.raises(ApprovalRequired):
            await _drive(
                gate,
                {"name": "doc.update", "input": {"doc_id": "d1"}},
                _next_handler(executed),
            )
    finally:
        gate.unbind(token)
    assert executed == []  # 未放行
    assert len(repo.rows) == 1  # 已建确认请求
    approval = next(iter(repo.rows.values()))
    assert approval.status == ApprovalStatus.PENDING
    assert approval.tool_call_digest == call_digest("doc.update", {"doc_id": "d1"})
    assert approval.expires_at > datetime.now(UTC)
    assert approval.expires_at <= datetime.now(UTC) + timedelta(minutes=31)


# ---- AutoModeMatcher 命中语义（支撑 TC-054 的单元面） ----


def test_auto_rule_hit_with_version():
    matcher = AutoModeMatcher(
        [{"tool": "doc.update", "params": {"doc_id": "^doc-.*"}}], rule_version="v3"
    )
    decision = matcher.match("doc.update", {"doc_id": "doc-100"})
    assert decision.hit is True
    assert decision.rule_version == "v3"
    assert matcher.match("doc.update", {"doc_id": "other-1"}).hit is False
    assert matcher.match("doc.delete", {"doc_id": "doc-100"}).hit is False


def test_auto_rule_scoped_doc_all_commands_pass():
    """按 doc_url 圈定的 feishu_doc_update：同文档所有 command 免审批。

    「改个标题还要再批一次」的根因是审批按单次调用摘要对账；Auto Mode
    按 tool+params 匹配，天然不受 command 变化影响。
    """
    matcher = AutoModeMatcher(
        [{
            "tool": "feishu_doc_update",
            "params": {"doc_url": r"^https://example\.feishu\.cn/wiki/JRxV.*"},
        }],
        rule_version="v1",
    )
    doc = "https://example.feishu.cn/wiki/JRxVWikiDemoToken0001"
    for command in ("append", "str_replace", "block_insert_after", "block_replace"):
        assert matcher.match("feishu_doc_update", {"command": command, "doc_url": doc}).hit
    # 圈外文档不命中，仍走人工审批
    assert not matcher.match(
        "feishu_doc_update",
        {"command": "append", "doc_url": "https://other.example.com/wiki/x"},
    ).hit


def test_auto_rule_command_regex_excludes_overwrite():
    """command 正则排除 overwrite：整篇重写不随免审批规则放行。"""
    matcher = AutoModeMatcher(
        [{
            "tool": "feishu_doc_update",
            "params": {"command": r"^(append|str_replace|block_insert_after|block_replace)$"},
        }],
        rule_version="v1",
    )
    assert matcher.match("feishu_doc_update", {"command": "str_replace"}).hit
    assert not matcher.match("feishu_doc_update", {"command": "overwrite"}).hit
