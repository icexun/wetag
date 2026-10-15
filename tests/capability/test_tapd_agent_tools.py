"""Agent TAPD 工具：经真 ``CliExecutor`` 回放 tapd-cli 输出，钉住参数渲染与返回裁剪。"""

from __future__ import annotations

import json
from typing import Any, Sequence

import pytest

from app.capability.agents import tapd_tools as t
from app.platform.cli.executor import CliExecutor
from app.platform.cli.registry import TAPD_COMMANDS

PROJECT = "6001"


class Spawn:
    def __init__(self, reply: Any = None, code: int = 0) -> None:
        self.reply = reply if reply is not None else {"status": 1, "data": []}
        self.code = code
        self.calls: list[list[str]] = []

    async def __call__(
        self, argv: Sequence[str], *, env: dict[str, str], cwd: str | None
    ) -> tuple[int, bytes, bytes]:
        self.calls.append(list(argv))
        out = self.reply if isinstance(self.reply, str) else json.dumps(self.reply, ensure_ascii=False)
        return self.code, out.encode(), b""

    def kv(self) -> dict[str, str]:
        return dict(a.split("=", 1) for a in self.calls[-1] if "=" in a)


@pytest.fixture
def spawn() -> Spawn:
    s = Spawn()
    ex = CliExecutor(
        workspace_id="alpha",
        binaries={"tapd": "tapd-cli"},
        secrets={"TAPD_TOKEN": "tok"},
        specs=TAPD_COMMANDS,
        spawn=s,
        max_attempts=1,
    )
    t.bind_dependencies(ex, PROJECT)
    yield s
    t.bind_dependencies(None, None)


def _out(resp: Any) -> dict[str, Any]:
    return json.loads(resp.content[0]["text"])


async def test_story_list_defaults_project_and_trims_fields(spawn: Spawn) -> None:
    spawn.reply = {
        "status": 1,
        "data": [{"Story": {"id": "11", "name": "需求", "v_status": "开发中",
                            "iteration_id": "0", "begin": None}}],
    }
    out = _out(await t.tapd_story_list(v_status="开发中"))

    assert spawn.calls[-1][:3] == ["tapd-cli", "story", "list"]
    kv = spawn.kv()
    assert kv["workspaceid"] == PROJECT and kv["v_status"] == "开发中"
    assert kv["with_v_status"] == "1" and "fields" in kv
    assert "id" not in kv  # 空参数不下发
    item = out["items"][0]
    assert out["ok"] is True and item["id"] == "11"
    assert "iteration_id" not in item and "begin" not in item  # 空值裁掉
    assert item["url"].endswith(f"/{PROJECT}/prong/stories/view/11")


async def test_story_detail_returns_plain_description(spawn: Spawn) -> None:
    spawn.reply = {"status": 1, "data": [{"Story": {"id": "11", "description": "<p>背景</p><br><b>A</b>"}}]}
    out = _out(await t.tapd_story_list(story_id="11"))
    assert "fields" not in spawn.kv()
    assert out["items"][0]["description"] == "背景 A"


async def test_story_update_passes_custom_fields(spawn: Spawn) -> None:
    spawn.reply = {"status": 1, "data": {"Story": {"id": "11", "v_status": "开发中"}}}
    out = _out(await t.tapd_story_update("11", {"v_status": "开发中", "custom_field_one": "x"}))
    assert spawn.kv() == {
        "workspaceid": PROJECT, "id": "11", "v_status": "开发中", "custom_field_one": "x"
    }
    assert out["items"][0]["v_status"] == "开发中"


async def test_unregistered_field_is_rejected_before_spawn(spawn: Spawn) -> None:
    out = _out(await t.tapd_story_update("11", {"tester": "张三"}))
    assert out["ok"] is False and out["error_code"] == "INVALID_ARGUMENT"
    assert spawn.calls == []


async def test_comment_add_uses_hyphen_keys(spawn: Spawn) -> None:
    await t.tapd_comment_add("stories", "11", "## 结论\n- 通过")
    assert spawn.calls[-1][:3] == ["tapd-cli", "comment", "add"]
    assert spawn.kv() == {
        "workspaceid": PROJECT, "entry-type": "stories", "entry-id": "11",
        "description": "## 结论\n- 通过",
    }


async def test_comment_entry_type_is_closed_set(spawn: Spawn) -> None:
    out = _out(await t.tapd_comment_add("wiki", "11", "x"))
    assert out["ok"] is False and spawn.calls == []


async def test_task_create_links_story(spawn: Spawn) -> None:
    await t.tapd_task_create("子任务", story_id="11", owner="张三")
    kv = spawn.kv()
    assert kv["story_id"] == "11" and kv["parent_id"] == "11" and kv["owner"] == "张三"


async def test_fields_keeps_only_enums_and_custom(spawn: Spawn) -> None:
    spawn.reply = {
        "status": 1,
        "data": {
            "status": {"label": "状态", "options": {"status_2": "开发中"}},
            "name": {"label": "标题", "options": []},
            "custom_field_one": {"label": "文档交付链接", "options": []},
        },
    }
    out = _out(await t.tapd_fields())
    assert set(out["fields"]) == {"status", "custom_field_one"}
    assert out["fields"]["custom_field_one"]["label"] == "文档交付链接"


async def test_failure_is_reported_not_raised(spawn: Spawn) -> None:
    spawn.reply = 'API 错误 422: {"status":422,"info":"The access token provided is invalid"}'
    spawn.code = 1
    out = _out(await t.tapd_bug_list())
    assert out == {"ok": False, "error_code": "AUTH_FAILED", "stderr": out["stderr"]}


async def test_missing_project_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAPD_WORKSPACE_IDS", raising=False)
    t.bind_dependencies(object(), None)
    try:
        out = _out(await t.tapd_iteration_list())
    finally:
        t.bind_dependencies(None, None)
    assert out["ok"] is False and out["error_code"] == "CONFIG_MISSING"


def test_write_tools_are_classified() -> None:
    from app.control.approval.classifier import RiskLevel, classify

    for fn in t.AGENT_TAPD_TOOLS:
        level = classify(fn.__name__, {})
        assert level in (RiskLevel.READ, RiskLevel.LOW_WRITE), fn.__name__
        if any(w in fn.__name__ for w in ("create", "update", "add")):
            assert level is RiskLevel.LOW_WRITE, fn.__name__
