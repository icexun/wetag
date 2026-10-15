"""covers: TC-009, TC-010

TC-009 有变化的确定性任务只声明后续工作与消息（US2-2）
TC-010 无变化的确定性任务不产生模型工作（US2-3）
"""
from __future__ import annotations

from app.contracts.job_result import JobResult
from app.contracts.task import TaskKind, TaskSpec, TaskStatus, TriggerSource
from app.runtime.jobs.doc_poll import VERSIONS_KEY, WATCH_KEY, DocPollJob, build_idem_key
from app.runtime.worker.pool import WorkerPool


class _DocPort:
    """DocumentPort 替身：版本表可变，记录调用次数。"""

    def __init__(self, versions: dict[str, str]) -> None:
        self.versions = versions
        self.calls = 0

    async def list_meta(self, doc_ids: list[str]) -> list[dict]:
        self.calls += 1
        return [{"doc_id": d, "version": self.versions[d]} for d in doc_ids]


def _spec() -> TaskSpec:
    return TaskSpec(
        job_name="doc_poll",
        kind=TaskKind.DETERMINISTIC,
        trigger_source=TriggerSource.SCHEDULE,
    )


def _worker(task_repo, state_store, job, outbox: list | None = None) -> WorkerPool:
    if outbox is not None:
        async def submit(env):  # noqa: A001
            outbox.append(env)
    else:
        submit = None
    return WorkerPool(
        task_repo,
        {"doc_poll": job},
        state=state_store,
        outbox_submit=submit,
        renew_interval=3600,
    )


class TestNoChange:
    """TC-010: 未发现变化 → 不创建需模型推理的派生任务，该轮 token 成本为零。"""

    async def test_no_change_zero_token(self, task_repo, state_store):
        await state_store.set(WATCH_KEY, {"doc_ids": ["doc-1"]})
        await state_store.set(VERSIONS_KEY, {"versions": {"doc-1": "v1"}})
        job = DocPollJob(document_port=_DocPort({"doc-1": "v1"}))
        worker = _worker(task_repo, state_store, job)

        task = await task_repo.create(_spec())
        final = await worker.execute(task)

        assert final.status == TaskStatus.SUCCEEDED
        assert final.token_cost == 0  # 该轮 token 成本为零
        # 不创建需模型推理的派生任务
        assert await task_repo.count_by_status(TaskStatus.PENDING) == 0


