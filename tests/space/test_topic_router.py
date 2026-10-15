"""会话事项路由 L1/L2/L3/L4。"""

from __future__ import annotations

from app.space.topic.router import (
    RouteLevel,
    TopicRouter,
    derive_title,
    extract_ref_keys,
)
from tests.conftest import inbound_ref

# ===== 外部标识提取 =====


def test_extract_feishu_and_tapd_links():
    text = (
        "参考 https://example.feishu.cn/wiki/WikiDemoToken0003 "
        "和 https://www.tapd.cn/12345/prong/stories/view/1001234567890123456"
    )

    keys = extract_ref_keys(text)

    assert "feishu:wiki:WikiDemoToken0003" in keys
    assert "tapd:storie:1001234567890123456" in keys


def test_extract_plain_tapd_number():
    assert "tapd:story:10012" in extract_ref_keys("TAPD-10012 这个需求")
    assert "tapd:story:10012" in extract_ref_keys("需求 10012 改了")


def test_extract_dedups_preserving_order():
    text = "TAPD-101 又提了 TAPD-102，还是 TAPD-101"

    assert extract_ref_keys(text) == ["tapd:story:101", "tapd:story:102"]


def test_derive_title_strips_mention_and_truncates():
    assert derive_title("@WeTag  建个需求卡", 40) == "建个需求卡"
    assert derive_title("@WeTag " + "长" * 50, 10).endswith("…")
    assert derive_title("", 40) == "未命名事项"


# ===== 路由 =====


async def test_l4_creates_new_topic(space_repo):
    router = TopicRouter(space_repo)

    route = await router.resolve(inbound_ref(text="看下登录改造", event_id="msg_1"))

    assert route.level is RouteLevel.L4_NEW
    assert route.is_new
    assert route.topic.title == "看下登录改造"
    assert route.topic.known_msg_ids == ["msg_1"]


async def test_l1_quote_wins_over_everything(space_repo):
    router = TopicRouter(space_repo)
    first = await router.resolve(inbound_ref(text="需求 A", event_id="msg_1"))

    # 引用了首条消息，即使正文提到了另一个 TAPD 编号也应归到被引用的事项。
    route = await router.resolve(
        inbound_ref(
            text="顺带看下 TAPD-999", event_id="msg_2", quote_message_id="msg_1"
        )
    )

    assert route.level is RouteLevel.L1_QUOTE
    assert route.topic.topic_id == first.topic.topic_id


async def test_l1_quote_content_fallback_without_msgid(space_repo):
    """引用帧不带 msgid 时，用引用内容匹配 bot 出站消息定位事项。"""
    router = TopicRouter(space_repo)
    first = await router.resolve(inbound_ref(text="需求 A", event_id="msg_1"))
    space_repo.outbound_texts.append(
        ("chat_001", first.topic.topic_id, "情况汇总如下：\n已完成……")
    )

    route = await router.resolve(
        inbound_ref(text="@WeTag 1", event_id="msg_2", quote_content="情况汇总如下：\n已完成……")
    )

    assert route.level is RouteLevel.L1_QUOTE
    assert route.topic.topic_id == first.topic.topic_id


async def test_l2_matches_by_external_reference(space_repo):
    router = TopicRouter(space_repo)
    first = await router.resolve(inbound_ref(text="TAPD-1001 要改", event_id="msg_1"))

    route = await router.resolve(
        inbound_ref(text="TAPD-1001 进度如何", event_id="msg_2")
    )

    assert route.level is RouteLevel.L2_REFERENCE
    assert route.topic.topic_id == first.topic.topic_id


async def test_parallel_topics_in_one_chat_do_not_cross(space_repo):
    """同群 10 秒内讨论两个需求，必须分成两个事项（数字走查 #4）。"""
    router = TopicRouter(space_repo)

    a = await router.resolve(inbound_ref(text="TAPD-1001 改登录", event_id="msg_1"))
    b = await router.resolve(inbound_ref(text="TAPD-1002 改支付", event_id="msg_2"))

    assert a.topic.topic_id != b.topic.topic_id
    again = await router.resolve(inbound_ref(text="TAPD-1002 呢", event_id="msg_3"))
    assert again.topic.topic_id == b.topic.topic_id


async def test_l2_reactivates_archived_topic(space_repo):
    router = TopicRouter(space_repo)
    first = await router.resolve(inbound_ref(text="TAPD-1001 改登录", event_id="msg_1"))
    space_repo.topics[first.topic.topic_id] = type(first.topic)(
        **{**first.topic.__dict__, "status": "ARCHIVED"}
    )

    route = await router.resolve(
        inbound_ref(text="TAPD-1001 又改了", event_id="msg_2")
    )

    assert route.topic.topic_id == first.topic.topic_id
    assert space_repo.topics[first.topic.topic_id].status == "ACTIVE"


async def test_l3_classifier_hit(space_repo):
    existing = await TopicRouter(space_repo).resolve(
        inbound_ref(text="登录改造", event_id="msg_1")
    )

    class Classifier:
        async def classify(self, text, candidates, context):
            return existing.topic.topic_id

    route = await TopicRouter(space_repo, Classifier()).resolve(
        inbound_ref(text="这块的手机号校验呢", event_id="msg_2")
    )

    assert route.level is RouteLevel.L3_SEMANTIC
    assert route.topic.topic_id == existing.topic.topic_id


async def test_l3_abstains_falls_back_to_new_topic(space_repo):
    """宁可新建，不错误合并：分类器没把握时必须新建。"""
    await TopicRouter(space_repo).resolve(inbound_ref(text="登录改造", event_id="msg_1"))

    class Abstaining:
        async def classify(self, text, candidates, context):
            return None

    route = await TopicRouter(space_repo, Abstaining()).resolve(
        inbound_ref(text="完全不相干的事", event_id="msg_2")
    )

    assert route.level is RouteLevel.L4_NEW


# ===== L2.5 超短应答 =====


async def test_short_reply_joins_most_recent_topic(space_repo):
    """"1" / "好的" 这类应答没有独立语义，归入最近活跃事项而非新建孤儿 thread。"""
    router = TopicRouter(space_repo)
    existing = await router.resolve(inbound_ref(text="登录改造", event_id="msg_1"))

    route = await router.resolve(inbound_ref(text="@WeTag 1", event_id="msg_2"))

    assert route.level is RouteLevel.L2_RECENT
    assert route.topic.topic_id == existing.topic.topic_id


async def test_short_reply_without_active_topic_creates_new(space_repo):
    """群里没有任何活跃事项时，超短应答也只能新建。"""
    route = await TopicRouter(space_repo).resolve(inbound_ref(text="1", event_id="msg_1"))

    assert route.level is RouteLevel.L4_NEW


async def test_short_request_is_not_treated_as_reply(space_repo):
    """"查一下冲突" 是完整请求不是应答，不受 L2.5 影响。"""
    router = TopicRouter(space_repo)
    await router.resolve(inbound_ref(text="登录改造", event_id="msg_1"))

    route = await router.resolve(inbound_ref(text="查一下冲突", event_id="msg_2"))

    assert route.level is RouteLevel.L4_NEW
