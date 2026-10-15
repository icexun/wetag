"""统一端口契约（§4.5 / doc2 §5.1）——业务层唯一允许依赖的外部接口。

本文件装两类抽象：

1. **四个 Integration Port**：``DocumentPort`` / ``RequirementPort`` /
   ``TodoPort`` / ``CodeRepositoryPort``。任何实现都必须满足这些 Protocol，
   Intelligence 层不感知背后是哪个 CLI。

2. **跨模块协作抽象**：声明在契约、实现在别的模块，使各模块可并行开发，也让
   §4 的 import 方向规则不必被破例——``TopicClassifier`` 是 Space 依赖、
   Capability 实现；``SpaceContextProvider`` 与 ``ChatDirectory`` 是 Channel
   依赖、Space 实现（Channel 禁止 import ``space/``）。

**降级纪律（D-1）**：Port 不可用时抛 ``PortUnavailable``，**不得**用本地台账
伪装成外部系统已写入。``TodoPort`` 只有企微一个实现。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Optional, Protocol, Sequence, runtime_checkable

from app.contracts.ids import (
    ChatId,
    ChatType,
    DocumentId,
    IdemKey,
    RequirementId,
    TodoId,
    TopicId,
    UserId,
    WorkspaceId,
)
from app.contracts.space_context import SpaceContext

# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------


#: 可插拔的外部集成：tapd → 需求口，lark → 文档口与飞书协作工具，wecom → 待办口与企微协作工具
INTEGRATIONS: tuple[str, ...] = ("tapd", "lark", "wecom")


def parse_integrations(raw: Any) -> frozenset[str]:
    """``INTEGRATIONS`` 配置值 → 启用集合。

    接受 YAML 列表展平后的 JSON 串或逗号分隔串；未配置视为全部启用，未知名字丢弃。
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return frozenset(INTEGRATIONS)
    items: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("["):
            import json

            items = json.loads(text)
        else:
            items = text.split(",")
    names = {str(x).strip().lower() for x in items}
    return frozenset(n for n in INTEGRATIONS if n in names)


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    """业务线运行配置（D-2：一业务线一进程，来自 ``config/properties.py`` + 环境变量）。

    §5.1 表清单里**没有** ``workspace`` 表——配置不落库，由进程环境注入。
    """

    workspace_id: WorkspaceId
    tapd_project_id: str | None = None
    wiki_space_id: str | None = None
    repo_root: str | None = None
    repo_branch: str = "main"
    integrations: frozenset[str] = frozenset(INTEGRATIONS)


# ---------------------------------------------------------------------------
# DocumentPort
# ---------------------------------------------------------------------------


class FetchScope(StrEnum):
    """读取粒度——控制读取成本的关键。

    只关心部分章节时取 ``SECTIONS``，**绝不** ``WHOLE``。
    """

    WHOLE = "WHOLE"
    SECTIONS = "SECTIONS"
    OUTLINE = "OUTLINE"


@dataclass(frozen=True, slots=True)
class DocumentRef:
    """文档引用。``anchors`` 仅在 ``FetchScope.SECTIONS`` 下有意义。"""

    document_id: DocumentId
    url: str | None = None
    anchors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WikiSpaceRef:
    """知识库空间。冷启动递归遍历三级树下的 docx 叶子节点。"""

    space_id: str
    root_node_token: str | None = None
    max_depth: int = 3


@dataclass(frozen=True, slots=True)
class DocumentMeta:
    """批量元信息（``drive metas batch_query``）。

    ``revision_id`` / ``modified_time`` / ``content_hash`` 与 ``document_id``
    组成四元组，每次读取都要持久化（doc2 §5.3）。
    """

    document_id: DocumentId
    title: str
    revision_id: int | None = None
    modified_time: datetime | None = None
    content_hash: str | None = None
    owner_id: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentSection:
    """一个章节。``anchor`` 即 block_id，是文档证据 locator 的组成部分。"""

    anchor: str
    heading: str
    text: str
    level: int = 1


@dataclass(frozen=True, slots=True)
class DocumentSnapshot:
    """一次读取的快照。``scope`` 记录实际取到的粒度，供覆盖范围明示使用。"""

    meta: DocumentMeta
    scope: FetchScope
    sections: tuple[DocumentSection, ...] = ()
    outline: tuple[tuple[str, str], ...] = ()  # (anchor, heading)
    raw_text: str | None = None

    def locator(self, anchor: str) -> str:
        """构造文档证据 locator：``{document_id}#{anchor}@rev{revision_id}``。"""
        return f"{self.meta.document_id}#{anchor}@rev{self.meta.revision_id}"


