"""飞书 / 企微协作工具：经真 ``CliExecutor`` 回放 CLI 输出，钉住参数渲染、安全拒绝与返回处理。"""

from __future__ import annotations

import json
from typing import Any, Sequence

import pytest

from app.capability.agents import lark_office_tools as lk
from app.capability.agents import wecom_office_tools as wc
from app.contracts.ports import parse_integrations
from app.platform.cli.executor import CliExecutor
from app.platform.cli.registry import LARK_OFFICE_COMMANDS, WECOM_OFFICE_COMMANDS


class Spawn:
    def __init__(self) -> None:
        self.reply: Any = {"ok": True, "data": {}}
        self.code = 0
        self.calls: list[list[str]] = []

    async def __call__(
        self, argv: Sequence[str], *, env: dict[str, str], cwd: str | None
    ) -> tuple[int, bytes, bytes]:
        self.calls.append(list(argv))
        return self.code, json.dumps(self.reply, ensure_ascii=False).encode(), b""

    def flag(self, name: str) -> str:
        argv = self.calls[-1]
        return argv[argv.index(name) + 1]

    def body(self) -> dict[str, Any]:
        return json.loads(self.flag("--json"))


def _executor(spawn: Spawn, specs: Any) -> CliExecutor:
    return CliExecutor(
        workspace_id="alpha",
        binaries={"lark": "lark-cli", "wecom": "wecom-cli"},
        specs=specs,
        spawn=spawn,
        max_attempts=1,
    )


@pytest.fixture
def lark() -> Spawn:
    s = Spawn()
    lk.bind_dependencies(_executor(s, LARK_OFFICE_COMMANDS))
    yield s
    lk.bind_dependencies(None)


@pytest.fixture
def wecom() -> Spawn:
    s = Spawn()
    s.reply = {"errcode": 0, "errmsg": "ok"}
    wc.bind_dependencies(_executor(s, WECOM_OFFICE_COMMANDS))
    yield s
    wc.bind_dependencies(None)


def _out(resp: Any) -> Any:
    return json.loads(resp.content[0]["text"])


# --- 飞书 -----------------------------------------------------------------


async def test_lark_sheet_read_renders_user_identity_and_sheet(lark: Spawn) -> None:
    await lk.feishu_sheet_read("https://x.feishu.cn/sheets/shtA", sheet_name="Sheet1", range="A1:B2")
    argv = lark.calls[-1]
    assert argv[:4] == ["lark-cli", "--as", "user", "sheets"]
    assert lark.flag("--sheet-name") == "Sheet1" and lark.flag("--range") == "A1:B2"
    assert "--sheet-id" not in argv


async def test_lark_sheet_requires_exactly_one_sheet_ref(lark: Spawn) -> None:
    out = _out(await lk.feishu_sheet_read("https://x.feishu.cn/sheets/shtA"))
    assert out["ok"] is False and out["error_code"] == "INVALID_ARGUMENT"
    assert lark.calls == []


async def test_lark_base_upsert_takes_token_from_url(lark: Spawn) -> None:
    await lk.feishu_base_upsert("https://x.feishu.cn/base/bascnXYZ?table=t", "tbl1", {"名称": "x"})
    assert lark.flag("--base-token") == "bascnXYZ"
    assert json.loads(lark.flag("--json")) == {"名称": "x"}
    assert "--record-id" not in lark.calls[-1]


async def test_lark_task_create_idempotency_key_is_stable(lark: Spawn) -> None:
    await lk.feishu_task_create("写周报", due="2026-10-20")
    first = lark.flag("--idempotency-key")
    await lk.feishu_task_create("写周报", due="2026-10-20")
    assert lark.flag("--idempotency-key") == first and first.startswith("wetag-")
    await lk.feishu_task_create("写月报", due="2026-10-20")
    assert lark.flag("--idempotency-key") != first


async def test_lark_my_open_tasks_pins_incomplete_filter(lark: Spawn) -> None:
    await lk.feishu_task_my_open()
    assert "--complete=false" in lark.calls[-1]


async def test_lark_error_envelope_surfaces(lark: Spawn) -> None:
    lark.reply = {"ok": False, "error": {"type": "authorization", "subtype": "missing_scope",
                                         "message": "missing required scope(s): task:task:read"}}
    out = _out(await lk.feishu_task_search("周报"))
    assert out["ok"] is False and out["error_code"] == "PERMISSION_DENIED"
    assert "task:task:read" in out["stderr"]


# --- 企微 -----------------------------------------------------------------


async def test_wecom_message_send_body(wecom: Spawn) -> None:
    await wc.wecom_message_send("wrXXX", "x" * 3000)
    assert wecom.calls[-1][:3] == ["wecom-cli", "message", "send"]
    body = wecom.body()
    assert body["chat_id"] == "wrXXX" and body["msg_type"] == "text"
    assert len(body["text"]["content"]) == 2048