class TestChange:
    """TC-009: 检测到变更 → 只声明派生任务与待发消息，由执行侧落库，可追溯。"""

    async def test_change_derives_traceable_task(self, task_repo, state_store):
        await state_store.set(WATCH_KEY, {"doc_ids": ["doc-1"]})
        await state_store.set(VERSIONS_KEY, {"versions": {"doc-1": "v1"}})
        outbox: list = []
        job = DocPollJob(document_port=_DocPort({"doc-1": "v2"}))
        worker = _worker(task_repo, state_store, job, outbox=outbox)

        task = await task_repo.create(_spec())
        final = await worker.execute(task)

        assert final.status == TaskStatus.SUCCEEDED
        assert final.token_cost == 0  # 确定性 Job 本身零 token
        derived = await task_repo.pending_tasks()
        assert len(derived) == 1
        d = derived[0]
        assert d.job_name == "index_refresh"
        assert d.kind == TaskKind.AGENTIC
        assert d.trigger_source == TriggerSource.DERIVED
        assert d.parent_id == task.id  # 派生任务可追溯至该 Job
        assert d.idem_key == build_idem_key(task_repo.workspace_id, "doc-1", "v2")
        # Job 不直接发送消息：doc_poll 未声明任何待发消息
        assert outbox == []

    async def test_derived_task_enqueued_and_executed(self, task_repo, state_store):
        """TC-009 补：派生任务运行期入队并被 worker 实际领取执行（US2-2 链路贯通）。"""
        await state_store.set(WATCH_KEY, {"doc_ids": ["doc-1"]})
        await state_store.set(VERSIONS_KEY, {"versions": {"doc-1": "v1"}})
        job = DocPollJob(document_port=_DocPort({"doc-1": "v2"}))
        ran: list[str] = []

        async def index_refresh_handler(ctx) -> JobResult:
            ran.append(ctx.task.job_name)
            return JobResult()

        worker = WorkerPool(
            task_repo,
            {"doc_poll": job, "index_refresh": index_refresh_handler},
            state=state_store,
            renew_interval=3600,
        )

        task = await task_repo.create(_spec())
        await worker.execute(task)

        # 派生任务已推入内存队列，不等重启恢复即可被领取
        derived_task = await worker._queue.pop(timeout=0.1)
        assert derived_task.job_name == "index_refresh"
        final = await worker.execute(derived_task)
        assert final.status == TaskStatus.SUCCEEDED
        assert ran == ["index_refresh"]  # 派生任务被 worker 实际执行

    async def test_same_version_not_derived_twice(self, task_repo, state_store):
        """同版本重复探测不再派生（US3-2：同版本全生命周期只创建一次）。"""
        await state_store.set(WATCH_KEY, {"doc_ids": ["doc-1"]})
        job = DocPollJob(document_port=_DocPort({"doc-1": "v2"}))
        worker = _worker(task_repo, state_store, job)

        first = await task_repo.create(_spec())
        await worker.execute(first)  # 首轮发现 v1→v2（基线为无），派生一次
        second = await task_repo.create(_spec())
        final = await worker.execute(second)  # 次轮版本不变

        assert final.status == TaskStatus.SUCCEEDED
        derived = await task_repo.pending_tasks()
        assert len(derived) == 1  # 没有第二条派生任务


class _IndexRepo:
    """索引仓储替身：list_indexed_docs 返回索引内文档。"""

    def __init__(
        self,
        doc_ids: list[str],
        stale_ids: set[str] | None = None,
        *,
        requirement_docs: list[str] = (),
        subscribed_docs: list[str] = (),
    ) -> None:
        self._doc_ids = doc_ids
        self._stale = stale_ids or set()
        self._requirement_docs = list(requirement_docs)
        self._subscribed_docs = list(subscribed_docs)

    async def list_indexed_docs(self) -> list[dict]:
        return [
            {"document_id": d, "stale": d in self._stale} for d in self._doc_ids
        ]

    async def requirement_doc_ids(self) -> list[str]:
        return list(self._requirement_docs)

    async def subscribed_doc_ids(self) -> list[str]:
        return list(self._subscribed_docs)


class TestWatchList:
    """监听清单 = state 补充清单 ∪ 索引内文档 ∪ 需求关联文档 ∪ 被订阅文档。"""

    async def test_indexed_docs_are_watched_without_manual_registration(
        self, task_repo, state_store
    ):
        """索引里的文档自动进入监听——不依赖没人交付的 doc_poll:watch 写入方。"""
        port = _DocPort({"doc-9": "v1"})
        job = DocPollJob(document_port=port, index_repository=_IndexRepo(["doc-9"]))
        worker = _worker(task_repo, state_store, job)

        task = await task_repo.create(_spec())
        final = await worker.execute(task)

        assert final.status == TaskStatus.SUCCEEDED
        assert port.calls == 1  # 空 state 清单也监听了索引文档
        derived = await task_repo.pending_tasks()
        assert len(derived) == 1  # 首轮基线为空 → doc-9 视为变更
        assert derived[0].job_name == "index_refresh"
        assert derived[0].payload["document_ids"] == ["doc-9"]

    async def test_requirement_and_subscribed_docs_are_watched(
        self, task_repo, state_store
    ):
        """需求关联与订阅的文档尚未入库也要监听——首次刷新才会建立快照。"""
        port = _DocPort({"doc-r": "v1", "doc-s": "v1"})
        repo = _IndexRepo([], requirement_docs=["doc-r"], subscribed_docs=["doc-s"])
        job = DocPollJob(document_port=port, index_repository=repo)
        worker = _worker(task_repo, state_store, job)

        task = await task_repo.create(_spec())
        await worker.execute(task)

        derived = await task_repo.pending_tasks()
        assert sorted(d for t in derived for d in t.payload["document_ids"]) == [
            "doc-r", "doc-s",
        ]


