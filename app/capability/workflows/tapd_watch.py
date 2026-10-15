"""TAPD 需求同步与变更通知（``TapdSyncJob`` 的 sync_handler）。

每轮拉取进行中需求，``modified`` 有变化的逐条落台账；落库前读出上一版投影，
按字段（标题、状态、负责人、分类）比对，有差异就按订阅推送。

首跑（无基线）只建基线、不推送：冷启动全量拉取不是"变更"。
只有 ``modified`` 变、关注字段没变的编辑（如改描述）不推送，避免噪声。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from app.capability.integration.requirement.urls import story_url
from app.capability.workflows.change_notify import (
    Notifier,
    diff_requirement,
    render_requirement_change,
)
from app.contracts.job_result import JobResult
from app.contracts.ports import Requirement

if TYPE_CHECKING:
    from app.container import CapabilityContainer
    from app.runtime.jobs.base import JobContext

logger = logging.getLogger(__name__)

SEEN_KEY = "tapd_sync:seen"  # {"seen": {tapd_id: modified_iso}}：变更比对基线


def make_tapd_sync_handler(
    container: "CapabilityContainer",
) -> "Callable[[JobContext, Any], Awaitable[JobResult]]":
    """构造 ``TapdSyncJob`` 的 sync_handler。容器在装配期绑定，闭包持有。"""

    async def sync(ctx: "JobContext", port: Any) -> JobResult:
        reqs = await port.list_active(container.workspace)
        repo = container.repository

        baseline: dict[str, str] = {}
        if ctx.state is not None:
            baseline = dict(((await ctx.state.get(SEEN_KEY)) or {}).get("seen", {}))
        first_run = not baseline

        changed = [r for r in reqs if baseline.get(r.tapd_id) != _modified(r)]
        previous = await _previous(repo, [r.tapd_id for r in changed])
        notifier = Notifier(repo)

        synced = 0
        envelopes = []
        for req in changed:
            if repo is not None and await _upsert(repo, req):
                synced += 1
            prev = previous.get(req.tapd_id)
            if first_run or prev is None:
                continue
            diffs = diff_requirement(prev, req)
            if diffs:
                url = story_url(container.workspace.tapd_project_id, req.tapd_id)
                text = render_requirement_change(req, diffs, url)
                envelopes += await notifier.envelopes("requirement", req.tapd_id, text)

        if ctx.state is not None:
            await ctx.state.set(SEEN_KEY, {"seen": {r.tapd_id: _modified(r) for r in reqs}})

        metrics: dict[str, Any] = {
            "active": len(reqs),
            "changed": len(changed),
            "synced": synced,
            "first_run": first_run,
            "notified": len(envelopes),
        }
        if not getattr(port, "last_list_complete", True):
            metrics["list_truncated"] = True
        logger.info("tapd_sync 同步完成 %s", metrics)
        return JobResult(envelopes=envelopes, metrics=metrics)

    return sync


def _modified(req: Requirement) -> str:
    """变更指纹。TAPD 的 modified 精度到秒，够用。"""
    return req.updated_at.isoformat() if req.updated_at else ""


async def _previous(repo: Any, tapd_ids: list[str]) -> dict[str, Any]:
    if repo is None or not tapd_ids:
        return {}
    try:
        return await repo.requirements_by_tapd_ids(tapd_ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning("需求上一版读取失败，本轮不推送变更：%r", exc)
        return {}


async def _upsert(repo: Any, req: Requirement) -> bool:
    try:
        async with repo.db.tx() as conn:
            await repo.upsert_requirement(
                conn,
                tapd_id=req.tapd_id,
                title=req.title,
                status=req.status,
                status_category=req.status_category,
                category=req.category,
                owners=req.owners,
                code_hints=req.code_hints,
                doc_id=str(req.doc_ref.document_id) if req.doc_ref else None,
                raw=dict(req.raw),
                tapd_updated_at=req.updated_at,
            )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("需求台账同步失败 tapd=%s：%r", req.tapd_id, exc)
        return False


__all__ = ["SEEN_KEY", "make_tapd_sync_handler"]