class UpdateCommand(StrEnum):
    """按块更新而非整篇覆盖（doc2 §5.3）。"""

    APPEND = "append"
    STR_REPLACE = "str_replace"
    BLOCK_INSERT_AFTER = "block_insert_after"
    BLOCK_REPLACE = "block_replace"


@dataclass(frozen=True, slots=True)
class DocumentUpdate:
    """一次文档写入。``base_revision_id`` 即乐观锁：revision 已变则重算 diff。"""

    ref: DocumentRef
    command: UpdateCommand
    content: str
    base_revision_id: int | None = None
    anchor: str | None = None
    old_text: str | None = None


@dataclass(frozen=True, slots=True)
class WriteResult:
    """写入结果。``conflict=True`` 表示乐观锁失败，上层重算 diff 后重试一次。"""

    ok: bool
    revision_id: int | None = None
    conflict: bool = False
    error_code: str | None = None


@runtime_checkable
class DocumentPort(Protocol):
    """飞书文档端口。实现见 ``capability/integration/document/lark_cli.py``。"""

    async def fetch(self, ref: DocumentRef, scope: FetchScope) -> DocumentSnapshot: ...

    async def batch_meta(
        self, refs: Sequence[DocumentRef]
    ) -> list[DocumentMeta]: ...

    async def list_tree(self, space: WikiSpaceRef) -> list[DocumentRef]: ...

    async def update(self, cmd: DocumentUpdate, idem_key: IdemKey) -> WriteResult: ...

    # 全文评论。content 为纯文本，实现方自行包装。
    async def add_comment(self, ref: DocumentRef, content: str, idem_key: IdemKey) -> None: ...


# ---------------------------------------------------------------------------
# RequirementPort
# ---------------------------------------------------------------------------


class StatusCategory(StrEnum):
    """TAPD 37 个状态的归类结果。**动态拉取，不硬编码**（doc2 §5.4）。"""

    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    TERMINAL = "TERMINAL"


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """``getProjectConfig`` 的归一结果。

    ``status_map`` 把项目自定义状态名映射到三类，"进行中"判定完全依赖它。
    """

    project_id: str
    status_map: dict[str, StatusCategory] = field(default_factory=dict)
    custom_field_map: dict[str, str] = field(default_factory=dict)
    categories: tuple[str, ...] = ()

    def categorize(self, status: str) -> StatusCategory:
        """未登记状态按 ``ACTIVE`` 处理——宁可多查，不可漏判"进行中"。"""
        return self.status_map.get(status, StatusCategory.ACTIVE)


@dataclass(frozen=True, slots=True)
class Requirement:
    """一条 TAPD 需求。字段映射见 doc2 §5.4。"""

    requirement_id: RequirementId
    tapd_id: str
    workspace_id: WorkspaceId
    title: str
    status: str
    status_category: StatusCategory
    doc_ref: DocumentRef | None = None
    code_hints: tuple[str, ...] = ()
    owners: tuple[str, ...] = ()
    category: str | None = None
    updated_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RequirementChange:
    """变更历史的一条记录，辅助变更检测与追溯。"""

    tapd_id: str
    field_name: str
    old_value: str | None
    new_value: str | None
    changed_at: datetime | None = None
    actor: str | None = None


@dataclass(frozen=True, slots=True)
class RequirementDraft:
    """结构化产出（doc2 §6.1）。

    ``missing_fields`` 非空或 ``confidence`` 偏低时生成澄清问题，
    **不写外部系统**——人工确认后才创建 TAPD 与飞书文档。
    """

    title: str
    background: str = ""
    goal: str = ""
    scope_in: tuple[str, ...] = ()
    scope_out: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    owners: tuple[str, ...] = ()
    workspace_id: WorkspaceId | None = None
    chat_id: str | None = None
    topic_id: str | None = None
    tapd_category: str | None = None
    affected_clients: tuple[str, ...] = ()
    source_message_ids: tuple[str, ...] = ()
    confidence: float = 0.0
    missing_fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RequirementUpdate:
    """需求更新命令。以 ``TAPD_NPC_ROLE`` 身份署名。"""

    tapd_id: str
    fields: dict[str, Any]
    comment: str | None = None


