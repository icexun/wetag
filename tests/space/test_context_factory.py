"""归属解析与业务线隔离。"""

from __future__ import annotations

import pytest

from app.contracts.errors import ChatOwnershipConflict
from app.contracts.ids import ChatType
from app.space.context_factory import SpaceContextFactory
from app.space.resolver import BotBindingRegistry
from app.space.topic.router import TopicRouter
from tests.conftest import FakeSpaceRepository, inbound_ref
from tests.space.conftest import BINDINGS, factory_for


async def test_first_mention_registers_chat_and_creates_topic(space_repo):
    factory = factory_for(space_repo)

    resolution = await factory.resolve_message(inbound_ref(text="@WeTag TAPD-1001 要改"))

    assert resolution.context.workspace_id == "alpha"
    assert resolution.context.bot_id == "bot_alpha"
    assert resolution.context.organization_id == "default"
    assert resolution.route.is_new
    assert space_repo.ownership["chat_001"] == "alpha"


async def test_chat_is_locked_to_the_first_workspace():
    """一个真实群只属于一条业务线：第二条业务线的 Bot 不得接管。"""
    shared = {}
    alpha, beta = FakeSpaceRepository(shared), FakeSpaceRepository(shared)

    await factory_for(alpha, "bot_alpha").resolve_message(inbound_ref())

    with pytest.raises(ChatOwnershipConflict) as excinfo:
        await factory_for(beta, "bot_beta").resolve_message(
            inbound_ref(bot_id="bot_beta", event_id="msg_2")
        )

    assert excinfo.value.owner_workspace_id == "alpha"
    assert excinfo.value.current_workspace_id == "beta"
    # 被拒的业务线不初始化任何私有状态。
    assert beta.chat_groups == {}
    assert beta.topics == {}


async def test_single_chat_is_not_subject_to_ownership():
    """同一个人分别与两条业务线的 Bot 单聊是正常的，不能被归属互斥。"""
    shared = {}
    alpha, beta = FakeSpaceRepository(shared), FakeSpaceRepository(shared)

    await factory_for(alpha, "bot_alpha").resolve_message(
        inbound_ref(chat_id="user_777", chat_type=ChatType.SINGLE, user_id="user_777")
    )
    resolution = await factory_for(beta, "bot_beta").resolve_message(
        inbound_ref(
            chat_id="user_777",
            chat_type=ChatType.SINGLE,
            user_id="user_777",
            bot_id="bot_beta",
            event_id="msg_2",
        )
    )

    assert resolution.context.workspace_id == "beta"
    assert "user_777" not in shared


async def test_card_event_reuses_bound_topic(space_repo):
    factory = factory_for(space_repo)

    resolution = await factory.resolve_known_topic(
        inbound_ref(event_id="evt_001", text=""), topic_id="t_bound"
    )

    assert resolution.context.topic_id == "t_bound"
    assert resolution.route is None


async def test_config_version_is_carried_into_context(space_repo):
    class Versions:
        async def effective_version(self, chat_id):
            return 17

    bindings = BotBindingRegistry(dict(BINDINGS), "bot_alpha")
    factory = SpaceContextFactory(
        space_repo, bindings, TopicRouter(space_repo), Versions()
    )

    resolution = await factory.resolve_message(inbound_ref())

    assert resolution.context.effective_config_version == 17


async def test_correction_recorded_during_resolution(space_repo):
    factory = factory_for(space_repo)
    await factory.resolve_message(inbound_ref(text="登录改造"))

    resolution = await factory.resolve_message(
        inbound_ref(text="不是这个事，说的是另一个需求", event_id="msg_2")
    )

    assert resolution.correction_recorded
    assert len(space_repo.corrections) == 1
