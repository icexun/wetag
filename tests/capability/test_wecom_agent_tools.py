import json
from types import SimpleNamespace

import pytest

from app.capability.agents import wecom_tools
from app.contracts.ids import TodoId, UserId

pytestmark = pytest.mark.asyncio


class FakeTodos:
    def __init__(self) -> None:
        self.created = []

    async def resolve_user(self, name):
        return SimpleNamespace(user_id=UserId(f"uid_{name}"), display_name=name)

    async def create(self, cmd, idem_key):
        self.created.append((cmd, idem_key))
        return SimpleNamespace(todo_id=TodoId("todo-1"))


class FakeRefs:
    def __init__(self) -> None:
        self.registered = []

    async def register(self, todo_id, chat_id, *, topic_id=None, source):
        self.registered.append((todo_id, chat_id, topic_id, source))


def _payload(response):
    return json.loads(response.content[0]["text"])


async def test_agent_creates_native_todo_for_current_actor():
    todos = FakeTodos()
    refs = FakeRefs()
    wecom_tools.bind_dependencies(todos, refs)
    token = wecom_tools.bind_task_context(
        task_id="task-1",
        actor_user_id="actor-1",
        chat_id="chat-1",
        topic_id="topic-1",
    )
    try:
        result = await wecom_tools.wecom_todo_create("跟进冲突", "确认处理方向")
    finally:
        wecom_tools.unbind_task_context(token)
        wecom_tools.bind_dependencies(None)

    assert _payload(result)["ok"] is True
    cmd, idem_key = todos.created[0]
    assert tuple(cmd.assignee_ids) == ("actor-1",)
    assert str(idem_key) == "agent_todo:task-1"
    assert refs.registered == [("todo-1", "chat-1", "topic-1", "agent")]


async def test_agent_resolves_explicit_assignee_names():
    todos = FakeTodos()
    wecom_tools.bind_dependencies(todos)
    token = wecom_tools.bind_task_context(
        task_id="task-2",
        actor_user_id="actor-1",
        chat_id="chat-1",
        topic_id=None,
    )
    try:
        result = await wecom_tools.wecom_todo_create(
            "协调", assignee_names=["张三", "李四"]
        )
    finally:
        wecom_tools.unbind_task_context(token)
        wecom_tools.bind_dependencies(None)

    assert _payload(result)["ok"] is True
    cmd, _ = todos.created[0]
    assert tuple(cmd.assignee_ids) == ("uid_张三", "uid_李四")
