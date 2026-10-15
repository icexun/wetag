"""``RequirementPort`` 的唯一实现：官方 ``tapd-cli``（key=value 参数）。

本模块只负责"让 tapd-cli 把字节吐出来"；之后的判断（状态归类、自定义字段映射、
信封识别）都在 ``normalize.py``。

* **真分页。** ``story list`` 有 ``page`` / ``limit``，"取完了"是确认的事实。
  ``last_list_complete`` 只在撞上 ``max_pages`` 或某页失败时为 False。
* **变更历史。** ``story changes`` 返回 ``WorkitemChange.field_changes``。

红线："解析不出来"与"确实是空"绝不合并成同一个返回值。``_unwrap`` 失败即抛
``PortUnavailable``，从不降级成 ``[]``——空需求列表会一路变成"没有变更"。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence

from app.capability.integration.requirement import normalize as nz
from app.capability.integration.requirement.normalize import StatusClassifier
from app.capability.integration.requirement.quota import QuotaMeter
from app.contracts.errors import PortUnavailable
from app.contracts.ids import IdemKey, WorkspaceId
from app.contracts.ports import (
    ProjectConfig,
    Requirement,
    RequirementChange,
    RequirementDraft,
    RequirementUpdate,
    StatusCategory,
    WorkspaceConfig,
)
from app.platform.cli.result import CliErrorCode, CliResult

logger = logging.getLogger(__name__)

_SOURCE = "tapd.cli"

# 分页上限。撞到它说明项目需求量超出预期，此时**不能**假装取全了。
_MAX_PAGES = 50
# 按创建时间正序翻页：翻页期间新建的需求落在末页，不会把已翻过的页挤偏。
_LIST_ORDER = "created asc"


class TapdCliUnavailable(PortUnavailable):
    """``tapd-cli`` 没装。与 ``AUTH_FAILED`` 分开：没装不是安全事件。"""

    def __init__(self, detail: str = "") -> None:
        super().__init__(
            _SOURCE, CliErrorCode.BINARY_MISSING.value, detail or "tapd-cli 未安装"
        )


@dataclass(slots=True)
class _CachedConfig:
    config: ProjectConfig
    fetched_at: float


class TapdCliAdapter:
    """``RequirementPort`` 的 CLI 实现。"""

    def __init__(
        self,
        executor: Any,
        *,
        workspace_id: WorkspaceId,
        project_id: str | None = None,
        audit: Any | None = None,
        idempotency: Any | None = None,
        config_ttl_s: float = 600.0,
        page_size: int = 200,
        max_pages: int = _MAX_PAGES,
        terminal_overrides: Sequence[str] = (),
        suspended_overrides: Sequence[str] = (),
        active_overrides: Sequence[str] = (),
        field_labels: Mapping[str, str] | None = None,
        quota: QuotaMeter | None = None,
    ) -> None:
        self._cli = executor
        self._ws = workspace_id
        self._project_id = project_id
        self._audit = audit
        self._idem = idempotency
        self._quota = quota or QuotaMeter()
        self._config_ttl = config_ttl_s
        self._page_size = page_size
        self._max_pages = max_pages
        self._labels = dict(field_labels or nz.DEFAULT_FIELD_LABELS)
        self._classifier = StatusClassifier(
            terminal_overrides=terminal_overrides,
            suspended_overrides=suspended_overrides,
            active_overrides=active_overrides,
        )
        self._cache: dict[str, _CachedConfig] = {}
        self.last_list_complete: bool = True
        self.probe_error: str | None = None

    # -- 可用性探测 -------------------------------------------------------
    async def probe(self) -> bool:
        """``user info``：一条只读、无参的命令，同时验证二进制与凭据。

        只有"不可能自愈"的失败判为不可用：没装、命令面不符、鉴权失败。超时、5xx
        仍判可用——启动瞬间的抖动不该让 TAPD 整个进程周期都下线。
        """
        try:
            result = await self._run("tapd.user.info", {})
        except Exception as exc:  # noqa: BLE001 探测本身不该把调用方打断
            logger.warning("tapd-cli 探测异常：%s", exc)
            self.probe_error = f"探测异常：{exc}"
            return False
        if result.ok:
            self.probe_error = None
            return True
        detail = result.stderr_digest or result.stdout_digest or ""
        if result.error_code is CliErrorCode.BINARY_MISSING:
            self.probe_error = "tapd-cli 未安装"
        elif result.error_code in (
            CliErrorCode.AUTH_FAILED, CliErrorCode.PERMISSION_DENIED
        ):
            self.probe_error = f"TAPD 鉴权失败：{detail}"
        elif "unknown command" in detail.lower() or "未设置环境变量" in detail:
            self.probe_error = f"tapd-cli 不可用：{detail}"
        else:
            logger.warning("tapd-cli 探测失败但判为可用（%s）：%s", result.error_code, detail)
            self.probe_error = None
            return True
        logger.warning("%s", self.probe_error)
        return False

    # -- 项目配置 ---------------------------------------------------------
    async def project_config(self, project_id: str) -> ProjectConfig:
        """``story fields`` → 归一的 ``ProjectConfig``。"""
        pid = str(nz.project_id_int(project_id, source=_SOURCE))
        cached = self._cache.get(pid)
        if cached is not None and (time.monotonic() - cached.fetched_at) < self._config_ttl:
            return cached.config

        payload = self._unwrap(
            await self._run("tapd.story.fields", {"workspaceid": pid}),
            what="story fields",
        )
        data = nz.object_of(payload, what="story fields", source=_SOURCE)
        config = nz.build_project_config(
            nz.fields_info_to_config(data),
            project_id=pid,
            classifier=self._classifier,
            labels=self._labels,
            source="tapd.story_fields",
        )
        self._cache[pid] = _CachedConfig(config=config, fetched_at=time.monotonic())
        return config

    def _categorize(self, status_name: str) -> StatusCategory:
        return self._classifier.categorize(status_name)

    # -- 列表 -------------------------------------------------------------
    async def list_active(self, ws: WorkspaceConfig) -> list[Requirement]:
        """进行中需求（非终态），**翻完所有页**。

        不在服务端按状态过滤：项目状态随时可加，全量取回后按 ``status_map`` 归类。
        任何一页失败立刻抛并保持 ``last_list_complete=False``，绝不用"已拿到的那几页"
        当全量。
        """
        if not ws.tapd_project_id:
            raise PortUnavailable(
                "tapd.list_active",
                "CONFIG_MISSING",
                f"workspace {ws.workspace_id} 未配置 tapd_project_id",
            )
        config = await self.project_config(ws.tapd_project_id)
        pid = str(nz.project_id_int(ws.tapd_project_id, source=_SOURCE))

        rows: dict[str, dict[str, Any]] = {}
        fetched = 0
        self.last_list_complete = False
        for page in range(1, self._max_pages + 1):
            payload = self._unwrap(
                await self._run(
                    "tapd.story.list",
                    {
                        "workspaceid": pid,
                        "limit": self._page_size,
                        "page": page,
                        "order": _LIST_ORDER,
                        "with_v_status": True,
                    },
                ),
                what=f"story list p{page}",
            )
            batch = nz.rows_of(payload, what=f"story list p{page}", source=_SOURCE)
            fetched += len(batch)
            for row in batch:
                rows[str(row.get("id") or len(rows))] = row
            if len(batch) < self._page_size:
                self.last_list_complete = True
                break
        else:
            logger.warning(
                "story list 翻到第 %d 页仍是满页，停止并标记本轮清单不完整",
                self._max_pages,
            )

        out: list[Requirement] = []
        skipped = 0
        for row in rows.values():
            req = nz.to_requirement(row, config, workspace_id=self._ws)
            if req is None:
                skipped += 1
                continue
            if req.status_category is StatusCategory.TERMINAL:
                continue
            out.append(req)
        if skipped:
            logger.warning("story list 有 %d 行缺少 id/标题，已跳过", skipped)
        logger.info(
            "TAPD 进行中需求 project=%s 取回=%d 进行中=%d complete=%s",
            pid,
            fetched,
            len(out),
            self.last_list_complete,
        )
        return out

    # -- 变更历史 ---------------------------------------------------------
    async def get_changes(self, story_id: str) -> list[RequirementChange]:
        """``story changes`` → 变更记录。识别不出结构时抛，而不是返回 ``[]``。"""
        pid = self._pid("tapd.get_changes")
        payload = self._unwrap(
            await self._run(
                "tapd.story.changes", {"workspaceid": pid, "story-id": str(story_id)}
            ),
            what="story changes",
        )
        rows = nz.rows_of(payload, what="story changes", source=_SOURCE)
        out = [c for row in rows for c in _changes_of(row, story_id)]
        logger.info("TAPD 变更历史 story=%s 记录=%d", story_id, len(out))
        return out

    # -- 写操作 -----------------------------------------------------------
    async def create(self, draft: RequirementDraft, idem_key: IdemKey) -> Requirement:
        """建卡。``tapd.story.add`` 不重试（无幂等键），外层护栏是 ``Idempotency``。

        草稿没准备好时**不写**——结构化层也拦一道，不是冗余。
        """
        if draft.missing_fields:
            raise PortUnavailable(
                "tapd.create",
                "DRAFT_INCOMPLETE",
                f"草稿缺字段 {list(draft.missing_fields)}，需人工补齐后再建卡",
            )
        if not draft.title.strip():
            raise PortUnavailable("tapd.create", "DRAFT_INCOMPLETE", "草稿没有标题")
        if not draft.workspace_id:
            raise PortUnavailable("tapd.create", "CONFIG_MISSING", "草稿未带 workspace_id")
        pid = self._pid("tapd.create")
        params = nz.draft_to_params(draft)

        async def _do() -> Requirement:
            args: dict[str, Any] = {"workspaceid": pid, "name": params["name"]}
            for key in ("description", "owner", "category_id"):
                if params.get(key):
                    args[key] = params[key]
            payload = self._unwrap(
                await self._run("tapd.story.add", args, idem_key=str(idem_key)),
                what="story add",
                retryable=False,  # 可能已经建成功了，重试会产生第二张卡
            )
            req = await self._one(payload, pid, op="tapd.create", what="story add")
            await self._audit_write("tapd.create", tapd_id=req.tapd_id, title=req.title)
            return req

        return await self._guarded("tapd.create", idem_key, _do)

    async def update(self, cmd: RequirementUpdate, idem_key: IdemKey) -> Requirement:
        """改字段（``story update``）和/或发评论（``comment add``）。

        字段键不在登记表里（``TapdStoryUpdateArgs``，自定义字段只放行
        ``custom_field_*``）时参数校验失败、不起进程。
        """
        if not cmd.fields and not (cmd.comment or "").strip():
            raise PortUnavailable("tapd.update", "EMPTY_UPDATE", "没有要改的字段或评论")
        pid = self._pid("tapd.update")

        async def _do() -> Requirement:
            req: Requirement | None = None
            if cmd.fields:
                payload = self._unwrap(
                    await self._run(
                        "tapd.story.update",
                        {**dict(cmd.fields), "workspaceid": pid, "id": str(cmd.tapd_id)},
                        idem_key=str(idem_key),
                    ),
                    what="story update",
                    retryable=False,
                )
                req = await self._one(payload, pid, op="tapd.update", what="story update")
            if (cmd.comment or "").strip():
                self._unwrap(
                    await self._run(
                        "tapd.comment.add",
                        {
                            "workspaceid": pid,
                            "entry-type": "stories",
                            "entry-id": str(cmd.tapd_id),
                            "description": cmd.comment,
                        },
                        idem_key=f"{idem_key}:comment",
                    ),
                    what="comment add",
                    retryable=False,
                )
            if req is None:
                req = await self._get(pid, str(cmd.tapd_id))
            await self._audit_write(
                "tapd.update",
                tapd_id=cmd.tapd_id,
                fields=sorted(cmd.fields),
                commented=bool(cmd.comment),
            )
            return req

        return await self._guarded("tapd.update", idem_key, _do)

    # -- 额度水位（US2-10）------------------------------------------------
    async def quota_watermark(self) -> dict[str, int]:
        """本地滑窗估算，依据见 ``quota.py``——TAPD 没有查剩余额度的接口。"""
        return self._quota.watermark()

    # -- 内部 -------------------------------------------------------------
    def _pid(self, op: str) -> str:
        if not self._project_id:
            raise PortUnavailable(op, "CONFIG_MISSING", "未配置 tapd_project_id")
        return str(nz.project_id_int(self._project_id, source=_SOURCE))

    async def _get(self, pid: str, story_id: str) -> Requirement:
        payload = self._unwrap(
            await self._run(
                "tapd.story.list",
                {"workspaceid": pid, "id": story_id, "limit": 1, "with_v_status": True},
            ),
            what="story get",
        )
        return await self._one(payload, pid, op="tapd.get", what="story get")

    async def _one(self, payload: Any, pid: str, *, op: str, what: str) -> Requirement:
        rows = nz.rows_of(payload, what=what, source=_SOURCE)
        if not rows:
            raise PortUnavailable(
                op, "SCHEMA_UNKNOWN", f"{what} 没有返回需求对象，无法确认是否生效"
            )
        config = await self._config_for(pid)
        req = nz.to_requirement(rows[0], config, workspace_id=self._ws)
        if req is None:
            raise PortUnavailable(op, "SCHEMA_UNKNOWN", f"{what} 返回缺少 id")
        return req

    async def _run(self, key: str, args: dict[str, Any], **kw: Any) -> CliResult[Any]:
        """所有 TAPD 调用的唯一出口：计额度、认耗尽。成败都计数。"""
        self._quota.record()
        result = await self._cli.run(key, args, **kw)
        if result.error_code is CliErrorCode.QUOTA_EXCEEDED:
            self._quota.note_exhausted()
        return result

    def _unwrap(
        self, result: CliResult[Any], *, what: str, retryable: bool | None = None
    ) -> Any:
        if result.error_code is CliErrorCode.BINARY_MISSING:
            raise TapdCliUnavailable(f"{what}：tapd-cli 未安装")
        if not result.ok or result.data is None:
            raise PortUnavailable(
                _SOURCE,
                result.error_code.value,
                f"{what} 失败：{result.stderr_digest or result.stdout_digest or '无输出'}",
                retryable=result.retryable if retryable is None else retryable,
            )
        return result.data

    async def _config_for(self, project_id: str) -> ProjectConfig:
        try:
            return await self.project_config(project_id)
        except PortUnavailable as exc:
            # 写操作已经成功了：配置取不到就用空配置（状态一律 ACTIVE），
            # **不能因此把已完成的写报成失败**——那会引来重试，也就是第二张卡。
            logger.warning("取项目配置失败（%s），返回行的自定义字段将不完整", exc)
            return ProjectConfig(project_id=project_id)

    async def _guarded(
        self, operation: str, idem_key: IdemKey, action: Callable[[], Awaitable[Any]]
    ) -> Any:
        if self._idem is None:
            logger.warning(
                "%s 未经幂等护栏直连（未注入 Idempotency）：重试可能产生重复对象",
                operation,
            )
            return await action()
        outcome = await self._idem.guard(idem_key, operation, action)
        if outcome.replayed:
            logger.info("%s 命中幂等重放 key=%s，未再次调用 TAPD", operation, idem_key)
        return outcome.value

    async def _audit_write(self, operation: str, **fields: Any) -> None:
        if self._audit is None:
            return
        try:
            await self._audit.tool_call(
                tool=operation, workspace_id=str(self._ws), risk="HIGH_WRITE", **fields
            )
        except Exception as exc:  # noqa: BLE001 审计失败不该回滚已完成的外部写
            logger.warning("审计写入失败 operation=%s：%s", operation, exc)


# --- 变更记录解析 -----------------------------------------------------------

# 实测 ``WorkitemChange.field_changes`` 为数组；也接一行一个字段的扁平形态。
_CHANGE_LIST_KEYS = ("field_changes", "change_fields")


def _changes_of(row: Mapping[str, Any], story_id: str) -> list[RequirementChange]:
    tapd_id = str(row.get("story_id") or row.get("entity_id") or story_id)
    changed_at = nz.parse_dt(row.get("created") or row.get("modified"))
    actor = _first_str(row, ("creator", "author", "modifier", "operator"))

    nested: list[Mapping[str, Any]] = []
    for key in _CHANGE_LIST_KEYS:
        val = row.get(key)
        if isinstance(val, list):
            nested = [x for x in val if isinstance(x, Mapping)]
            break

    if nested:
        return [
            RequirementChange(
                tapd_id=tapd_id,
                field_name=_field_name(item),
                old_value=_opt_str(item.get("value_before") or item.get("old_value")),
                new_value=_opt_str(item.get("value_after") or item.get("new_value")),
                changed_at=nz.parse_dt(item.get("created")) or changed_at,
                actor=actor,
            )
            for item in nested
            if _field_name(item)
        ]

    name = _field_name(row)
    if not name:
        return []
    return [
        RequirementChange(
            tapd_id=tapd_id,
            field_name=name,
            old_value=_opt_str(row.get("value_before") or row.get("old_value")),
            new_value=_opt_str(row.get("value_after") or row.get("new_value")),
            changed_at=changed_at,
            actor=actor,
        )
    ]


def _field_name(node: Mapping[str, Any]) -> str:
    return str(node.get("field") or node.get("field_name") or "").strip()


def _first_str(node: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    for key in keys:
        val = node.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _opt_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


__all__ = ["TapdCliAdapter", "TapdCliUnavailable"]
