"""aibotid → workspace 静态映射与进程一致性校验。"""

from __future__ import annotations

import pytest

from app.space.resolver import (
    BotBinding,
    BotBindingRegistry,
    ScopeType,
    UnknownBotError,
)
from tests.space.conftest import BINDINGS


def test_binding_rejects_other_workspace_bot():
    bindings = BotBindingRegistry(dict(BINDINGS), "bot_alpha")

    with pytest.raises(UnknownBotError):
        bindings.require("bot_beta")


def test_binding_rejects_unknown_bot():
    bindings = BotBindingRegistry(dict(BINDINGS), "bot_alpha")

    with pytest.raises(UnknownBotError):
        bindings.require("bot_ghost")


def test_organization_scoped_bot():
    bindings = BotBindingRegistry(dict(BINDINGS), "bot_pub")

    assert bindings.current.is_organization_scoped
    assert bindings.current.workspace_id == "public"


def test_disabled_binding_is_refused():
    bindings = BotBindingRegistry(
        {"bot_alpha": BotBinding("bot_alpha", ScopeType.WORKSPACE, "alpha", enabled=False)},
        "bot_alpha",
    )

    with pytest.raises(UnknownBotError):
        bindings.require("bot_alpha")