async def test_wecom_business_error_keeps_full_help_message(wecom: Spawn) -> None:
    """wecom-cli 业务错误退出码 1、JSON 在 stdout；help_message 比摘要上限长也要完整带回。"""
    help_message = "当前机器人未被授权「文档」使用权限，[点击这里](https://work.weixin.qq.com/" + "a" * 900 + ")"
    wecom.code = 1
    wecom.reply = {"errcode": 850002, "errmsg": "no authorization, hint: [1]",
                   "results_json": None, "help_message": help_message}
    out = _out(await wc.wecom_doc_search("周报"))
    assert out["ok"] is False and out["error_code"] == "PERMISSION_DENIED"
    assert out["errcode"] == 850002 and out["help_message"] == help_message
    assert "results_json" not in out


async def test_office_tool_result_is_capped(wecom: Spawn, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OFFICE_TOOL_RESULT_MAX_CHARS", "200")
    wecom.reply = {"errcode": 0, "schedule_list": ["x" * 50] * 20}
    text = (await wc.wecom_schedule_list()).content[0]["text"]
    assert len(text) < 300 and "已截断" in text


async def test_wecom_success_drops_identity_noise(wecom: Spawn) -> None:
    wecom.reply = {"errcode": 0, "items": [1], "extra_identity_context": {"bot": "x"}}
    out = _out(await wc.wecom_schedule_list())
    assert out == {"errcode": 0, "items": [1]}
    assert set(wecom.body()) == {"begin_time", "end_time"}  # 默认补今后 7 天


async def test_wecom_schedule_create_attendees(wecom: Spawn) -> None:
    await wc.wecom_schedule_create("周会", "2026-10-14 10:00:00", "2026-10-14 11:00:00", "u1，u2")
    body = wecom.body()
    assert body["attendees"] == [{"userid": "u1"}, {"userid": "u2"}]
    assert "location" not in body


async def test_wecom_smartsheet_records_use_field_titles(wecom: Spawn) -> None:
    await wc.wecom_smartsheet_update("dc1", "s1", [{"record_id": "r1", "values": {"标题": "y"}}])
    body = wecom.body()
    assert body["key_type"] == "field_title"
    assert body["records"] == [{"record_id": "r1", "values": {"标题": "y"}}]
    assert "type" not in body


async def test_wecom_mail_send_has_no_attachments(wecom: Spawn) -> None:
    await wc.wecom_mail_send("主题", "正文", to_userids="u1", to_emails="a@b.com")
    body = wecom.body()
    assert body["to"] == {"userids": ["u1"], "emails": ["a@b.com"]}
    assert "cc" not in body and "attachments" not in body


@pytest.mark.parametrize(
    ("key", "body"),
    [
        ("wecom.smartsheet.records_update", {"docid": "d", "type": "delete", "records": []}),
        ("wecom.mail.send", {"subject": "s", "file_path": "/etc/passwd"}),
        ("wecom.mail.send", {"subject": "s", "attachments": [{"file_path": "/etc/passwd"}]}),
        ("wecom.doc.get", {"docid": "d", "output": "/tmp/x"}),
    ],
)
async def test_wecom_registry_rejects_delete_and_local_paths(
    wecom: Spawn, key: str, body: dict[str, Any]
) -> None:
    r = await wc._get_executor().run(key, {"json": json.dumps(body)})
    assert r.ok is False and str(r.error_code) == "INVALID_ARGUMENT"
    assert wecom.calls == []


# --- 集成开关 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, {"tapd", "lark", "wecom"}),
        ("", {"tapd", "lark", "wecom"}),
        ('["wecom"]', {"wecom"}),
        ("wecom, LARK, jira", {"wecom", "lark"}),
        ("[]", set()),
    ],
)
def test_parse_integrations(raw: Any, expected: set[str]) -> None:
    assert parse_integrations(raw) == frozenset(expected)


def test_tools_registered_only_for_enabled_integrations_with_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from utils import active_integration_tools

    monkeypatch.setenv("INTEGRATIONS", "tapd,wecom")
    for key in ("LARK_CLI_BIN", "TAPD_CLI_BIN", "WECOM_CLI_BIN"):
        monkeypatch.delenv(key, raising=False)
    groups = {"lark": ("l",), "tapd": ("t",), "wecom": ("w",)}
    active = active_integration_tools(
        groups, which=lambda b: None if b == "tapd-cli" else f"/bin/{b}"
    )
    assert active == {"wecom": ("w",)}


def test_skills_of_disabled_integrations_not_loaded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import utils

    for name in ("code-search", "feishu-doc", "tapd-requirement", "wecom-office"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "SKILL.md").write_text(f"# {name}", encoding="utf-8")
    monkeypatch.setenv("INTEGRATIONS", "wecom")
    monkeypatch.setattr("shutil.which", lambda b: f"/bin/{b}")
    text = utils.load_skills(str(tmp_path))
    assert "# code-search" in text and "# wecom-office" in text
    assert "feishu-doc" not in text and "tapd-requirement" not in text
