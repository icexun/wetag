"""SpaceContext 的归属等式（§4.2）。"""

from __future__ import annotations

import pytest

from app.contracts.errors import SpaceMismatchError
from app.contracts.space_context import SpaceContext

_CONTEXT = SpaceContext(
    organization_id="acme",
    workspace_id="alpha",
    bot_id="bot_alpha",
    chat_id="chat_001",
    topic_id="t_1",
    actor_user_id="user_123",
)


def test_matching_workspace_and_bot_pass():
    _CONTEXT.require_same_workspace("alpha")
    _CONTEXT.require_same_bot("bot_alpha")


def test_foreign_workspace_is_refused():
    with pytest.raises(SpaceMismatchError):
        _CONTEXT.require_same_workspace("beta")


def test_foreign_bot_is_refused():
    with pytest.raises(SpaceMismatchError):
        _CONTEXT.require_same_bot("bot_beta")


def test_round_trips_through_dict():
    """Runtime 随 Task 持久化时走这条路径，字段名用驼峰对齐 §4.2。"""
    payload = _CONTEXT.to_dict()

    assert payload["workspaceId"] == "alpha"
    assert SpaceContext.from_dict(payload) == _CONTEXT
