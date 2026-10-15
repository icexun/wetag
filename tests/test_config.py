"""config 加载层：优先级与 workspace YAML 展开；人设模板渲染。"""

from __future__ import annotations

import config
from utils import render_persona


def test_workspace_yaml_flattens_and_expands(tmp_path, monkeypatch):
    (tmp_path / "w1.yaml").write_text(
        "tapd:\n  project_id: 42\nwecom:\n  bot_id: ${BOT_X}\n  secret: ${MISSING_X}\n"
        "flag: true\nitems: [a, b]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BOT_X", "bot-1")
    monkeypatch.delenv("MISSING_X", raising=False)

    values = config.load_workspace_file("w1", tmp_path)

    assert values["TAPD_PROJECT_ID"] == "42"
    assert values["WECOM_BOT_ID"] == "bot-1"
    assert "WECOM_SECRET" not in values
    assert values["FLAG"] == "true"
    assert values["ITEMS"] == '["a", "b"]'


def test_missing_workspace_file_is_empty(tmp_path):
    assert config.load_workspace_file("nope", tmp_path) == {}


def test_merge_keeps_higher_layer(monkeypatch):
    monkeypatch.setenv("CFG_T_KEEP", "env")
    monkeypatch.delenv("CFG_T_NEW", raising=False)
    monkeypatch.delenv("CFG_T_EMPTY", raising=False)

    config._merge({"CFG_T_KEEP": "yaml", "CFG_T_NEW": "yaml", "CFG_T_EMPTY": ""})

    assert config.get("CFG_T_KEEP") == "env"
    assert config.get("CFG_T_NEW") == "yaml"
    assert config.get("CFG_T_EMPTY", "d") == "d"


def test_render_persona_drops_lines_with_unset_placeholders():
    template = "# {{BOT_NAME}}\n- TAPD `{{TAPD_PROJECT_ID}}`\n- 仓库 `{{REPO_ROOT}}`\n"
    values = {"BOT_NAME": "Bot", "TAPD_PROJECT_ID": "7"}

    out = render_persona(template, values.get)

    assert out == "# Bot\n- TAPD `7`"
