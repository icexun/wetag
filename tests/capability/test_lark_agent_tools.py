"""lark_tools（Agent 飞书工具）测试：fake spawn 断言 argv 与返回包装。"""

import json

import pytest

from app.capability.agents import lark_tools
from app.platform.cli.executor import CliExecutor
from app.platform.cli.registry import LARK_COMMANDS

pytestmark = pytest.mark.asyncio


class FakeSpawn:
    def __init__(self, payload: bytes = b'{"ok": true, "data": {"url": "https://x"}}'):
        self._payload = payload
        self.calls: list[list[str]] = []

    async def __call__(self, argv, *, env, cwd):
        self.calls.append(list(argv))
        return 0, self._payload, b""


def _text(resp) -> str:
    return resp.content[0]["text"]


@pytest.fixture()
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeSpawn:
    spawn = FakeSpawn()
    ex = CliExecutor(
        workspace_id="alpha",
        binaries={"lark": "lark-cli"},
        secrets={"LARK_CLI_TOKEN": "tk"},
        specs=LARK_COMMANDS,
        spawn=spawn,
    )
    monkeypatch.setattr(lark_tools, "_executor", ex)
    return spawn


async def test_create_argv(fake: FakeSpawn):
    out = _text(await lark_tools.feishu_doc_create("标题", "# 正文", parent_token="fld123"))
    argv = fake.calls[0]
    assert argv[:4] == ["lark-cli", "--as", "user", "docs"]
    assert "+create" in argv
    # 正文是 argv 独立元素，不经 shell
    assert "# 正文" in argv
    assert "--doc-format" in argv and "markdown" in argv
    assert "fld123" in argv
    assert "https://x" in out


async def test_create_without_parent(fake: FakeSpawn):
    await lark_tools.feishu_doc_create("标题", "正文")
    assert "--parent-token" not in fake.calls[0]


async def test_create_parent_url_normalized_to_bare_token(fake: FakeSpawn):
    """parent_token 误传完整 wiki 链接时自动提取裸 token（飞书只认裸 token）。"""
    await lark_tools.feishu_doc_create(
        "标题", "正文",
        parent_token="https://example.feishu.cn/wiki/WikiNewDocParent0001",
    )
    argv = fake.calls[0]
    assert "WikiNewDocParent0001" in argv
    assert not any("://" in a for a in argv)


async def test_append_uses_update(fake: FakeSpawn):
    await lark_tools.feishu_doc_append("https://doc", "追加内容")
    argv = fake.calls[0]
    assert "+update" in argv
    assert argv[argv.index("--command") + 1] == "append"
    assert argv[argv.index("--doc-format") + 1] == "markdown"


async def test_fetch_and_search(fake: FakeSpawn):
    await lark_tools.feishu_doc_fetch("https://doc")
    argv = fake.calls[0]
    assert "+fetch" in argv
    assert argv[argv.index("--detail") + 1] == "with-ids"
    await lark_tools.feishu_doc_search("关键词")
    argv = fake.calls[1]
    assert "+search" in argv
    assert argv[argv.index("--query") + 1] == "关键词"


async def test_update_str_replace(fake: FakeSpawn):
    await lark_tools.feishu_doc_update(
        "https://doc", "str_replace", "新文本", pattern="旧文本"
    )
    argv = fake.calls[0]
    assert "+update" in argv
    assert argv[argv.index("--command") + 1] == "str_replace"
    assert argv[argv.index("--pattern") + 1] == "旧文本"
    assert argv[argv.index("--doc-format") + 1] == "markdown"


async def test_update_block_replace(fake: FakeSpawn):
    await lark_tools.feishu_doc_update(
        "https://doc", "block_replace", "新块内容", block_id="blk123"
    )
    argv = fake.calls[0]
    assert argv[argv.index("--command") + 1] == "block_replace"
    assert argv[argv.index("--block-id") + 1] == "blk123"
    assert "--pattern" not in argv


async def test_failure_returns_error_json(monkeypatch: pytest.MonkeyPatch):
    spawn = FakeSpawn(b'{"ok": false, "error": {"message": "no permission"}}')
    ex = CliExecutor(
        workspace_id="alpha",
        binaries={"lark": "lark-cli"},
        secrets={"LARK_CLI_TOKEN": "tk"},
        specs=LARK_COMMANDS,
        spawn=spawn,
        max_attempts=1,
    )
    monkeypatch.setattr(lark_tools, "_executor", ex)
    out = json.loads(_text(await lark_tools.feishu_doc_fetch("https://doc")))
    assert out["ok"] is False
    assert "error_code" in out


