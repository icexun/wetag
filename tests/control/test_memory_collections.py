"""T039：三层 collection 解析与记忆服务不可用时的显式降级。

covers: TC-069
"""
from __future__ import annotations

import pytest

from app.contracts.errors import MemoryUnavailable
from app.control.memory.collections import MemoryCollections
from app.control.memory.recall import DEGRADED_NOTE, recall
from app.platform.observability.alerting import AlertKind


class TestResolve:
    def test_resolve_three_layers(self):
        collections = MemoryCollections("alpha", biz_collection="wetag-alpha", org_collection="wetag-org")
        layers = collections.resolve()
        assert layers.biz_collection == "wetag-alpha"
        assert layers.org_collection == "wetag-org"
        # 群级 = 业务线 collection 内按 metadata chat_id 过滤
        assert layers.group_filter("c1") == {
            "workspace_id": "alpha",
            "chat_id": "c1",
            "layer": "group",
        }
        # 业务线层不加 chat_id 过滤（跨群召回，US8-3）
        assert layers.biz_filter() == {"workspace_id": "alpha", "layer": "biz"}

    def test_resolve_without_biz_collection_raises(self):
        collections = MemoryCollections("alpha", biz_collection="", org_collection="wetag-org")
        with pytest.raises(MemoryUnavailable):
            collections.resolve()


# ---- TC-069 记忆服务不可用时显式降级不中断任务 (US8-9) ----


class TestUnavailableDegradation:
    async def test_no_collection_config_degrades(self, alerts, memory_backend):
        """covers: TC-069 —— 无平台凭据（无 collection 名）：召回返回空 + degraded
        标记 + 降级告警，不抛错（任务整体不失败）。"""
        collections = MemoryCollections("alpha", biz_collection="")
        result = await recall(collections, memory_backend, "发布 窗口", chat_id="c1")

        assert result.degraded is True
        assert result.empty is True
        assert result.note == DEGRADED_NOTE  # 回答层明示「本次未使用历史记忆」
        degrade = [a for a in alerts if a.kind == AlertKind.DEGRADE]
        assert len(degrade) == 1 and degrade[0].workspace_id == "alpha"

    async def test_backend_failure_degrades(self, alerts, memory_backend):
        """covers: TC-069 —— 后端调用失败同样走显式降级而非任务失败。"""
        memory_backend.fail = True
        collections = MemoryCollections("alpha", biz_collection="wetag-alpha")
        result = await recall(collections, memory_backend, "发布 窗口", chat_id="c1")

        assert result.degraded is True
        assert result.note == DEGRADED_NOTE
        assert any(a.kind == AlertKind.DEGRADE for a in alerts)
