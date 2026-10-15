"""T040：三层召回与来源标注（记忆替身运行，真实 AgentRun 路径联调重跑）。

covers: TC-061, TC-062, TC-063, TC-070
"""
from __future__ import annotations

from app.control.memory.collections import MemoryCollections
from app.control.memory.recall import EMPTY_NOTE, layer_weight, recall


def _collections() -> MemoryCollections:
    return MemoryCollections("alpha", biz_collection="wetag-alpha", org_collection="wetag-org")


# ---- TC-061 三层记忆召回附带完整来源元数据 (US8-1) ----


async def test_recall_hits_carry_source_metadata(memory_backend):
    """covers: TC-061 —— 三层都有相关记忆时均可用于回答，每条标注层级、
    来源群、事项与时间；排序按 群 > 业务线 > 组织 加权。"""
    memory_backend.seed(
        "wetag-alpha",
        "发布 窗口 固定 周四",
        metadata={"workspace_id": "alpha", "chat_id": "c1", "topic_id": "t1", "layer": "group"},
    )
    memory_backend.seed(
        "wetag-alpha",
        "发布 窗口 需 提前 报备",
        metadata={"workspace_id": "alpha", "chat_id": "c2", "topic_id": "t9", "layer": "biz"},
    )
    memory_backend.seed(
        "wetag-org",
        "发布 窗口 遵守 公司 变更 规范",
        metadata={"layer": "org"},
    )

    result = await recall(_collections(), memory_backend, "发布 窗口", chat_id="c1")

    assert result.note is None
    by_layer = {h.layer: h for h in result.hits}
    assert set(by_layer) == {"group", "biz", "org"}
    group = by_layer["group"]
    assert group.source_chat_id == "c1" and group.source_topic_id == "t1"
    # 层级加权排序：群层排在最前
    assert result.hits[0].layer == "group"
    assert layer_weight("group") > layer_weight("biz") > layer_weight("org")


# ---- TC-062 记忆检索绝不跨业务线命中 (US8-2) ----


async def test_recall_never_cross_workspace(memory_backend):
    """covers: TC-062 —— 检索只发往本业务线 collection 与组织级 collection；
    后端即便混入其他业务线的命中也被防御性过滤。"""
    memory_backend.seed(
        "wetag-beta",
        "发布 窗口 beta 业务线 秘密",
        metadata={"workspace_id": "beta", "layer": "biz"},
    )
    # 异常情形：组织级 collection（无过滤）里混入了他线数据（防御性过滤兜底）
    memory_backend.seed(
        "wetag-org",
        "发布 窗口 混入 他线",
        metadata={"workspace_id": "beta", "layer": "biz"},
    )

    result = await recall(_collections(), memory_backend, "发布 窗口", chat_id="c1")

    # 检索范围只有本业务线 + 组织级，绝不触及 wetag-beta
    queried = {c["collection"] for c in memory_backend.search_calls}
    assert queried <= {"wetag-alpha", "wetag-org"}
    assert all(h.content != "发布 窗口 混入 他线" for h in result.hits)


# ---- TC-063 同业务线跨群决策可被召回并标明来源 (US8-3) ----


async def test_cross_group_hit_annotated(memory_backend):
    """covers: TC-063 —— 业务线层命中来自本业务线另一群的决策，
    结果标明来源群且 cross_group=True。"""
    memory_backend.seed(
        "wetag-alpha",
        "灰度 发布 先 小 流量",
        metadata={"workspace_id": "alpha", "chat_id": "c-other", "topic_id": "t7", "layer": "biz"},
    )

    result = await recall(_collections(), memory_backend, "灰度 发布", chat_id="c1")

    assert len(result.hits) == 1
    hit = result.hits[0]
    assert hit.layer == "biz"
    assert hit.cross_group is True
    assert hit.source_chat_id == "c-other"


# ---- TC-070 空召回结果在回答中明确说明 (US8-10) ----


async def test_empty_recall_has_explicit_note(memory_backend):
    """covers: TC-070 —— 服务可用但无匹配：结果为空且 note 明示
    「未找到相关历史记忆」，degraded 不为真（区别于服务不可用）。"""
    result = await recall(_collections(), memory_backend, "发布 窗口", chat_id="c1")

    assert result.degraded is False
    assert result.empty is True
    assert result.note == EMPTY_NOTE