def test_risk_levels_registered():
    specs = {s.key: s for s in LARK_COMMANDS}
    assert specs["lark.docs.create"].risk.value == "HIGH_WRITE"
    assert specs["lark.docs.search"].risk.value == "READ"
    assert specs["lark.docs.create"].env_keys == ("LARK_CLI_TOKEN",)


# -- 写成功 → 即时派生 index_refresh（变更通知链的群聊直触入口）----------------


@pytest.fixture()
def sink():
    """派生通道替身；每个用例跑完复位，防跨用例泄漏。"""
    calls: list = []

    async def _append(spec):  # sink 是 async 协议
        calls.append(spec)

    lark_tools.bind_derived_sink(_append)
    yield calls
    lark_tools.bind_derived_sink(None)


async def test_update_success_derives_index_refresh(fake: FakeSpawn, sink):
    await lark_tools.feishu_doc_update(
        "https://example.feishu.cn/docx/ABC123", "str_replace", "新文本", pattern="旧"
    )
    assert len(sink) == 1
    spec = sink[0]
    assert spec.job_name == "index_refresh"
    assert spec.payload["document_ids"] == ["ABC123"]  # 裸 token
    assert spec.payload["source"] == "chat_write"


async def test_derived_task_carries_the_initiator_and_chat(sink):
    """派生任务必须带上归属：发起人进 payload，群进 TaskSpec 列。

    群聊里 @机器人 改文档的人是「变更的发起人」——不随派生链传下去，
    后续协调待办就不知道该派给谁，通知也不知道发回哪个群。
    """
    token = lark_tools.bind_task_context(
        actor_user_id="user_asker", chat_id="chat1", topic_id="t1"
    )
    try:
        await lark_tools._notify_doc_changed("https://doc/ABC", summary="改了额度")
    finally:
        lark_tools.unbind_task_context(token)
    assert len(sink) == 1
    spec = sink[0]
    assert spec.payload["initiator_user_id"] == "user_asker"
    assert spec.chat_id == "chat1"
    assert spec.topic_id == "t1"


async def test_derived_task_without_bound_context_has_no_initiator(sink):
    """轮询/脚本链没有绑上下文：initiator 为空串而不是编一个。"""
    await lark_tools._notify_doc_changed("https://doc/ABC", summary="改了额度")
    assert sink[0].payload["initiator_user_id"] == ""
    assert sink[0].chat_id is None


async def test_append_success_derives_index_refresh(fake: FakeSpawn, sink):
    await lark_tools.feishu_doc_append("https://example.feishu.cn/wiki/WIKI1", "追加")
    assert len(sink) == 1
    assert sink[0].payload["document_ids"] == ["WIKI1"]


async def test_create_success_derives_with_response_token(fake: FakeSpawn, sink):
    await lark_tools.feishu_doc_create("标题", "正文")
    # FakeSpawn 默认返回 {"url": "https://x"} → 从响应取 token
    assert len(sink) == 1
    assert sink[0].payload["change_summary"].startswith("新建：")


async def test_failed_write_derives_nothing(monkeypatch: pytest.MonkeyPatch, sink):
    spawn = FakeSpawn(b'{"ok": false, "error": {"message": "no permission"}}')
    ex = CliExecutor(
        workspace_id="alpha",
        binaries={"lark": "lark-cli"},
        secrets={"LARK_CLI_TOKEN": "tk"},
        specs=LARK_COMMANDS,
        spawn=spawn,
        max_attempts=1,
    )
    monkeypatch.setattr(lark_tools, "_executor", ex)
    await lark_tools.feishu_doc_update("https://doc", "str_replace", "新", pattern="旧")
    assert sink == []


async def test_sink_unbound_is_noop(fake: FakeSpawn):
    """未接线（脚本/单测）时静默跳过——轮询侧仍是兜底触发。"""
    lark_tools.bind_derived_sink(None)
    await lark_tools.feishu_doc_update("https://doc", "str_replace", "新", pattern="旧")
    # 不抛异常即通过
