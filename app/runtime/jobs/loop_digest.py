"""周一：Loop 周期归因与提案卡片（Agentic 骨架，业务逻辑在 Capability）。"""
from __future__ import annotations

from app.runtime.jobs.base import CapabilityJob


class LoopDigestJob(CapabilityJob):
    name = "loop_digest"
    timeout_s = 900.0
    default_cron = "0 9 * * mon"  # 每周一 09:00（UTC）
