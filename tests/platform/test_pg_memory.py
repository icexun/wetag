"""PgMemory（PG 持久化工作记忆）行为测试：语义对齐 InMemoryMemory。

真库测试：依赖 conftest 的 pg_db 夹具（一次性 schema），未配 PG_DSN 自动跳过。
"""

import pytest
from agentscope.message import Msg

from app.platform.memory.working import PgMemory

pytestmark = pytest.mark.asyncio


def _msg(name: str, text: str, role: str = "user") -> Msg:
    return Msg(name, text, role)


async def test_add_and_get_roundtrip(pg_db):
    mem = PgMemory(pg_db, "t1")
    m1, m2 = _msg("u", "第一条"), _msg("a", "第二条", "assistant")
    await mem.add([m1, m2])

    got = await mem.get_memory(prepend_summary=False)
    assert [m.id for m in got] == [m1.id, m2.id]
    assert got[0].content == "第一条"
    assert await mem.size() == 2


async def test_add_dedup_by_msg_id(pg_db):
    mem = PgMemory(pg_db, "t1")
    m = _msg("u", "重复写入")
    await mem.add(m)
    await mem.add(m)
    assert await mem.size() == 1


async def test_thread_isolation(pg_db):
    a, b = PgMemory(pg_db, "t1"), PgMemory(pg_db, "t2")
    await a.add(_msg("u", "线程一"))
    assert await b.size() == 0
    assert await a.size() == 1


async def test_marks_filter(pg_db):
    mem = PgMemory(pg_db, "t1")
    m1, m2 = _msg("u", "带标记"), _msg("u", "无标记")
    await mem.add(m1, marks="hint")
    await mem.add(m2)

    by_mark = await mem.get_memory(mark="hint", prepend_summary=False)
    assert [m.id for m in by_mark] == [m1.id]
    excluded = await mem.get_memory(exclude_mark="hint", prepend_summary=False)
    assert [m.id for m in excluded] == [m2.id]


async def test_update_messages_mark(pg_db):
    mem = PgMemory(pg_db, "t1")
    m = _msg("u", "改标记")
    await mem.add(m, marks="old")
    n = await mem.update_messages_mark("new", old_mark="old")
    assert n == 1
    assert [x.id for x in await mem.get_memory(mark="new", prepend_summary=False)] == [m.id]
    assert await mem.get_memory(mark="old", prepend_summary=False) == []


async def test_delete_and_delete_by_mark(pg_db):
    mem = PgMemory(pg_db, "t1")
    m1, m2 = _msg("u", "按 id 删"), _msg("u", "按标记删")
    await mem.add(m1)
    await mem.add(m2, marks="tmp")
    assert await mem.delete([m1.id]) == 1
    assert await mem.delete_by_mark("tmp") == 1
    assert await mem.size() == 0


async def test_compressed_summary_persisted(pg_db):
    mem = PgMemory(pg_db, "t1")
    await mem.add(_msg("u", "正文"))
    await mem.update_compressed_summary("前文摘要")

    # 新实例（模拟进程重启）能读回摘要，且 get_memory 前置
    fresh = PgMemory(pg_db, "t1")
    got = await fresh.get_memory()
    assert got[0].content == "前文摘要"
    assert len(got) == 2


async def test_clear_removes_messages_and_summary(pg_db):
    mem = PgMemory(pg_db, "t1")
    await mem.add(_msg("u", "待清"))
    await mem.update_compressed_summary("摘要")
    await mem.clear()
    assert await mem.size() == 0
    assert await PgMemory(pg_db, "t1").get_memory() == []