@runtime_checkable
class RequirementPort(Protocol):
    """TAPD 端口。实现：``tapd_cli.py``（官方 tapd-cli）。"""

    async def project_config(self, project_id: str) -> ProjectConfig: ...

    async def list_active(self, ws: WorkspaceConfig) -> list[Requirement]: ...

    async def get_changes(self, story_id: str) -> list[RequirementChange]: ...

    async def create(
        self, draft: RequirementDraft, idem_key: IdemKey
    ) -> Requirement: ...

    async def update(
        self, cmd: RequirementUpdate, idem_key: IdemKey
    ) -> Requirement: ...

    async def quota_watermark(self) -> dict[str, Any]:
        """额度水位（US2-10），至少含 ``remaining`` 键。

        TAPD 没有"查剩余额度"的接口，实现一律走本地滑窗
        估算（``integration/requirement/quota.py``），不得编造服务端数字。
        """
        ...


# ---------------------------------------------------------------------------
# TodoPort
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UserRef:
    """``search_todo_userid`` 的解析结果。"""

    user_id: UserId
    display_name: str


@dataclass(frozen=True, slots=True)
class TodoCreate:
    """建待办命令。"""

    title: str
    assignee_ids: tuple[UserId, ...]
    chat_id: str | None = None
    due_at: datetime | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class TodoRef:
    todo_id: TodoId


@dataclass(frozen=True, slots=True)
class TodoSnapshot:
    """批量回读结果。完成态在 ``finished``，原文状态在 ``status``。

    ``status`` 取 ``deleted`` / ``invisible`` / ``gone`` 表示原生侧已不可见；
    回读结果里根本没有的 id 不进本对象，由调用方按缺失处理。
    """

    todo_id: TodoId
    finished: bool
    follower_status: dict[str, str] = field(default_factory=dict)
    updated_at: datetime | None = None
    content: str = ""
    assignee_ids: tuple[str, ...] = ()
    owner_id: str = ""
    due_at: datetime | None = None
    status: str = ""


@runtime_checkable
class TodoPort(Protocol):
    """企微待办端口。**D-1：唯一实现是 ``todo/wecom.py``**。

    待办域不可用即抛 ``PortUnavailable``，群内明示"能力暂不可用"。
    绝不写本地台账替身冒充已建待办。
    """

    async def resolve_user(self, display_name: str) -> UserRef: ...

    async def create(self, cmd: TodoCreate, idem_key: IdemKey) -> TodoRef: ...

    async def get_batch(self, todo_ids: Sequence[TodoId]) -> list[TodoSnapshot]: ...


# ---------------------------------------------------------------------------
# CodeRepositoryPort
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RepoSnapshot:
    """一次检索固定的仓库快照。

    ``commit_sha`` **禁止留空**（裁决 #14）：A.4 降级掉的只是 ``git blame`` 级归因，
    快照 sha 必须有，否则 G3 的 locator 不成立。
    ``age_hours`` 供调用方判断快照新鲜度。
    """

    root: str
    branch: str
    commit_sha: str
    synced_at: datetime
    age_hours: float = 0.0
    submodules: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RepoTarget:
    """图谱路由收敛出的子仓（1~3 个）。

    ``confidence`` 在图谱资产缺失、只能靠 ``code_hints``
    直接映射时 ≤0.5（P0 #8 退化路径）。
    """

    repo: str
    subdir: str | None = None
    reason: str = ""
    confidence: float = 1.0


@dataclass(frozen=True, slots=True)
class CodeQuery:
    """ripgrep 检索请求。``patterns`` 是术语翻译后的代码实体，不是业务词。"""

    snapshot: RepoSnapshot
    targets: tuple[RepoTarget, ...]
    patterns: tuple[str, ...]
    globs: tuple[str, ...] = ()
    context_lines: int = 3
    max_hits: int = 50


# 代码证据 locator 的唯一合法形态 repo:path:l1-l2@sha[:12]
CODE_LOCATOR_RE = re.compile(r"^[\w.\-/]+:[\w./\-]+:\d+-\d+@[0-9a-f]{12}$")


@dataclass(frozen=True, slots=True)
class CodeEvidence:
    """一条代码证据。

    ``locator`` 形如 ``repo:path:start-end@sha12``，可直接定位到行。
    """

    repo: str
    path: str
    line_start: int
    line_end: int
    commit_sha: str
    excerpt: str
    symbol: str | None = None

    @property
    def locator(self) -> str:
        return (
            f"{self.repo}:{self.path}:{self.line_start}-{self.line_end}"
            f"@{self.commit_sha[:12]}"
        )


