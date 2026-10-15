"""确定性：飞书元信息批量比对，默认 3 分钟（裁决 #11）。

零模型开销（US2-3）：只调 DocumentPort.list_meta 做版本比对；
有变化时派生 index_refresh（Agentic）任务，可追溯 parent_id 与幂等键（US2-2 / US3-2）。
"""
from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.contracts.job_result import JobResult
from app.contracts.task import TaskKind, TaskSpec, TriggerSource
from app.runtime.jobs.base import Job, JobContext, require_port

if TYPE_CHECKING:
    from app.platform.degradation import FreshnessTracker

logger = logging.getLogger(__name__)

WATCH_KEY = "doc_poll:watch"  # {"doc_ids": [...]}：补充清单（索引内文档自动并入）
VERSIONS_KEY = "doc_poll:versions"  # {"versions": {doc_id: version}}：上轮比对基线


def build_idem_key(workspace_id: str, doc_id: str, version: Any) -> str:
    """同文档同版本只派生一次下游任务（US3-2）。"""
    raw = f"doc_poll:{workspace_id}:{doc_id}:{version}"
    return "docpoll:" + hashlib.sha256(raw.encode()).hexdigest()[:40]


class DocPollJob(Job):
    """文档轮询：无变化零产出，有变化只派生 ``index_refresh``（不直接发消息、不调模型）。

    监听清单取并集：

    * ``doc_poll:watch``（runtime_state）——人工登记的补充清单；
    * 索引内全部未归档文档；
    * 非终态需求关联的文档、群里具体订阅的文档——首次进入清单即被索引建基线。
    """

    name = "doc_poll"
    kind = TaskKind.DETERMINISTIC
    timeout_s = 60.0
    default_cron = "*/3 * * * *"

    def __init__(
        self,
        document_port: Any = None,
        freshness: FreshnessTracker | None = None,
        index_repository: Any = None,
    ) -> None:
        self._document = document_port
        self._freshness = freshness
        self._index_repo = index_repository

    async def run(self, ctx: JobContext) -> JobResult:
        port = self._document or require_port(ctx, "document")
        doc_ids, stale_ids = await self._watch_list(ctx)
        if not doc_ids:
            return JobResult(metrics={"watched": 0, "changed": 0})

        metas = await port.list_meta(doc_ids)
        known = ((await ctx.state.get(VERSIONS_KEY)) or {}).get("versions", {}) if ctx.state else {}
        changed = [m for m in metas if known.get(m["doc_id"]) != str(m.get("version"))]

        if ctx.state is not None:
            await ctx.state.set(
                VERSIONS_KEY,
                {"versions": {m["doc_id"]: str(m.get("version")) for m in metas}},
            )
        if self._freshness is not None:
            await self._freshness.record_success(self.name)

        derived = [
            TaskSpec(
                job_name="index_refresh",
                kind=TaskKind.AGENTIC,
                trigger_source=TriggerSource.DERIVED,
                idem_key=build_idem_key(ctx.workspace_id, m["doc_id"], m.get("version")),
                payload={"document_ids": [m["doc_id"]], "version": m.get("version")},
            )
            for m in changed
        ]
        derived += self._stale_recovery(ctx, metas, stale_ids, {m["doc_id"] for m in changed})
        return JobResult(
            derived_tasks=derived,
            metrics={"watched": len(doc_ids), "changed": len(changed)},
        )

    def _stale_recovery(
        self, ctx: JobContext, metas: list, stale_ids: set[str], changed_ids: set[str]
    ) -> list[TaskSpec]:
        """stale 索引行的自愈重刷。

        stale 行版本不变时永远进不了 changed，而首次标脏已消费掉
        ``docpoll:{doc}:{version}`` 幂等键（唯一索引对失败任务同样生效），
        不自愈就是一行脏快照永远不刷新。
        幂等键带小时级 epoch：瞬时故障下一轮即恢复；永久性失败（如文档已删）
        每文档每小时最多重试一次，不会刷爆队列。
        """
        epoch = datetime.now(UTC).strftime("%Y-%m-%dT%H")
        out = []
        for m in metas:
            doc_id = m["doc_id"]
            if doc_id not in stale_ids or doc_id in changed_ids:
                continue
            out.append(
                TaskSpec(
                    job_name="index_refresh",
                    kind=TaskKind.AGENTIC,
                    trigger_source=TriggerSource.DERIVED,
                    idem_key=build_idem_key(
                        ctx.workspace_id, doc_id, f"{m.get('version')}:recover:{epoch}"
                    ),
                    payload={"document_ids": [doc_id], "version": m.get("version")},
                )
            )
        if out:
            logger.info("stale 索引自愈重刷 %d 篇", len(out))
        return out

    async def _watch_list(self, ctx: JobContext) -> tuple[list[str], set[str]]:
        """全部来源的并集，附带索引侧的 stale 集合（供自愈重刷）。

        各来源都允许缺失，并集为空则本轮空跑。
        """
        watch = (await ctx.state.get(WATCH_KEY) or {}) if ctx.state else {}
        doc_ids = {str(d) for d in watch.get("doc_ids", [])}
        stale_ids: set[str] = set()
        repo = self._index_repo or (ctx.extras or {}).get("capability_repository")
        if repo is not None:
            try:
                rows = await repo.list_indexed_docs()
                doc_ids.update(str(r["document_id"]) for r in rows)
                stale_ids.update(str(r["document_id"]) for r in rows if r["stale"])
                for extra in ("requirement_doc_ids", "subscribed_doc_ids"):
                    if hasattr(repo, extra):
                        doc_ids.update(await getattr(repo, extra)())
            except Exception as exc:  # noqa: BLE001
                # 库侧清单拿不到不该让 state 清单也听不成——降级并留痕。
                logger.warning("索引监听清单获取失败，本轮仅听部分来源：%r", exc)
        return sorted(doc_ids), stale_ids
