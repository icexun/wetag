"""事项划分纠正样本采集。"""

from __future__ import annotations

from app.space.topic.correction import CorrectionCollector
from tests.conftest import inbound_ref


async def test_correction_is_recorded_but_not_applied(space_repo):
    """今日降级：只留样本，不即时迁移（§10.3）。"""
    collector = CorrectionCollector(space_repo)

    recorded = await collector.maybe_record(
        inbound_ref(text="不是这个事，说的是另一个需求"), current_topic_id="t_1"
    )

    assert recorded
    assert space_repo.corrections[0]["from_topic_id"] == "t_1"
    assert space_repo.corrections[0]["to_topic_id"] is None


async def test_plain_message_is_not_treated_as_correction(space_repo):
    collector = CorrectionCollector(space_repo)

    assert not await collector.maybe_record(
        inbound_ref(text="帮我建个需求卡"), current_topic_id="t_1"
    )