@runtime_checkable
class CodeRepositoryPort(Protocol):
    """代码大仓端口（只读）。实现见 ``capability/integration/repo/``。"""

    async def sync(self, ws: WorkspaceConfig) -> RepoSnapshot: ...

    async def search(self, q: CodeQuery) -> list[CodeEvidence]: ...

    async def last_synced_at(self) -> datetime | None:
        """本地仓最近一次同步（fetch）时刻；从未同步过返回 ``None``。

        仓根未配置或不是 git 仓时抛 ``PortUnavailable``——那是装配缺口，
        不是"没同步过"。
        """
        ...


# ---------------------------------------------------------------------------
# 跨模块协作抽象（Channel ↔ Space ↔ Capability）
# ---------------------------------------------------------------------------

class TopicHint(Protocol):
    """事项路由 L3 的判定结果。"""

    topic_id: Optional[str]
    confidence: float


class TopicClassifier(Protocol):
    """事项路由 L3：候选事项的语义判定。

    主责在 Capability（topic-routing，PRD A-6）。Space 只负责 L1/L2 的确定性
    规则与最终裁定。未提供实现时按「宁可新建，不错误合并」新建事项。
    """

    async def classify(
        self,
        text: str,
        candidates: Sequence[Any],
        context: Optional[SpaceContext],
    ) -> Optional[str]:
        """返回命中的 ``topic_id``；无把握时返回 None。"""
        ...


@dataclass(frozen=True)
class InboundRef:
    """Channel 递给 Space 的归属线索。

    只含归属推导与事项路由需要的字段。AgentEvent 是 Channel 的内部形态，
    Space 不认识它——出 Channel 的入站信息一律先收敛成本结构。
    """

    event_id: str
    bot_id: str
    chat_id: ChatId
    chat_type: ChatType
    actor_user_id: str
    text: str = ""
    quote_message_id: Optional[str] = None
    quote_content: Optional[str] = None


class SpaceResolution(Protocol):
    """归属解析结果。Channel 只关心其中的 SpaceContext。"""

    context: SpaceContext


class SpaceContextProvider(Protocol):
    """InboundRef → SpaceContext。实现方为 Space 的 ``context_factory``。

    归属推导链（Bot → 业务线 → 群 → 会话事项）整条都在 Space，Channel 只递入
    线索、取回凭证。该群已属于另一条业务线时抛 ``ChatOwnershipConflict``。
    """

    async def resolve_message(self, ref: InboundRef) -> SpaceResolution: ...

    async def resolve_known_topic(
        self, ref: InboundRef, topic_id: TopicId
    ) -> SpaceResolution: ...


@dataclass(frozen=True)
class ChatBinding:
    """出站目标的归属事实。

    Channel 据此做归属校验与会话窗判定，但不读 ``chat_group``——那张表归
    Space。字段只取出站需要的这几项，不搬运整行。
    """

    chat_id: ChatId
    workspace_id: WorkspaceId
    bot_id: str
    chat_type: ChatType
    last_inbound_at: datetime


class ChatDirectory(Protocol):
    """已登记会话的只读查询面。实现方为 Space 的 ``registry``。"""

    async def binding_of(self, chat_id: ChatId) -> Optional[ChatBinding]: ...


__all__ = [
    "CODE_LOCATOR_RE",
    "ChatBinding",
    "ChatDirectory",
    "CodeEvidence",
    "CodeQuery",
    "CodeRepositoryPort",
    "DocumentMeta",
    "DocumentPort",
    "DocumentRef",
    "DocumentSection",
    "DocumentSnapshot",
    "DocumentUpdate",
    "FetchScope",
    "InboundRef",
    "ProjectConfig",
    "RepoSnapshot",
    "RepoTarget",
    "Requirement",
    "RequirementChange",
    "RequirementDraft",
    "RequirementPort",
    "RequirementUpdate",
    "SpaceContextProvider",
    "SpaceResolution",
    "StatusCategory",
    "TodoCreate",
    "TodoPort",
    "TodoRef",
    "TodoSnapshot",
    "TopicClassifier",
    "TopicHint",
    "UpdateCommand",
    "UserRef",
    "WikiSpaceRef",
    "WorkspaceConfig",
    "WriteResult",
]
