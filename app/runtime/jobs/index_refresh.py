"""Agentic：派生自 doc_poll，业务逻辑在 Capability。

骨架：只做派发与留痕；capability 处理器未注入时抛 PortUnavailable（TC-089）。
"""
from __future__ import annotations

from app.runtime.jobs.base import CapabilityJob


class IndexRefreshJob(CapabilityJob):
    name = "index_refresh"
    timeout_s = 600.0
