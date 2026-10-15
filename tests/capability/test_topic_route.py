"""L3 语义判定：指代消息归入既有事项；无把握/幻觉/调用失败一律 None（落 L4 新建）。"""
from __future__ import annotations

from types import SimpleNamespace

from app.capability.intelligence.topic_route import LlmTopicClassifier, TopicVerdict


def _candidates():
    return [
        SimpleNamespace(topic_id="t_aaa", title="wetag使用场景验证", ref_keys=["tapd:story:1001"]),
        SimpleNamespace(topic_id="t_bbb", title="提现需求冲突", ref_keys=[]),
    ]


class _FakeAgent:
    def __init__(self, result):
        self.result = result

    async def complete(self, schema, *, system, user):
        return self.result


def _ok(topic_id):
    return SimpleNamespace(ok=True, data=TopicVerdict(topic_id=topic_id), reason="")


async def test_anaphoric_message_routes_to_existing_topic():
    clf = LlmTopicClassifier(_FakeAgent(_ok("t_aaa")))
    hit = await clf.classify("更新这条需求，把链接替换为刚创建的飞书文档", _candidates())
    assert hit == "t_aaa"


async def test_no_match_returns_none():
    clf = LlmTopicClassifier(_FakeAgent(_ok(None)))
    assert await clf.classify("一个全新的问题", _candidates()) is None


async def test_hallucinated_topic_id_rejected():
    """模型返回候选集合外的 topic_id 按未命中处理，不错误合并。"""
    clf = LlmTopicClassifier(_FakeAgent(_ok("t_not_in_candidates")))
    assert await clf.classify("随便一句", _candidates()) is None


async def test_structured_failure_returns_none():
    clf = LlmTopicClassifier(_FakeAgent(SimpleNamespace(ok=False, data=None, reason="超时")))
    assert await clf.classify("这条需求怎么样了", _candidates()) is None


async def test_empty_candidates_short_circuit():
    clf = LlmTopicClassifier(_FakeAgent(_ok("t_aaa")))
    assert await clf.classify("任何消息", []) is None
