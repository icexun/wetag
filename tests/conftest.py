"""共享 fixture：临时 schema、伪 SpaceContext、CliExecutor 替身。

Channel 与 Space 的单元测试不连数据库：SQL 正确性属集成测试范畴，这里验证的是
编排逻辑——归属校验、幂等、降级与拒绝路径。对应的替身在本文件上半部分。

`pg` 标记的测试则需要一个真 PostgreSQL。它们验证的东西**没有任何 Fake 能替代**：
`ON CONFLICT ... WHERE` 到底写没写、GIN 倒排索引 `&&` 走不走、`xmax = 0` 判断
插入还是更新、`UNIQUE` 约束在并发下是否真的唯一。这些全是 PG 的行为，不是
Python 的——用内存替身测它们，等于把被测对象换成了自己写的那个替身。

没配 `PG_ADMIN_DSN` 就 skip 而不是 fail：算法层的 300 多个测试必须能在零外部
依赖的环境下跑绿（CI、别人的笔记本、飞机上），否则跑测试这件事本身就会被放弃。
但 skip 必须**看得见**——`pytest.ini` 里的 `-ra` 保证 skip 原因打在摘要里，
不会出现"以为全跑了其实一半没跑"。

Runtime / Control 侧的用例另有一组真建 schema 的夹具（``pg_schema`` /
``ws_pair``），与上面的 ``pg_db`` 并存：前者每个用例建一个带角色的独立 schema
用来验隔离，后者复用 session 级 schema 只验 SQL 行为。两者都保留是因为建角色
比建 schema 贵得多，让所有用例都走前者会把测试跑成分钟级。
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio
import secrets
from dataclasses import dataclass
from types import SimpleNamespace
import asyncpg
from app.contracts import SpaceContext
from app.platform.db.schema import ws_role, ws_schema
from migrations.version import WORKSPACE_MIGRATIONS
from scripts.init_workspace import (
    MIGRATIONS_DIR,
    apply_migrations,
    has_statements,
    render_migration,
)
from scripts.verify_isolation import role_dsn


PG_DSN_ENV = "PG_ADMIN_DSN"

# 测试 schema 需要的迁移：0002 建 task/outbox/audit_log/runtime_state，
# 0005 建 Capability 表，0006 建 Control 台账与 Agent 工作记忆。
# 0003/0004 属于 Space/Channel，Capability 不读它们，多跑只是让每个测试慢几百毫秒。
# 0007（角色与授权）**刻意不跑**：它会 CREATE ROLE，而角色是库级对象，
# DROP SCHEMA CASCADE 清不掉，跑一次测试就在库里留一个垃圾角色。
# 隔离验收由 scripts/verify_isolation.py 在真角色上做，不在这里。
_TEST_MIGRATIONS = (
    "0002_init_workspace_schema.sql",
    "0005_capability_tables.sql",
    "0006_control_tables.sql",
)
os.environ.setdefault("ORGANIZATION_ID", "default")
os.environ.setdefault("LARK_WEB_BASE", "https://example.feishu.cn")
os.environ.setdefault("WORKSPACE_ID", "alpha")
os.environ.setdefault("WECOM_BOT_ID", "bot_alpha")
os.environ.setdefault("WECOM_BOT_SECRET", "secret_alpha")
from app.contracts.ids import ChatType  # noqa: E402
from app.contracts.ports import InboundRef  # noqa: E402
from app.gateway.repository import CardStatus, CardTask  # noqa: E402
from app.space.repository import ChatGroup, Topic  # noqa: E402
from app.space.topic.router import new_topic_id  # noqa: E402


def _dsn() -> str | None:
    return os.environ.get(PG_DSN_ENV) or None


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    dsn = _dsn()
    if not dsn:
        pytest.skip(f"未设置 {PG_DSN_ENV}，跳过真库测试")
    return dsn


@pytest_asyncio.fixture
async def pg_workspace(pg_dsn: str) -> AsyncIterator[str]:
    """建一个一次性 workspace schema，跑完 DROP CASCADE。

    schema 名带随机后缀而不是只带 pid：pid 会复用，而复用时上一轮如果崩在
    teardown 之前，新一轮就会撞上一个**有数据的** schema，测出来的
    "重复 upsert 返回 inserted=false" 是假绿。
    """
    import asyncpg

    from app.platform.db.schema import render_migration, ws_schema

    workspace_id = f"t{uuid.uuid4().hex[:12]}"
    root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "migrations")

    conn = await asyncpg.connect(pg_dsn)
    try:
        await conn.execute(f"CREATE SCHEMA {ws_schema(workspace_id)}")
        for name in _TEST_MIGRATIONS:
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                raw = fh.read()
            # 刻意**不写** org.schema_migration：那张表是已发布迁移的 checksum
            # 台账，一次性测试 schema 往里塞记录既污染台账，又会让同名 schema
            # 的第二次创建被判定为"已应用"而跳过建表。
            await conn.execute(render_migration(raw, workspace_id))
        yield workspace_id
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {ws_schema(workspace_id)} CASCADE")
        await conn.close()


@pytest_asyncio.fixture
async def pg_db(pg_dsn: str, pg_workspace: str) -> AsyncIterator["object"]:
    """接到一次性 schema 上的 ``Database``（真池、真 search_path）。"""
    from app.platform.db.pool import Database

    db = Database(pg_dsn, pg_workspace, pool_min=1, pool_max=4)
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


@pytest_asyncio.fixture
async def repo(pg_db: "object") -> "object":
    from app.capability.repository import CapabilityRepository

    return CapabilityRepository(pg_db)  # type: ignore[arg-type]


class FakeSpaceRepository:
    """SpaceRepository 的内存替身，语义与 SQL 版一致。

    ``ownership`` 对应 ``org`` schema，跨业务线共享；其余三张表是业务线私有
    schema 内的表。模拟多业务线时给每条业务线一个实例、共用一个 ownership
    字典，才与真实的 schema 布局一致。
    """

    def __init__(self, shared_ownership: Optional[Dict[str, str]] = None):
        self.ownership: Dict[str, str] = (
            shared_ownership if shared_ownership is not None else {}
        )
        self.chat_groups: Dict[str, ChatGroup] = {}
        self.topics: Dict[str, Topic] = {}
        self.corrections: List[Dict[str, Any]] = []
        # (chat_id, topic_id, text)：模拟 outbox 里 bot 发过的消息，供引用内容匹配
        self.outbound_texts: List[tuple] = []

    async def register_ownership(
        self, chat_id, workspace_id, bot_id, chat_type, actor_user_id
    ) -> str:
        return self.ownership.setdefault(chat_id, workspace_id)

    async def upsert_chat_group(
        self, chat_id, workspace_id, bot_id, chat_type, actor_user_id
    ) -> ChatGroup:
        now = datetime.now(timezone.utc)
        existing = self.chat_groups.get(chat_id)
        if existing:
            group = ChatGroup(
                chat_id=existing.chat_id,
                workspace_id=existing.workspace_id,
                bot_id=existing.bot_id,
                chat_type=existing.chat_type,
                display_name=existing.display_name,
                first_actor_user_id=existing.first_actor_user_id,
                activated_at=existing.activated_at,
                last_inbound_at=now,
            )
        else:
            group = ChatGroup(
                chat_id=chat_id,
                workspace_id=workspace_id,
                bot_id=bot_id,
                chat_type=chat_type,
                display_name=None,
                first_actor_user_id=actor_user_id,
                activated_at=now,
                last_inbound_at=now,
            )
        self.chat_groups[chat_id] = group
        return group

    async def get_chat_group(self, chat_id) -> Optional[ChatGroup]:
        return self.chat_groups.get(chat_id)

    async def set_display_name(self, chat_id, display_name) -> None:
        group = self.chat_groups[chat_id]
        self.chat_groups[chat_id] = ChatGroup(
            chat_id=group.chat_id,
            workspace_id=group.workspace_id,
            bot_id=group.bot_id,
            chat_type=group.chat_type,
            display_name=display_name,
            first_actor_user_id=group.first_actor_user_id,
            activated_at=group.activated_at,
            last_inbound_at=group.last_inbound_at,
        )

    async def create_topic(
        self, topic_id, chat_id, title, ref_keys=(), known_msg_ids=()
    ) -> Topic:
        now = datetime.now(timezone.utc)
        topic = Topic(
            topic_id=topic_id,
            chat_id=chat_id,
            title=title,
            status="ACTIVE",
            ref_keys=list(ref_keys),
            known_msg_ids=list(known_msg_ids),
            created_at=now,
            last_active_at=now,
        )
        self.topics[topic_id] = topic
        return topic

    async def get_topic(self, topic_id) -> Optional[Topic]:
        return self.topics.get(topic_id)

    async def find_topic_by_msg_id(self, chat_id, msg_id) -> Optional[Topic]:
        matches = [
            t
            for t in self.topics.values()
            if t.chat_id == chat_id and msg_id in t.known_msg_ids
        ]
        return max(matches, key=lambda t: t.last_active_at, default=None)

    async def find_topic_by_outbound_text(self, chat_id, text_prefix) -> Optional[Topic]:
        """按出站文本前缀匹配：outbound_texts 记录 (chat_id, topic_id, text)。"""
        matches = [
            t
            for c, tid, text in self.outbound_texts
            if c == chat_id and text.startswith(text_prefix)
            for t in [self.topics.get(tid)]
            if t is not None
        ]
        return max(matches, key=lambda t: t.last_active_at, default=None)

    async def find_topic_by_ref_keys(self, chat_id, ref_keys) -> Optional[Topic]:
        wanted = set(ref_keys)
        matches = [
            t
            for t in self.topics.values()
            if t.chat_id == chat_id and wanted & set(t.ref_keys)
        ]
        return max(matches, key=lambda t: t.last_active_at, default=None)

    async def list_active_topics(self, chat_id, limit) -> List[Topic]:
        active = [
            t
            for t in self.topics.values()
            if t.chat_id == chat_id and t.status == "ACTIVE"
        ]
        active.sort(key=lambda t: t.last_active_at, reverse=True)
        return active[:limit]

    async def touch_topic(self, topic_id, msg_id=None, ref_keys=()) -> None:
        topic = self.topics[topic_id]
        self.topics[topic_id] = Topic(
            topic_id=topic.topic_id,
            chat_id=topic.chat_id,
            title=topic.title,
            status=topic.status,
            ref_keys=sorted(set(topic.ref_keys) | set(ref_keys)),
            known_msg_ids=sorted(
                set(topic.known_msg_ids) | ({msg_id} if msg_id else set())
            ),
            created_at=topic.created_at,
            last_active_at=datetime.now(timezone.utc) + timedelta(microseconds=1),
        )

    async def archive_idle_topics(self, chat_id, idle_hours) -> List[str]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=idle_hours)
        archived = []
        for topic_id, topic in list(self.topics.items()):
            if (
                topic.chat_id == chat_id
                and topic.status == "ACTIVE"
                and topic.last_active_at < cutoff
            ):
                self.topics[topic_id] = Topic(
                    **{**topic.__dict__, "status": "ARCHIVED"}
                )
                archived.append(topic_id)
        return archived

    async def reactivate_topic(self, topic_id) -> None:
        topic = self.topics[topic_id]
        self.topics[topic_id] = Topic(**{**topic.__dict__, "status": "ACTIVE"})

    async def record_correction(
        self, chat_id, actor_user_id, raw_text, from_topic_id, to_topic_id
    ) -> None:
        self.corrections.append(
            {
                "chat_id": chat_id,
                "actor_user_id": actor_user_id,
                "raw_text": raw_text,
                "from_topic_id": from_topic_id,
                "to_topic_id": to_topic_id,
            }
        )


class FakeCardTaskRepository:
    """CardTaskRepository 的内存替身。

    只替身数据访问：绑定校验与 CAS 判定留给真实的 ``CardBinding``，
    否则那部分逻辑在单测里就被替身绕过了。
    """

    def __init__(self):
        self.cards: Dict[str, CardTask] = {}

    def seed(
        self,
        task_id: str,
        chat_id: str = "chat_001",
        topic_id: str = "t_1",
        bot_id: str = "bot_alpha",
        workspace_id: str = "alpha",
        allowed_actions: Sequence[str] = ("useful", "useless", "false_alarm"),
        allowed_actors: Sequence[str] = (),
        payload: Optional[Dict[str, Any]] = None,
    ) -> CardTask:
        card = CardTask(
            task_id=task_id,
            workspace_id=workspace_id,
            bot_id=bot_id,
            chat_id=chat_id,
            topic_id=topic_id,
            allowed_actions=list(allowed_actions),
            allowed_actors=list(allowed_actors),
            status=CardStatus.ACTIVE,
            payload=payload or {"title": "冲突预警"},
        )
        self.cards[task_id] = card
        return card

    async def create(
        self,
        task_id,
        workspace_id,
        bot_id,
        chat_id,
        topic_id,
        allowed_actions,
        allowed_actors=(),
        payload=None,
        ttl_hours=24,
    ) -> CardTask:
        existing = self.cards.get(task_id)
        expected = {
            "workspace_id": str(workspace_id),
            "bot_id": bot_id,
            "chat_id": str(chat_id),
            "topic_id": str(topic_id),
            "allowed_actions": list(allowed_actions),
            "allowed_actors": list(allowed_actors),
            "payload": payload or {},
        }
        if existing is not None:
            actual = {key: getattr(existing, key) for key in expected}
            if actual != expected:
                raise ValueError(
                    f"card task_id collision with different binding: {task_id}"
                )
            return existing
        return self.seed(
            task_id,
            chat_id=expected["chat_id"],
            topic_id=expected["topic_id"],
            bot_id=bot_id,
            workspace_id=expected["workspace_id"],
            allowed_actions=allowed_actions,
            allowed_actors=allowed_actors,
            payload=payload,
        )

    async def get(self, task_id) -> Optional[CardTask]:
        return self.cards.get(task_id)

    async def cas_claim(self, task_id, actor_user_id, event_key) -> Optional[CardTask]:
        card = self.cards.get(task_id)
        if card is None or card.status is not CardStatus.ACTIVE:
            return None
        claimed = CardTask(
            **{
                **card.__dict__,
                "status": CardStatus.PROCESSING,
                "acted_by": actor_user_id,
                "acted_key": event_key,
            }
        )
        self.cards[task_id] = claimed
        return claimed

    async def finish(self, task_id, status) -> None:
        card = self.cards[task_id]
        self.cards[task_id] = CardTask(**{**card.__dict__, "status": status})


class FakeFeedbackRepository:
    def __init__(self):
        self.records: List[Dict[str, Any]] = []

    async def record(
        self, response_id, chat_id, topic_id, actor_user_id, result, reason=(), comment=None
    ) -> bool:
        key = (response_id, actor_user_id, result)
        if any(
            (r["response_id"], r["actor_user_id"], r["result"]) == key
            for r in self.records
        ):
            return False
        self.records.append(
            {
                "response_id": response_id,
                "chat_id": chat_id,
                "topic_id": topic_id,
                "actor_user_id": actor_user_id,
                "result": result,
                "reason": list(reason),
                "comment": comment,
            }
        )
        return True


class FakeWSClient:
    """记录出站帧的 WSClient 替身。"""

    def __init__(self):
        self.handlers: Dict[str, Any] = {}
        self.sent: List[Dict[str, Any]] = []
        self.connected = False

    def on(self, event: str, handler) -> None:
        self.handlers[event] = handler

    async def emit(self, event: str, *args) -> None:
        handler = self.handlers.get(event)
        if handler is None:
            return
        result = handler(*args)
        if hasattr(result, "__await__"):
            await result

    async def connect(self):
        self.connected = True
        return self

    def disconnect(self) -> None:
        self.connected = False

    async def reply_stream(
        self, frame, stream_id, content, finish=False, msg_item=None, feedback=None
    ):
        self.sent.append(
            {
                "kind": "stream",
                "req_id": frame["headers"]["req_id"],
                "stream_id": stream_id,
                "content": content,
                "finish": finish,
                "feedback": feedback,
            }
        )
        return {"errcode": 0, "errmsg": "ok"}

    async def reply_template_card(self, frame, template_card, feedback=None):
        self.sent.append(
            {
                "kind": "reply_card",
                "req_id": frame["headers"]["req_id"],
                "card": template_card,
            }
        )
        return {"errcode": 0, "errmsg": "ok"}

    async def reply_welcome(self, frame, body):
        self.sent.append({"kind": "welcome", "body": body})
        return {"errcode": 0, "errmsg": "ok"}

    async def update_template_card(self, frame, template_card, userids=None):
        self.sent.append(
            {
                "kind": "update_card",
                "req_id": frame["headers"]["req_id"],
                "card": template_card,
            }
        )
        return {"errcode": 0, "errmsg": "ok"}

    async def send_message(self, chatid, body):
        self.sent.append({"kind": "push", "chat_id": chatid, "body": body})
        return {"errcode": 0, "errmsg": "ok"}

    def of_kind(self, kind: str) -> List[Dict[str, Any]]:
        return [f for f in self.sent if f["kind"] == kind]


def message_frame(
    text: str = "@WeTag 帮我看下这个需求",
    msgid: str = "msg_001",
    chat_id: str = "chat_001",
    bot_id: str = "bot_alpha",
    user_id: str = "user_123",
    chattype: str = "group",
    req_id: str = "req_001",
    quote_msgid: Optional[str] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "msgid": msgid,
        "aibotid": bot_id,
        "chattype": chattype,
        "from": {"userid": user_id},
        "msgtype": "text",
        "text": {"content": text},
    }
    if chattype == "group":
        body["chatid"] = chat_id
    if quote_msgid:
        body["quote"] = {"msgid": quote_msgid, "content": "被引用的内容"}
    return {"cmd": "aibot_msg_callback", "headers": {"req_id": req_id}, "body": body}


def event_frame(
    eventtype: str,
    detail: Optional[Dict[str, Any]] = None,
    msgid: str = "evt_001",
    chat_id: str = "chat_001",
    bot_id: str = "bot_alpha",
    user_id: str = "user_123",
    req_id: str = "req_evt_001",
    chattype: str = "group",
) -> Dict[str, Any]:
    event: Dict[str, Any] = {"eventtype": eventtype}
    if detail:
        event[eventtype] = detail
    body: Dict[str, Any] = {
        "msgid": msgid,
        "aibotid": bot_id,
        "chattype": chattype,
        "create_time": 1700000000,
        "from": {"userid": user_id},
        "msgtype": "event",
        "event": event,
    }
    if chattype == "group":
        body["chatid"] = chat_id
    return {
        "cmd": "aibot_event_callback",
        "headers": {"req_id": req_id},
        "body": body,
    }


def inbound_ref(
    text: str = "@WeTag 帮我看下这个需求",
    event_id: str = "msg_001",
    chat_id: str = "chat_001",
    bot_id: str = "bot_alpha",
    user_id: str = "user_123",
    chat_type: ChatType = ChatType.GROUP,
    quote_message_id: Optional[str] = None,
    quote_content: Optional[str] = None,
) -> InboundRef:
    """Space 侧的入站线索。Space 不认识 AgentEvent，测试也按契约递入。"""
    return InboundRef(
        event_id=event_id,
        bot_id=bot_id,
        chat_id=chat_id,
        chat_type=chat_type,
        actor_user_id=user_id,
        text=text,
        quote_message_id=quote_message_id,
        quote_content=quote_content,
    )


@pytest.fixture
def space_repo() -> FakeSpaceRepository:
    return FakeSpaceRepository()


@pytest.fixture
def card_repo() -> FakeCardTaskRepository:
    return FakeCardTaskRepository()


@pytest.fixture
def feedback_repo() -> FakeFeedbackRepository:
    return FakeFeedbackRepository()


@pytest.fixture
def ws_client() -> FakeWSClient:
    return FakeWSClient()


__all__ = [
    "ChatType",
    "FakeCardTaskRepository",
    "FakeFeedbackRepository",
    "FakeSpaceRepository",
    "FakeWSClient",
    "event_frame",
    "inbound_ref",
    "message_frame",
    "new_topic_id",
]


# --------------------------------------------------------------------------
# Runtime / Control：真建 schema 与角色的隔离夹具
# --------------------------------------------------------------------------

ADMIN_DSN = (
    os.environ.get("PG_TEST_DSN")
    or os.environ.get("PG_ADMIN_DSN")
    or os.environ.get("PG_DSN")
    or "postgresql://postgres:postgres@localhost:5432/wetag"
)
TEST_ROLE_PASSWORD = "wetag_test_pw"


def run(coro):
    """在同步 fixture 中执行异步 provisioning。"""
    return asyncio.run(coro)


@dataclass
class TempWorkspace:
    workspace: str
    schema: str
    admin_dsn: str
    role_dsn: str | None = None


async def _ensure_org(dsn: str) -> None:
    from migrations.version import SHARED_MIGRATIONS

    conn = await asyncpg.connect(dsn)
    try:
        for name in SHARED_MIGRATIONS:
            await conn.execute((MIGRATIONS_DIR / name).read_text())
    finally:
        await conn.close()


async def _provision_schema(dsn: str, workspace: str) -> None:
    """只建 schema 与表（不建角色），供普通仓储测试。"""
    conn = await asyncpg.connect(dsn)
    try:
        for name in WORKSPACE_MIGRATIONS:
            if name == "0007_roles_and_grants.sql":
                continue
            sql = render_migration((MIGRATIONS_DIR / name).read_text(), workspace)
            if has_statements(sql):
                await conn.execute(sql)
    finally:
        await conn.close()


async def _drop_schema(dsn: str, workspace: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {ws_schema(workspace)} CASCADE")
    finally:
        await conn.close()


async def _drop_workspace_with_role(dsn: str, workspace: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        role = ws_role(workspace)
        await conn.execute(f"DROP SCHEMA IF EXISTS {ws_schema(workspace)} CASCADE")
        # 角色持有的授权须全部回收才能 DROP ROLE。库级那条来自 0007 给
        # APScheduler 的 CREATE——漏掉它，DROP ROLE 会报 DependentObjectsStillExist。
        db = await conn.fetchval("SELECT current_database()")
        await conn.execute(f"REVOKE ALL ON SCHEMA org FROM {role}")
        await conn.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA org FROM {role}")
        await conn.execute(f'REVOKE ALL ON DATABASE "{db}" FROM {role}')
        await conn.execute(f"DROP ROLE IF EXISTS {role}")
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def admin_dsn() -> str:
    return ADMIN_DSN


@pytest.fixture(scope="session")
def org_ready(admin_dsn: str) -> None:
    run(_ensure_org(admin_dsn))


@pytest.fixture()
def pg_schema(admin_dsn: str, org_ready: None):
    ws = f"t{secrets.token_hex(4)}"
    run(_provision_schema(admin_dsn, ws))
    yield TempWorkspace(workspace=ws, schema=ws_schema(ws), admin_dsn=admin_dsn)
    run(_drop_schema(admin_dsn, ws))


@pytest.fixture()
def pg_schema_pair(admin_dsn: str, org_ready: None):
    """两条无角色的临时业务线 schema，供调度隔离等双业务线测试（TC-012）。"""
    a, b = f"ta{secrets.token_hex(3)}", f"tb{secrets.token_hex(3)}"
    run(_provision_schema(admin_dsn, a))
    run(_provision_schema(admin_dsn, b))
    yield (
        TempWorkspace(a, ws_schema(a), admin_dsn),
        TempWorkspace(b, ws_schema(b), admin_dsn),
    )
    run(_drop_schema(admin_dsn, a))
    run(_drop_schema(admin_dsn, b))


@pytest.fixture()
def ws_pair(admin_dsn: str, org_ready: None):
    """两条带角色与完整授权的临时业务线，供 TC-001 / TC-003。"""
    a, b = f"ta{secrets.token_hex(3)}", f"tb{secrets.token_hex(3)}"

    async def _prov():
        conn = await asyncpg.connect(admin_dsn)
        try:
            for ws in (a, b):
                await apply_migrations(conn, ws, TEST_ROLE_PASSWORD)
        finally:
            await conn.close()

    run(_prov())
    yield (
        TempWorkspace(a, ws_schema(a), admin_dsn, role_dsn(admin_dsn, a, TEST_ROLE_PASSWORD)),
        TempWorkspace(b, ws_schema(b), admin_dsn, role_dsn(admin_dsn, b, TEST_ROLE_PASSWORD)),
    )
    run(_drop_workspace_with_role(admin_dsn, a))
    run(_drop_workspace_with_role(admin_dsn, b))


@pytest.fixture()
def space_context() -> SpaceContext:
    return SpaceContext(
        organization_id="org-test",
        workspace_id="alpha",
        bot_id="bot-test",
        chat_id="chat-test",
        topic_id="topic-test",
        actor_user_id="user-test",
        effective_config_version=1,
    )


class _FakeBase:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, method: str, *args, **kwargs) -> None:
        self.calls.append((method, args, kwargs))


class FakeDocumentPort(_FakeBase):
    async def list_meta(self, doc_ids: list[str]) -> list[dict[str, Any]]:
        self._record("list_meta", doc_ids)
        return [{"doc_id": d, "version": "v1"} for d in doc_ids]

    async def get_content(self, doc_id: str) -> str:
        self._record("get_content", doc_id)
        return ""

    async def update(self, doc_id: str, content: str) -> None:
        self._record("update", doc_id, content)


class FakeRequirementPort(_FakeBase):
    async def create_card(self, payload: dict[str, Any]) -> str:
        self._record("create_card", payload)
        return "req-fake-1"

    async def get(self, requirement_id: str) -> dict[str, Any]:
        self._record("get", requirement_id)
        return {"id": requirement_id}

    async def quota_watermark(self) -> dict[str, Any]:
        self._record("quota_watermark")
        return {"remaining": 1000}


class FakeTodoPort(_FakeBase):
    async def create(self, payload: dict[str, Any]) -> str:
        self._record("create", payload)
        return "todo-fake-1"

    async def get_batch(self, todo_ids: list[str]) -> list[dict[str, Any]]:
        self._record("get_batch", todo_ids)
        return [{"id": t, "status": "active"} for t in todo_ids]


class FakeCodeRepositoryPort(_FakeBase):
    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        self._record("search", query, limit)
        return []

    async def last_synced_at(self):
        self._record("last_synced_at")
        return None


@pytest.fixture()
def fake_ports() -> SimpleNamespace:
    return SimpleNamespace(
        document=FakeDocumentPort(),
        requirement=FakeRequirementPort(),
        todo=FakeTodoPort(),
        repo=FakeCodeRepositoryPort(),
    )