class TestStaleRecovery:
    """stale 索引行自愈：版本不变也派生重刷（幂等键带小时 epoch）。

    首次标脏已消费 ``docpoll:{doc}:{version}`` 幂等键（唯一索引对失败任务
    同样生效），不自愈则这篇文档的变更通知永久停摆。
    """

    async def test_stale_doc_rederives_refresh_with_epoch_key(
        self, task_repo, state_store
    ):
        await state_store.set(VERSIONS_KEY, {"versions": {"doc-1": "v1"}})
        port = _DocPort({"doc-1": "v1"})  # 版本不变
        job = DocPollJob(
            document_port=port,
            index_repository=_IndexRepo(["doc-1"], stale_ids={"doc-1"}),
        )
        worker = _worker(task_repo, state_store, job)

        task = await task_repo.create(_spec())
        final = await worker.execute(task)

        assert final.status == TaskStatus.SUCCEEDED
        derived = await task_repo.pending_tasks()
        assert len(derived) == 1
        assert derived[0].job_name == "index_refresh"
        assert derived[0].idem_key != build_idem_key(
            task_repo.workspace_id, "doc-1", "v1"
        )  # 恢复键不同于被消费的变更键

    async def test_fresh_doc_not_rederived(self, task_repo, state_store):
        """非 stale 且版本不变：不派生（自愈不影响正常零产出路径）。"""
        await state_store.set(VERSIONS_KEY, {"versions": {"doc-1": "v1"}})
        port = _DocPort({"doc-1": "v1"})
        job = DocPollJob(
            document_port=port, index_repository=_IndexRepo(["doc-1"])
        )
        worker = _worker(task_repo, state_store, job)

        task = await task_repo.create(_spec())
        await worker.execute(task)

        assert await task_repo.count_by_status(TaskStatus.PENDING) == 0

    async def test_state_and_index_lists_merge(self, task_repo, state_store):
        await state_store.set(WATCH_KEY, {"doc_ids": ["doc-1"]})
        await state_store.set(
            VERSIONS_KEY, {"versions": {"doc-1": "v1", "doc-9": "v1"}}
        )
        port = _DocPort({"doc-1": "v1", "doc-9": "v1"})
        job = DocPollJob(
            document_port=port, index_repository=_IndexRepo(["doc-9"])
        )
        worker = _worker(task_repo, state_store, job)

        task = await task_repo.create(_spec())
        final = await worker.execute(task)

        assert final.status == TaskStatus.SUCCEEDED
        assert await task_repo.count_by_status(TaskStatus.PENDING) == 0  # 都没变

    async def test_index_repo_failure_degrades_to_state_list(
        self, task_repo, state_store
    ):
        """索引清单拿不到不该让 state 清单也听不成。"""

        class BoomRepo:
            async def list_indexed_docs(self) -> list[dict]:
                raise ConnectionError("pg down")

        await state_store.set(WATCH_KEY, {"doc_ids": ["doc-1"]})
        await state_store.set(VERSIONS_KEY, {"versions": {"doc-1": "v1"}})
        port = _DocPort({"doc-1": "v1"})
        job = DocPollJob(document_port=port, index_repository=BoomRepo())
        worker = _worker(task_repo, state_store, job)

        task = await task_repo.create(_spec())
        final = await worker.execute(task)

        assert final.status == TaskStatus.SUCCEEDED
        assert port.calls == 1
