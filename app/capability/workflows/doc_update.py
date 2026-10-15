"""文档更新与索引刷新。

两件事放在一个文件里，因为它们共用同一个"读取即持久化四元组"的纪律：每次读文档都
把 ``document_id / revision_id / modified_time / content_hash`` 落下来，写文档时拿
``revision_id`` 当乐观锁。

## 乐观锁冲突只重试一次

``WriteResult.conflict`` 表示别人在我们读取之后改过这篇文档。处置是**重算 diff
再试一次**，失败即停，不进重试循环。因为版本冲突意味着有人正在编辑：第二次冲突说明
他还在编，第三次第四次只会把我们的改动插进他正在写的段落中间。停下来让人来判断比
"最终一定写进去"更可取——这里追求的不是送达率。

## 索引刷新的"零写入"不是失败

内容没变时 ``refresh_index`` 返回 ``UNCHANGED``，此时必须调 ``touch_index``
只更新 ``refreshed_at``。漏掉这一步，一篇三个月没人改的文档会被当成"三个月没刷新"，
稳定被当成了故障。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from app.capability.repository import IndexRefresh, as_list
from app.capability.workflows.base import Step, StepOutcome, Workflow
from app.capability.workflows.change_notify import (
    Notifier,
    diff_sections,
    render_doc_change,
    section_digest,
)
from app.contracts.errors import PortUnavailable
from app.contracts.ids import DocumentId, IdemKey
from app.contracts.job_result import JobResult
from app.contracts.ports import (
    DocumentRef,
    DocumentSnapshot,
    DocumentUpdate,
    FetchScope,
    UpdateCommand,
    WorkspaceConfig,
)
from app.contracts.space_context import SpaceContext
from app.platform.cli.result import CliErrorCode

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DocUpdateContext:
    payload: Mapping[str, Any]
    workspace: WorkspaceConfig
    context: SpaceContext | None = None
    idem_key: str | None = None
    trace_id: str | None = None

    snapshot: DocumentSnapshot | None = None
    written_revision: int | None = None
    retried: bool = False


class DocUpdateWorkflow(Workflow[DocUpdateContext]):
    """按块更新文档（**不整篇覆盖**）。

    整篇覆盖会把别人在同一篇文档里的编辑一起抹掉，而抹掉的内容不在我们的 diff 里，
    所以连"改了什么"都无法回溯。所以 ``UpdateCommand`` 只有 append / str_replace /
    block_* 四种。
    """

    name = "doc_update"

    def __init__(self, documents: Any) -> None:
        self._docs = documents

    def steps(self, ctx: DocUpdateContext) -> Sequence[Step[DocUpdateContext]]:
        return (
            Step("read_base", self._read_base, calls_external=True),
            Step("write", self._write, calls_external=True),
        )

    async def _read_base(self, ctx: DocUpdateContext) -> StepOutcome:
        """读基线版本，拿 ``revision_id`` 当乐观锁。

        用 ``OUTLINE`` 而不是 ``WHOLE``：这一步只需要 revision 与章节锚点，正文
        全文对写入没有用，而 ``WHOLE`` 在长文档上既慢又可能超 ``max_output_bytes``。
        """
        doc_id = str(ctx.payload.get("document_id") or "")
        if not doc_id:
            return StepOutcome.fail("payload 缺少 document_id")
        ctx.snapshot = await self._docs.fetch(
            DocumentRef(document_id=DocumentId(doc_id)), FetchScope.OUTLINE
        )
        return StepOutcome(metrics={"base_revision": ctx.snapshot.meta.revision_id})

    async def _write(self, ctx: DocUpdateContext) -> StepOutcome:
        assert ctx.snapshot is not None
        cmd_name = str(ctx.payload.get("command") or UpdateCommand.APPEND.value)
        try:
            command = UpdateCommand(cmd_name)
        except ValueError:
            # 不认识的命令一律拒绝，**不回退到 append**。回退会让一次"替换某段"的
            # 意图变成在文末追加一段，文档里于是同时存在新旧两份说法。
            return StepOutcome.fail(f"未知的更新命令：{cmd_name}")

        content = str(ctx.payload.get("content") or "")
        if not content:
            return StepOutcome.fail("payload 无写入内容")

        update = DocumentUpdate(
            ref=DocumentRef(document_id=ctx.snapshot.meta.document_id),
            command=command,
            content=content,
            base_revision_id=ctx.snapshot.meta.revision_id,
            anchor=ctx.payload.get("anchor"),
            old_text=ctx.payload.get("old_text"),
        )
        key = IdemKey(f"{ctx.idem_key or 'doc'}:write")
        result = await self._docs.update(update, key)

        if result.conflict:
            # 重读一次拿到新 revision，再试一次。仅一次——见模块 docstring。
            ctx.retried = True
            ctx.snapshot = await self._docs.fetch(update.ref, FetchScope.OUTLINE)
            result = await self._docs.update(
                DocumentUpdate(
                    ref=update.ref,
                    command=update.command,
                    content=update.content,
                    base_revision_id=ctx.snapshot.meta.revision_id,
                    anchor=update.anchor,
                    old_text=update.old_text,
                ),
                key,
            )
            if result.conflict:
                return StepOutcome.fail(
                    "文档连续两次乐观锁冲突：有人正在编辑，本次不再重试，"
                    "改动未写入（需人工确认）"
                )

        if not result.ok:
            return StepOutcome.fail(f"文档写入失败：{result.error_code or '未知'}")

        ctx.written_revision = result.revision_id
        return StepOutcome(
            state_patch={f"doc_revision:{update.ref.document_id}": result.revision_id},
            metrics={"revision": result.revision_id, "retried_on_conflict": ctx.retried},
        )


# ---------------------------------------------------------------------------
# 索引刷新（零 token 的确定性作业）
# ---------------------------------------------------------------------------


async def refresh_index(
    *,
    repository: Any,
    documents: Any,
    document_ids: Sequence[str] = (),
    embedder: Any = None,
    notifier: Notifier | None = None,
) -> JobResult:
    """刷新文档快照（``JobKind.INDEX_REFRESH``），内容变化时按订阅推送变更。

    **每篇文档一个事务**：一轮可能刷几十篇，每篇都要 await 一次飞书读取，
    一个大事务会把连接占住整轮。代价是部分成功，可接受——快照可重复刷新。

    单篇失败**不中断整轮**，但只要有失败，整轮 ``completed`` 就是 False。
    """
    if not document_ids:
        return JobResult(metrics={"refreshed": 0, "note": "无待刷新文档"})

    refreshed = written = touched = failed = 0
    backfilled = rejected = stale_marked = archived_failed = 0
    failures: list[str] = []
    envelopes = []

    for doc_id in document_ids:
        ref = DocumentRef(document_id=DocumentId(str(doc_id)))
        try:
            snapshot = await documents.fetch(ref, FetchScope.WHOLE)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            failures.append(f"{doc_id}: {type(exc).__name__}")
            logger.warning("索引刷新读取失败 doc=%s：%r", doc_id, exc)
            # 拿不到最新内容就必须标脏，否则旧快照会被当成最新。
            # NOT_FOUND（文档已删）是永久性失败：直接归档，不走「连续 3 次」慢路径。
            permanent = isinstance(exc, PortUnavailable) and (
                exc.error_code == CliErrorCode.NOT_FOUND
            )
            try:
                async with repository.db.tx() as conn:
                    outcome = await repository.mark_stale_or_archive(
                        conn,
                        document_id=ref.document_id,
                        reason=f"fetch_failed:{type(exc).__name__}"
                        + (":NOT_FOUND" if permanent else ""),
                        max_failures=1 if permanent else 3,
                    )
                if outcome == "archived":
                    archived_failed += 1
                elif outcome == "stale":
                    stale_marked += 1
            except Exception as mark_exc:  # noqa: BLE001
                failures.append(f"{doc_id}: 标脏也失败 {type(mark_exc).__name__}")
                logger.error("索引标脏失败 doc=%s：%r", doc_id, mark_exc)
            continue

        snapshot = await _fill_meta_title(documents, snapshot)
        sections = section_digest(snapshot)
        content_hash = snapshot.meta.content_hash or _hash(snapshot)
        # 向量在开事务之前算完：远程模型调用不能占着事务等网络。
        embedding = await _embed(embedder, snapshot)
        previous = await repository.get_index(snapshot.meta.document_id)
        try:
            async with repository.db.tx() as conn:
                outcome = await repository.refresh_index(
                    conn,
                    document_id=snapshot.meta.document_id,
                    title=snapshot.meta.title,
                    sections=sections,
                    embedding=embedding,
                    doc_revision=int(snapshot.meta.revision_id or 0),
                    content_hash=content_hash,
                    doc_modified_at=snapshot.meta.modified_time,
                )
                if outcome is IndexRefresh.REJECTED_STALE:
                    # 没核对过内容，绝不能 touch（会给未核对的快照盖章），反过来标脏。
                    await repository.mark_stale(
                        conn, document_id=snapshot.meta.document_id, reason="stale_revision"
                    )
                    rejected += 1
                elif outcome is IndexRefresh.UNCHANGED:
                    await repository.touch_index(conn, document_id=snapshot.meta.document_id)
                    touched += 1
                    if embedding and await repository.backfill_embedding(
                        conn, document_id=snapshot.meta.document_id, embedding=embedding
                    ):
                        backfilled += 1
                else:
                    written += 1
            refreshed += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            failures.append(f"{doc_id}: {type(exc).__name__}")
            logger.warning("索引刷新落库失败 doc=%s：%r", doc_id, exc)
            continue

        if outcome is IndexRefresh.UPDATED and previous is not None and notifier is not None:
            diff = diff_sections(as_list(previous["sections"]), sections)
            text = render_doc_change(
                title=snapshot.meta.title,
                document_id=str(snapshot.meta.document_id),
                diff=diff,
                revision=snapshot.meta.revision_id,
            )
            envelopes += await notifier.envelopes(
                "document", str(snapshot.meta.document_id), text
            )

    metrics = {
        "refreshed": refreshed,
        "written": written,
        "touched": touched,
        "backfilled": backfilled,
        "rejected": rejected,
        "stale_marked": stale_marked,
        "archived_failed": archived_failed,
        "notified": len(envelopes),
        "failed": failed,
        "failures": failures[:20],
    }
    if failed:
        return JobResult.incomplete(
            f"{failed}/{len(document_ids)} 篇索引刷新失败，索引不完整",
            metrics=metrics,
            envelopes=envelopes,
        )
    return JobResult(envelopes=envelopes, metrics=metrics)


async def _fill_meta_title(documents: Any, snapshot: DocumentSnapshot) -> DocumentSnapshot:
    """fetch 拿不到 title（lark-cli fetch 响应无此字段），用 batch_meta 补。

    补不到（接口失败 / 无权限 / metas 也空）时原样返回——标题是文案增强，
    不阻断刷新；下游仍有 ``or str(document_id)`` 兜底，只是文案降级，
    不是结论降级。
    """
    if snapshot.meta.title:
        return snapshot
    try:
        metas = await documents.batch_meta(
            [DocumentRef(document_id=snapshot.meta.document_id)]
        )
        title = str(metas[0].title) if metas else ""
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "标题补齐失败 doc=%s：%r", snapshot.meta.document_id, exc
        )
        return snapshot
    if not title:
        return snapshot
    return replace(snapshot, meta=replace(snapshot.meta, title=title))


async def _embed(embedder: Any, snapshot: DocumentSnapshot) -> list[float] | None:
    if embedder is None:
        return None
    try:
        return list(await embedder.embed(snapshot.meta.title))
    except Exception as exc:  # noqa: BLE001
        logger.warning("向量生成失败，本篇无向量兜底路：%r", exc)
        return None


def _hash(snapshot: DocumentSnapshot) -> str:
    """端口没给 ``content_hash`` 时自己算一个。

    必须有值：``refresh_index`` 的 ``WHERE`` 子句靠它判断"内容是否变化"，
    留 None 会让每一轮轮询都判定为变化并整条重写，3 分钟一次。
    """
    h = hashlib.sha256()
    for s in snapshot.sections:
        h.update(s.anchor.encode())
        h.update(s.text.encode())
    if not snapshot.sections and snapshot.raw_text:
        h.update(snapshot.raw_text.encode())
    return h.hexdigest()[:32]


__all__ = ["DocUpdateContext", "DocUpdateWorkflow", "refresh_index"]
