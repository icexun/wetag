"""CLI 命令白名单登记表（doc2 §5.2「命令白名单」）。

**本文件是"能对外做什么"的完整清单**。不在这里的命令在运行时不可达。

裁决 #10 的落法：**删除类命令一律不登记**。
``lark docs +delete``、``drive files +delete``、``tapd story delete``、
``git push``/``git reset --hard`` 等都刻意缺席——不可达优于运行时拒绝，
因为"运行时拒绝"依赖一段可能被改错的判断代码，而"没登记"依赖的是这份清单本身。

风险分档（§10.3 四级）今日 Auto Mode 全通（裁决 #13），但分档先落下来：
``RiskLevel.DESTRUCTIVE`` 有定义而**无任何命令使用**。
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

from app.platform.cli.executor import CliCommandSpec, RiskLevel

# ---------------------------------------------------------------------------
# 参数 schema。字段名即 --kebab-case 参数名（executor._argv_from_model）。
# ---------------------------------------------------------------------------


class _Args(BaseModel):
    """所有命令参数的基类。``extra="forbid"`` 是刻意的。

    pydantic 默认静默丢弃多余字段。放在这里就意味着：调用方写了
    ``{"branch": "main"}`` 而这个命令没有 ``branch`` 字段时，命令会**照常执行**，
    只是少了那个参数——``git fetch`` 会去拉默认 refspec 而不是指定分支，而调用方
    以为自己限定了分支。这类错静默发生、事后极难归因。

    ``forbid`` 把它变成构造期的 ``ValidationError``，在任何子进程被启动之前。
    """

    model_config = {"extra": "forbid"}


class LarkDocsFetchArgs(_Args):
    """``lark-cli docs +fetch``。

    ``scope``/``detail`` 决定成本：能只读命中章节（``section``）就不读整篇。
    """

    doc: str
    scope: str = "full"  # full | outline | section
    detail: str | None = None  # simple | with-ids
    max_depth: int | None = None
    start_block_id: str | None = None
    format: str = "json"
    as_: str | None = Field(default=None, alias="as")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class LarkDriveMetasArgs(_Args):
    """``lark-cli drive metas batch_query``。

    实测契约：该命令**不接受** ``--tokens``，而是 ``--data`` 传 JSON body
    ``{"request_docs":[{"doc_token","doc_type"}],"with_url":true}``。
    单批 ≤50 由调用方（``LarkDocumentAdapter.batch_meta``）分批保证；
    这里只做长度校验兜底。

    返回的 meta 含 ``title``/``latest_modify_time``/``owner_id``，但
    **不含 revision_id**——那一项只能从 ``docs +fetch`` 得到。
    """

    data: str = Field(min_length=2, max_length=8192)
    format: str = "json"


class LarkWikiNodesArgs(_Args):
    """知识库递归遍历。冷启动发现三级树下的 docx 叶子节点。"""

    space_id: str
    parent_node_token: str | None = None
    page_size: int = 50
    page_token: str | None = None
    # 实测：CLI 自带 --page-all 翻页，省掉我们自己维护 page_token 循环
    page_all: bool = True
    page_limit: int = 20
    format: str = "json"


class LarkDocsUpdateArgs(_Args):
    """``lark-cli docs +update``。

    ``revision_id`` 即乐观锁：平台侧 revision 已变则返回 CONFLICT，
    上层重算 diff 后重试**一次**（``error_map.RECOMPUTE_RETRY_ONCE``）。
    ``command`` 刻意不含任何删除语义。
    """

    doc: str
    command: Literal[
        "append", "overwrite", "str_replace", "block_insert_after", "block_replace"
    ]
    content: str
    revision_id: int | None = None
    block_id: str | None = None
    # 实测 flag 名是 --pattern（不是 --old-text/--old-str）；str_replace 必填
    pattern: str | None = None
    doc_format: str = "xml"
    format: str = "json"

    @model_validator(mode="after")
    def _check(self) -> "LarkDocsUpdateArgs":
        if self.command == "str_replace" and not self.pattern:
            raise ValueError("str_replace 必须提供 pattern")
        if self.command in ("block_insert_after", "block_replace") and not self.block_id:
            raise ValueError(f"{self.command} 必须提供 block_id")
        return self


class LarkDocsCreateArgs(_Args):
    """``lark-cli docs +create``（对话 Agent 建文档用）。

    正文经 ``--content`` 以 argv 独立元素传入（executor 的 stdin 是 DEVNULL，
    不能用 ``-``）；单篇需求文档几个 KB，远低于单参数长度上限。
    """

    title: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1)
    doc_format: Literal["markdown", "xml"] = "markdown"
    # folder token 或 wiki node token；不给则建在默认位置
    parent_token: str | None = None
    format: str = "json"


class LarkDriveCommentAddArgs(_Args):
    """``lark-cli drive +add-comment``：全文评论。

    ``content`` 是 reply_elements JSON 数组（``[{"type":"text","text":...}]``），
    文本总量上限 10000 字；``--block-id`` 不传即全文评论。
    """

    doc: str
    content: str = Field(min_length=2, max_length=12000)
    type: str | None = None  # 裸 token 必填（docx）；URL 可自动识别
    full_comment: bool = True
    format: str = "json"


class LarkDocsSearchArgs(_Args):
    """``lark-cli docs +search``（Search v2：doc_wiki/search）。"""

    query: str = Field(min_length=1, max_length=256)
    page_size: str = "15"
    page_token: str | None = None
    format: str = "json"


# --- tapd-cli（官方 CLI，参数为 key=value）-----------------------------------
# 字段名即参数名（含连字符的走 alias）。列表类统一带 limit/page/order/fields。

_TAPD_CUSTOM_KEY = re.compile(r"^custom_(?:plan_)?field_[0-9a-z_]+$")
_TapdId = Annotated[str, Field(pattern=r"^\d{1,32}$")]
_Short = Annotated[str, Field(max_length=512)]
_Html = Annotated[str, Field(max_length=200_000)]


class _TapdArgs(_Args):
    workspaceid: _TapdId

    model_config = {"populate_by_name": True, "extra": "forbid"}


class _TapdListArgs(_TapdArgs):
    limit: int = Field(default=30, ge=1, le=200)
    page: int = Field(default=1, ge=1)
    order: _Short | None = None
    fields: _Short | None = None


class _TapdWriteArgs(_TapdArgs):
    """写命令额外放行 ``custom_field_*``（各项目不同）；其余未登记键仍拒绝。"""

    model_config = {"populate_by_name": True, "extra": "allow"}

    @model_validator(mode="after")
    def _only_custom_extras(self) -> "_TapdWriteArgs":
        for key, value in (self.model_extra or {}).items():
            if not _TAPD_CUSTOM_KEY.match(key):
                raise ValueError(f"未登记的字段：{key}")
            if not isinstance(value, (str, int, float)) or len(str(value)) > 4096:
                raise ValueError(f"自定义字段值非法：{key}")
        return self


class TapdUserInfoArgs(_Args):
    """``user info``：无参数。用作可用性探测（二进制 + 凭据）。"""


class TapdUserMembersArgs(_TapdArgs):
    user: _Short | None = None
    fields: _Short | None = None


class TapdStoryFieldsArgs(_TapdArgs):
    """``story fields``：状态 / 分类 / 自定义字段的键与中文名，动态拉取不硬编码。"""


class TapdStoryListArgs(_TapdListArgs):
    id: _Short | None = None
    name: _Short | None = None
    status: _Short | None = None
    v_status: _Short | None = None
    with_v_status: bool = False
    owner: _Short | None = None
    developer: _Short | None = None
    creator: _Short | None = None
    iteration_id: _Short | None = None
    category_id: _Short | None = None
    parent_id: _Short | None = None
    label: _Short | None = None
    created: _Short | None = None
    modified: _Short | None = None


class TapdStoryChangesArgs(_TapdArgs):
    story_id: _TapdId = Field(alias="story-id")
    limit: int = Field(default=100, ge=1, le=100)
    page: int = Field(default=1, ge=1)


class TapdStoryRelatedBugsArgs(_TapdArgs):
    story_id: _TapdId = Field(alias="story-id")


class TapdStoryAddArgs(_TapdWriteArgs):
    name: str = Field(min_length=1, max_length=256)
    description: _Html | None = None
    priority_label: _Short | None = None
    owner: _Short | None = None
    cc: _Short | None = None
    developer: _Short | None = None
    begin: _Short | None = None
    due: _Short | None = None
    iteration_id: _Short | None = None
    category_id: _Short | None = None
    parent_id: _Short | None = None
    label: _Short | None = None


class TapdStoryUpdateArgs(_TapdWriteArgs):
    id: _TapdId
    name: str | None = Field(default=None, min_length=1, max_length=256)
    description: _Html | None = None
    status: _Short | None = None
    v_status: _Short | None = None
    priority_label: _Short | None = None
    owner: _Short | None = None
    cc: _Short | None = None
    developer: _Short | None = None
    begin: _Short | None = None
    due: _Short | None = None
    iteration_id: _Short | None = None
    category_id: _Short | None = None
    label: _Short | None = None


class TapdBugFieldsArgs(_TapdArgs):
    pass


class TapdBugListArgs(_TapdListArgs):
    id: _Short | None = None
    title: _Short | None = None
    status: _Short | None = None
    v_status: _Short | None = None
    severity: _Short | None = None
    priority_label: _Short | None = None
    current_owner: _Short | None = None
    reporter: _Short | None = None
    iteration_id: _Short | None = None
    module: _Short | None = None
    label: _Short | None = None
    created: _Short | None = None
    modified: _Short | None = None


class TapdBugAddArgs(_TapdWriteArgs):
    title: str = Field(min_length=1, max_length=256)
    description: _Html | None = None
    severity: _Short | None = None
    priority_label: _Short | None = None
    current_owner: _Short | None = None
    cc: _Short | None = None
    reporter: _Short | None = None
    deadline: _Short | None = None
    iteration_id: _Short | None = None
    module: _Short | None = None
    label: _Short | None = None


class TapdBugUpdateArgs(_TapdWriteArgs):
    id: _TapdId
    title: str | None = Field(default=None, min_length=1, max_length=256)
    description: _Html | None = None
    status: _Short | None = None
    v_status: _Short | None = None
    severity: _Short | None = None
    priority_label: _Short | None = None
    current_owner: _Short | None = None
    resolution: _Short | None = None
    cc: _Short | None = None
    fixer: _Short | None = None
    deadline: _Short | None = None
    iteration_id: _Short | None = None
    module: _Short | None = None
    label: _Short | None = None


class TapdTaskListArgs(_TapdListArgs):
    id: _Short | None = None
    name: _Short | None = None
    status: _Short | None = None
    owner: _Short | None = None
    story_id: _Short | None = None
    iteration_id: _Short | None = None
    creator: _Short | None = None
    priority_label: _Short | None = None
    label: _Short | None = None
    created: _Short | None = None
    modified: _Short | None = None


class TapdTaskAddArgs(_TapdWriteArgs):
    name: str = Field(min_length=1, max_length=256)
    description: _Html | None = None
    story_id: _Short | None = None
    parent_id: _Short | None = None
    owner: _Short | None = None
    cc: _Short | None = None
    priority_label: _Short | None = None
    iteration_id: _Short | None = None
    begin: _Short | None = None
    due: _Short | None = None
    label: _Short | None = None


class TapdTaskUpdateArgs(_TapdWriteArgs):
    id: _TapdId
    name: str | None = Field(default=None, min_length=1, max_length=256)
    description: _Html | None = None
    status: _Short | None = None
    owner: _Short | None = None
    priority_label: _Short | None = None
    story_id: _Short | None = None
    iteration_id: _Short | None = None
    begin: _Short | None = None
    due: _Short | None = None
    label: _Short | None = None


TapdEntryType = Literal["stories", "bug", "bug_remark", "tasks"]


class TapdCommentListArgs(_TapdListArgs):
    entry_type: TapdEntryType | None = Field(default=None, alias="entry-type")
    entry_id: _TapdId | None = Field(default=None, alias="entry-id")


class TapdCommentAddArgs(_TapdArgs):
    """description 为 Markdown，CLI 自动转 HTML。"""

    entry_type: TapdEntryType = Field(alias="entry-type")
    entry_id: _TapdId = Field(alias="entry-id")
    description: str = Field(min_length=1, max_length=65536)
    root_id: _TapdId | None = Field(default=None, alias="root-id")
    reply_id: _TapdId | None = Field(default=None, alias="reply-id")


class TapdIterationListArgs(_TapdListArgs):
    id: _Short | None = None
    name: _Short | None = None
    status: _Short | None = None
    startdate: _Short | None = None
    enddate: _Short | None = None


class _LarkArgs(_Args):
    format: str = "json"

    model_config = {"populate_by_name": True, "extra": "forbid"}


_Text = Annotated[str, Field(max_length=200_000)]


class _LarkSheetRef(_LarkArgs):
    """表格定位：``url`` 必填，工作表二选一（``sheet_id`` / ``sheet_name``）。"""

    url: _Short
    sheet_id: _Short | None = None
    sheet_name: _Short | None = None

    @model_validator(mode="after")
    def _one_sheet(self) -> "_LarkSheetRef":
        if bool(self.sheet_id) == bool(self.sheet_name):
            raise ValueError("sheet_id 与 sheet_name 必须且只能给一个")
        return self


class LarkSheetsCsvGetArgs(_LarkSheetRef):
    range: _Short | None = None
    max_chars: int = Field(default=50_000, ge=1000, le=500_000)


class LarkSheetsCsvPutArgs(_LarkSheetRef):
    start_cell: str = Field(pattern=r"^[A-Z]{1,3}[0-9]{1,7}$")
    csv: _Text


class LarkSheetsCellsSearchArgs(_LarkSheetRef):
    find: str = Field(min_length=1, max_length=256)
    range: _Short | None = None


class LarkBaseTableListArgs(_LarkArgs):
    base_token: _Short
    limit: int = Field(default=50, ge=1, le=100)


class LarkBaseFieldListArgs(_LarkArgs):
    base_token: _Short
    table_id: _Short
    limit: int = Field(default=100, ge=1, le=200)


class LarkBaseRecordListArgs(_LarkArgs):
    base_token: _Short
    table_id: _Short
    view_id: _Short | None = None
    filter_json: str | None = Field(default=None, max_length=8192)
    field_id: list[_Short] = Field(default_factory=list, max_length=50)
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


class LarkBaseRecordUpsertArgs(_LarkArgs):
    """无 ``record_id`` 为新建，有则更新该行。字段值 JSON 见 ``--json``。"""

    base_token: _Short
    table_id: _Short
    record_id: _Short | None = None
    json_: str = Field(alias="json", min_length=2, max_length=65536)


class LarkContactSearchArgs(_LarkArgs):
    query: str = Field(min_length=1, max_length=50)
    page_size: int = Field(default=10, ge=1, le=30)


class LarkTaskCreateArgs(_LarkArgs):
    summary: str = Field(min_length=1, max_length=256)
    description: _Text | None = None
    due: _Short | None = None
    assignee: _Short | None = None
    idempotency_key: _Short | None = None


class LarkTaskMyArgs(_LarkArgs):
    query: _Short | None = None
    due_end: _Short | None = None
    page_limit: int = Field(default=20, ge=1, le=40)


class LarkTaskCompleteArgs(_LarkArgs):
    task_id: _Short


class LarkTaskSearchArgs(_LarkArgs):
    query: str = Field(min_length=1, max_length=256)
    page_limit: int = Field(default=20, ge=1, le=40)


class LarkCalendarAgendaArgs(_LarkArgs):
    start: _Short | None = None
    end: _Short | None = None


class LarkCalendarFreebusyArgs(_LarkArgs):
    start: _Short | None = None
    end: _Short | None = None
    user_id: _Short | None = None


class LarkCalendarSearchArgs(_LarkArgs):
    query: _Short | None = None
    start: _Short | None = None
    end: _Short | None = None
    page_size: str = "20"


class LarkCalendarCreateArgs(_LarkArgs):
    summary: str = Field(min_length=1, max_length=256)
    start: _Short
    end: _Short
    description: _Text | None = None
    attendee_ids: _Short | None = None


class LarkApprovalTasksArgs(_LarkArgs):
    """topic：1 待办 / 2 已办 / 3 已发起 / 17 未读知会 / 18 已读知会。"""

    topic: Literal["1", "2", "3", "17", "18"]
    page_size: int = Field(default=20, ge=1, le=100)


class LarkApprovalInstanceArgs(_LarkArgs):
    instance_code: _Short


class LarkWikiNodeListArgs(_LarkArgs):
    space_id: _Short
    parent_node_token: _Short | None = None
    page_size: int = Field(default=50, ge=1, le=50)


class LarkWikiNodeCreateArgs(_LarkArgs):
    title: str = Field(min_length=1, max_length=256)
    space_id: _Short | None = None
    parent_node_token: _Short | None = None
    obj_type: Literal["docx", "sheet", "bitable", "mindnote"] = "docx"


class RgSearchArgs(_Args):
    """``rg --json``。逐行解析 ``type=="match"``（见 repo/search.py）。

    ``--json`` 固定在 ``argv_prefix`` 里而非字段——字段名 ``json`` 会遮蔽
    ``BaseModel`` 的属性。``path`` 声明为位置参数（rg 不认 ``--path``）。
    """

    regexp: list[str] = Field(min_length=1)
    # 与 git.grep 同理：检索词部分来自不可信数据，当正则跑会搜错东西且可能回溯爆炸
    fixed_strings: bool = True
    glob: list[str] = []
    max_count: int | None = None
    context: int = 3
    ignore_case: bool = False
    path: str = "."


class GitRevParseArgs(_Args):
    """取当前快照 HEAD sha。裁决 #14：commit **禁止留空**。"""

    verify: bool = True
    rev: str = "HEAD"  # 位置参数


class GitGrepArgs(_Args):
    """``git grep``——``rg`` 缺失时的等价实现（``BINARY_MISSING`` 触发切换）。

    比 ``rg`` 多一个**本质优势**：可以直接搜某个 ``treeish``，于是"被检索的内容"
    与 locator 里的 ``@sha`` 严格同源。搜工作区则依赖"只读仓不会有本地改动"
    这个假设，而假设会被违反。

    ``fixed_strings`` 默认 True：检索词可能来自需求文档或用户消息（不可信数据），
    当正则跑会带来两个真问题——特殊字符让检索静默搜错东西，以及嵌套量词造成
    灾难性回溯把 CPU 吃满。定长匹配两者都不存在。
    """

    pattern: str = Field(min_length=2, max_length=512)
    treeish: str = Field(min_length=4, max_length=64)
    max_count: int | None = None
    fixed_strings: bool = True
    ignore_case: bool = False

    @model_validator(mode="after")
    def _check(self) -> "GitGrepArgs":
        # pattern/treeish 是位置参数：以 - 开头会被 git 当成选项解析。
        # 这是唯一能让不可信内容影响 argv 语义的缝，在这里堵死。
        for name, v in (("pattern", self.pattern), ("treeish", self.treeish)):
            if v.startswith("-"):
                raise ValueError(f"{name} 不得以 - 开头（会被解析为选项）：{v!r}")
        return self


class GitShowArgs(_Args):
    """``git show <sha>:<path>``——从快照读文件内容，供上下文抽取。

    不从磁盘读：磁盘上的是工作区，可能与 ``sha`` 不一致，那会让证据的行号
    对不上 locator 声称的那个 commit。
    """

    obj: str = Field(min_length=4, max_length=1024)

    @model_validator(mode="after")
    def _check(self) -> "GitShowArgs":
        if self.obj.startswith("-"):
            raise ValueError(f"obj 不得以 - 开头：{self.obj!r}")
        if ":" not in self.obj:
            raise ValueError("obj 必须是 <treeish>:<path> 形态")
        return self


class GitFetchArgs(_Args):
    """日级增量 fetch。只读身份：无 push 权限、不写仓内文件。

    ``branch`` 必须显式给：浅克隆是单分支的，不限定分支会去拉默认 refspec，
    拿到的 HEAD 可能不是本业务线配置的那个分支。
    """

    depth: int | None = None
    branch: str | None = None
    prune: bool = True
    quiet: bool = True


# --- 企微待办（wecom-cli 1.3.2 实测契约）-----------------------------------
class WecomTodoUserSearchArgs(_Args):
    """``contact users search``；精确匹配由适配器对响应二次过滤。"""

    keywords: list[str] = Field(min_length=1, max_length=10)
    search_mode: Literal["list"] = "list"


class WecomTodoCreateArgs(_Args):
    """``todo create`` 的请求体。CLI 只接受 ``--json``/``--items``。"""

    json_: str = Field(alias="json", min_length=2, max_length=65536)

    model_config = {"populate_by_name": True, "extra": "forbid"}


class WecomTodoGetArgs(_Args):
    """``todo get`` 的批量请求体（每批最多 20 条）。"""

    json_: str = Field(alias="json", min_length=2, max_length=65536)

    model_config = {"populate_by_name": True, "extra": "forbid"}


class WecomTodoUpdateArgs(_Args):
    """``todo update`` 的批量请求体，**不含删除**（裁决 #10）。"""

    json_: str = Field(alias="json", min_length=2, max_length=65536)

    model_config = {"populate_by_name": True, "extra": "forbid"}



# --- wecom-cli 业务方法：统一 ``--json`` 请求体 --------------------------------
# 请求体由工具层按强类型参数拼装；这里再兜一道：本地文件路径类键一律拒绝
# （CLI 会读服务器本地文件当附件/正文发出去），智能表格写不得带 delete。

_WECOM_LOCAL_PATH_KEYS = frozenset({"file_path", "content_path", "output", "output_dir"})


def _walk_keys(node: object) -> list[str]:
    if isinstance(node, dict):
        return [str(k) for k in node] + [k for v in node.values() for k in _walk_keys(v)]
    if isinstance(node, list):
        return [k for v in node for k in _walk_keys(v)]
    return []


class WecomBodyArgs(_Args):
    json_: str = Field(alias="json", min_length=2, max_length=65536)

    model_config = {"populate_by_name": True, "extra": "forbid"}

    def body(self) -> dict:
        import json as _json

        try:
            body = _json.loads(self.json_)
        except ValueError as exc:
            raise ValueError("请求体不是合法 JSON") from exc
        if not isinstance(body, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return body

    @model_validator(mode="after")
    def _no_local_paths(self) -> "WecomBodyArgs":
        bad = _WECOM_LOCAL_PATH_KEYS.intersection(_walk_keys(self.body()))
        if bad:
            raise ValueError(f"不允许读取本地文件：{sorted(bad)}")
        return self


class WecomRecordsWriteArgs(WecomBodyArgs):
    @model_validator(mode="after")
    def _no_delete(self) -> "WecomRecordsWriteArgs":
        if str(self.body().get("type") or "").lower() == "delete":
            raise ValueError("删除类操作不登记")
        return self



# ---------------------------------------------------------------------------
# 登记表
# ---------------------------------------------------------------------------

LARK_COMMANDS: tuple[CliCommandSpec, ...] = (
    CliCommandSpec(
        key="lark.docs.fetch",
        binary_key="lark",
        argv_prefix=("--as", "user", "docs", "+fetch"),
        args_model=LarkDocsFetchArgs,
        risk=RiskLevel.READ,
        env_keys=("LARK_CLI_TOKEN",),
    ),
    CliCommandSpec(
        key="lark.drive.metas",
        binary_key="lark",
        argv_prefix=("--as", "user", "drive", "metas", "batch_query"),
        args_model=LarkDriveMetasArgs,
        risk=RiskLevel.READ,
        env_keys=("LARK_CLI_TOKEN",),
    ),
    CliCommandSpec(
        key="lark.wiki.nodes",
        binary_key="lark",
        argv_prefix=("--as", "user", "wiki", "nodes", "list"),
        args_model=LarkWikiNodesArgs,
        risk=RiskLevel.READ,
        env_keys=("LARK_CLI_TOKEN",),
    ),
    CliCommandSpec(
        key="lark.docs.update",
        binary_key="lark",
        argv_prefix=("--as", "user", "docs", "+update"),
        args_model=LarkDocsUpdateArgs,
        # 写他人文档：最高非破坏档。今日 Auto Mode 全通，但分档已记账。
        risk=RiskLevel.HIGH_WRITE,
        env_keys=("LARK_CLI_TOKEN",),
        # 乐观锁冲突只重试一次，交由上层重算 diff
        max_attempts=1,
    ),
    CliCommandSpec(
        key="lark.docs.create",
        binary_key="lark",
        argv_prefix=("--as", "user", "docs", "+create"),
        args_model=LarkDocsCreateArgs,
        risk=RiskLevel.HIGH_WRITE,
        env_keys=("LARK_CLI_TOKEN",),
    ),
    CliCommandSpec(
        key="lark.drive.comment_add",
        binary_key="lark",
        argv_prefix=("--as", "user", "drive", "+add-comment"),
        args_model=LarkDriveCommentAddArgs,
        # 在他人文档上加评论：不改内容，但对外可见 → LOW_WRITE
        risk=RiskLevel.LOW_WRITE,
        env_keys=("LARK_CLI_TOKEN",),
        max_attempts=1,  # 无幂等键，重试 = 重复评论
    ),
    CliCommandSpec(
        key="lark.docs.search",
        binary_key="lark",
        argv_prefix=("--as", "user", "docs", "+search"),
        args_model=LarkDocsSearchArgs,
        risk=RiskLevel.READ,
        env_keys=("LARK_CLI_TOKEN",),
    ),
)

def _lark(
    key: str, argv: tuple[str, ...], model: type[BaseModel],
    risk: RiskLevel = RiskLevel.READ, **kw: object,
) -> CliCommandSpec:
    return CliCommandSpec(
        key=key, binary_key="lark", argv_prefix=("--as", "user", *argv),
        args_model=model, risk=risk, env_keys=("LARK_CLI_TOKEN",), **kw,  # type: ignore[arg-type]
    )


# 协作域（表格 / 多维表格 / 任务 / 日历 / 审批只读 / 知识库 / 通讯录）。
# 写命令 max_attempts=1：均无服务端幂等（task +create 的 idempotency_key 由调用方给）。
LARK_OFFICE_COMMANDS: tuple[CliCommandSpec, ...] = (
    _lark("lark.sheets.csv_get", ("sheets", "+csv-get"), LarkSheetsCsvGetArgs),
    _lark("lark.sheets.cells_search", ("sheets", "+cells-search"), LarkSheetsCellsSearchArgs),
    _lark("lark.sheets.csv_put", ("sheets", "+csv-put"), LarkSheetsCsvPutArgs,
          RiskLevel.HIGH_WRITE, max_attempts=1),
    _lark("lark.base.table_list", ("base", "+table-list"), LarkBaseTableListArgs),
    _lark("lark.base.field_list", ("base", "+field-list"), LarkBaseFieldListArgs),
    _lark("lark.base.record_list", ("base", "+record-list"), LarkBaseRecordListArgs),
    _lark("lark.base.record_upsert", ("base", "+record-upsert"), LarkBaseRecordUpsertArgs,
          RiskLevel.HIGH_WRITE, max_attempts=1),
    _lark("lark.contact.search", ("contact", "+search-user"), LarkContactSearchArgs),
    _lark("lark.task.create", ("task", "+create"), LarkTaskCreateArgs,
          RiskLevel.LOW_WRITE, max_attempts=1),
    # 只看未完成：cobra 的 bool flag 只能写成 --complete=false，固定在前缀里
    _lark("lark.task.my_open", ("task", "+get-my-tasks", "--complete=false"), LarkTaskMyArgs),
    _lark("lark.task.search", ("task", "+search"), LarkTaskSearchArgs),
    _lark("lark.task.complete", ("task", "+complete"), LarkTaskCompleteArgs,
          RiskLevel.LOW_WRITE, max_attempts=1),
    _lark("lark.calendar.agenda", ("calendar", "+agenda"), LarkCalendarAgendaArgs),
    _lark("lark.calendar.freebusy", ("calendar", "+freebusy"), LarkCalendarFreebusyArgs),
    _lark("lark.calendar.search", ("calendar", "+search-event"), LarkCalendarSearchArgs),
    _lark("lark.calendar.create", ("calendar", "+create"), LarkCalendarCreateArgs,
          RiskLevel.LOW_WRITE, max_attempts=1),
    _lark("lark.approval.tasks", ("approval", "tasks", "query"), LarkApprovalTasksArgs),
    _lark("lark.approval.instance", ("approval", "instances", "get"), LarkApprovalInstanceArgs),
    _lark("lark.wiki.node_list", ("wiki", "+node-list"), LarkWikiNodeListArgs),
    _lark("lark.wiki.node_create", ("wiki", "+node-create"), LarkWikiNodeCreateArgs,
          RiskLevel.HIGH_WRITE, max_attempts=1),
)


_TAPD_ENV = ("TAPD_API_ENDPOINT", "TAPD_TOKEN", "TAPD_NPC_ROLE")


def _tapd(
    key: str,
    argv: tuple[str, ...],
    model: type[BaseModel],
    risk: RiskLevel = RiskLevel.READ,
    **kw: object,
) -> CliCommandSpec:
    return CliCommandSpec(
        key=key,
        binary_key="tapd",
        argv_prefix=argv,
        args_model=model,
        risk=risk,
        env_keys=_TAPD_ENV,
        arg_style="kv",
        **kw,  # type: ignore[arg-type]
    )


# 写命令均 max_attempts=1：TAPD 写接口无幂等键，重试即重复建单 / 重复评论。
TAPD_COMMANDS: tuple[CliCommandSpec, ...] = (
    _tapd("tapd.user.info", ("user", "info"), TapdUserInfoArgs),
    _tapd("tapd.user.members", ("user", "members"), TapdUserMembersArgs),
    _tapd("tapd.story.fields", ("story", "fields"), TapdStoryFieldsArgs),
    _tapd("tapd.story.list", ("story", "list"), TapdStoryListArgs, timeout_s=60.0),
    _tapd("tapd.story.changes", ("story", "changes"), TapdStoryChangesArgs),
    _tapd("tapd.story.related_bugs", ("story", "related-bugs"), TapdStoryRelatedBugsArgs),
    _tapd("tapd.story.add", ("story", "add"), TapdStoryAddArgs,
          RiskLevel.HIGH_WRITE, max_attempts=1),
    _tapd("tapd.story.update", ("story", "update"), TapdStoryUpdateArgs,
          RiskLevel.LOW_WRITE, max_attempts=1),
    _tapd("tapd.bug.fields", ("bug", "fields"), TapdBugFieldsArgs),
    _tapd("tapd.bug.list", ("bug", "list"), TapdBugListArgs, timeout_s=60.0),
    _tapd("tapd.bug.add", ("bug", "add"), TapdBugAddArgs,
          RiskLevel.HIGH_WRITE, max_attempts=1),
    _tapd("tapd.bug.update", ("bug", "update"), TapdBugUpdateArgs,
          RiskLevel.LOW_WRITE, max_attempts=1),
    _tapd("tapd.task.list", ("task", "list"), TapdTaskListArgs, timeout_s=60.0),
    _tapd("tapd.task.add", ("task", "add"), TapdTaskAddArgs,
          RiskLevel.HIGH_WRITE, max_attempts=1),
    _tapd("tapd.task.update", ("task", "update"), TapdTaskUpdateArgs,
          RiskLevel.LOW_WRITE, max_attempts=1),
    _tapd("tapd.comment.list", ("comment", "list"), TapdCommentListArgs),
    _tapd("tapd.comment.add", ("comment", "add"), TapdCommentAddArgs,
          RiskLevel.LOW_WRITE, max_attempts=1),
    _tapd("tapd.iteration.list", ("iteration", "list"), TapdIterationListArgs),
)

REPO_COMMANDS: tuple[CliCommandSpec, ...] = (
    CliCommandSpec(
        key="rg.search",
        binary_key="rg",
        argv_prefix=("--json",),
        args_model=RgSearchArgs,
        positional_fields=("path",),
        # rg: 0=有命中, 1=无命中（正常空结果）, 2=真故障
        ok_exit_codes=(0, 1),
        risk=RiskLevel.READ,
        json_output=False,  # --json 是 JSON Lines，逐行解析，不是单个 JSON
        timeout_s=20.0,
        max_output_bytes=16 * 1024 * 1024,
    ),
    CliCommandSpec(
        key="git.rev_parse",
        binary_key="git",
        argv_prefix=("rev-parse",),
        args_model=GitRevParseArgs,
        positional_fields=("rev",),
        risk=RiskLevel.READ,
        json_output=False,
    ),
    CliCommandSpec(
        key="git.grep",
        binary_key="git",
        # --full-name：路径相对仓库根，省掉调用方拼接（拼错就是 locator 指错文件）
        argv_prefix=("grep", "--line-number", "--no-color", "--full-name", "-I"),
        args_model=GitGrepArgs,
        positional_fields=("pattern", "treeish"),
        # git grep: 0=有命中, 1=无命中（正常空结果）, ≥2=真故障
        ok_exit_codes=(0, 1),
        risk=RiskLevel.READ,
        json_output=False,
        timeout_s=30.0,
        max_output_bytes=16 * 1024 * 1024,
    ),
    CliCommandSpec(
        key="git.show",
        binary_key="git",
        argv_prefix=("show",),
        args_model=GitShowArgs,
        positional_fields=("obj",),
        risk=RiskLevel.READ,
        json_output=False,
        timeout_s=15.0,
        max_output_bytes=4 * 1024 * 1024,
    ),
    CliCommandSpec(
        key="git.fetch",
        binary_key="git",
        argv_prefix=("fetch",),
        args_model=GitFetchArgs,
        # 只读仓：fetch 不改工作区，但会写 .git，算低风险写
        risk=RiskLevel.LOW_WRITE,
        json_output=False,
        timeout_s=120.0,
    ),
)

WECOM_COMMANDS: tuple[CliCommandSpec, ...] = (
    CliCommandSpec(
        key="wecom.todo.user_search",
        binary_key="wecom",
        argv_prefix=("contact", "users", "search"),
        args_model=WecomTodoUserSearchArgs,
        risk=RiskLevel.READ,
        env_keys=("WECOM_CLI_TOKEN",),
    ),
    CliCommandSpec(
        key="wecom.todo.create",
        binary_key="wecom",
        argv_prefix=("todo", "create"),
        args_model=WecomTodoCreateArgs,
        # 建待办会给真人发通知，撤不回来。分档与 TAPD 建卡同级。
        risk=RiskLevel.HIGH_WRITE,
        env_keys=("WECOM_CLI_TOKEN",),
        # 企微待办没有幂等键，重试就是第二条待办（同 R10）。
        # 幂等由调用方的 claim/settle 负责，不在这里靠重试补。
        max_attempts=1,
    ),
    CliCommandSpec(
        key="wecom.todo.get",
        binary_key="wecom",
        argv_prefix=("todo", "get"),
        args_model=WecomTodoGetArgs,
        risk=RiskLevel.READ,
        env_keys=("WECOM_CLI_TOKEN",),
    ),
    CliCommandSpec(
        key="wecom.todo.update",
        binary_key="wecom",
        argv_prefix=("todo", "update"),
        args_model=WecomTodoUpdateArgs,
        risk=RiskLevel.LOW_WRITE,
        env_keys=("WECOM_CLI_TOKEN",),
        max_attempts=1,
    ),
)

def _wecom(
    key: str, argv: tuple[str, ...], risk: RiskLevel = RiskLevel.READ,
    model: type[BaseModel] = WecomBodyArgs, **kw: object,
) -> CliCommandSpec:
    return CliCommandSpec(
        key=key, binary_key="wecom", argv_prefix=argv, args_model=model,
        risk=risk, env_keys=("WECOM_CLI_TOKEN",), **kw,  # type: ignore[arg-type]
    )


# 协作域。发消息 / 发邮件对外可见且撤不回 → HIGH_WRITE；写操作一律不重试。
WECOM_OFFICE_COMMANDS: tuple[CliCommandSpec, ...] = (
    _wecom("wecom.contact.search", ("contact", "users", "search")),
    _wecom("wecom.chat.groups", ("chat", "groups", "list")),
    _wecom("wecom.message.send", ("message", "send"), RiskLevel.HIGH_WRITE, max_attempts=1),
    _wecom("wecom.calendar.list", ("calendar", "schedules", "list")),
    _wecom("wecom.calendar.get", ("calendar", "schedules", "get")),
    _wecom("wecom.calendar.create", ("calendar", "schedules", "create"),
           RiskLevel.LOW_WRITE, max_attempts=1),
    _wecom("wecom.meeting.list", ("meeting", "list")),
    _wecom("wecom.meeting.get", ("meeting", "get")),
    _wecom("wecom.meeting.create", ("meeting", "create"), RiskLevel.LOW_WRITE, max_attempts=1),
    _wecom("wecom.doc.search", ("doc", "search")),
    _wecom("wecom.doc.get", ("doc", "contents", "get")),
    _wecom("wecom.sheet.get", ("sheet", "get")),
    _wecom("wecom.sheet.range_get", ("sheet", "ranges", "get")),
    _wecom("wecom.sheet.rows_append", ("sheet", "rows", "append"),
           RiskLevel.HIGH_WRITE, max_attempts=1),
    _wecom("wecom.smartsheet.get", ("smartsheet", "get")),
    _wecom("wecom.smartsheet.records_list", ("smartsheet", "records", "list")),
    _wecom("wecom.smartsheet.records_add", ("smartsheet", "records", "add"),
           RiskLevel.HIGH_WRITE, WecomRecordsWriteArgs, max_attempts=1),
    _wecom("wecom.smartsheet.records_update", ("smartsheet", "records", "update"),
           RiskLevel.HIGH_WRITE, WecomRecordsWriteArgs, max_attempts=1),
    _wecom("wecom.disk.search", ("disk", "files", "search")),
    _wecom("wecom.mail.search", ("mail", "search")),
    _wecom("wecom.mail.get", ("mail", "get")),
    _wecom("wecom.mail.send", ("mail", "send"), RiskLevel.HIGH_WRITE, max_attempts=1),
)


ALL_COMMANDS: tuple[CliCommandSpec, ...] = (
    LARK_COMMANDS + LARK_OFFICE_COMMANDS + TAPD_COMMANDS + REPO_COMMANDS
    + WECOM_COMMANDS + WECOM_OFFICE_COMMANDS
)

# 明确记账：**没有任何命令**使用 DESTRUCTIVE 档。删除类不可达。
_DESTRUCTIVE = [s.key for s in ALL_COMMANDS if s.risk is RiskLevel.DESTRUCTIVE]
assert not _DESTRUCTIVE, f"删除类命令不得登记：{_DESTRUCTIVE}"


__all__ = [
    "ALL_COMMANDS",
    "LARK_COMMANDS",
    "LARK_OFFICE_COMMANDS",
    "REPO_COMMANDS",
    "TAPD_COMMANDS",
    "WECOM_COMMANDS",
    "WECOM_OFFICE_COMMANDS",
    "LarkDocsFetchArgs",
    "LarkDocsUpdateArgs",
    "LarkDriveMetasArgs",
    "LarkWikiNodesArgs",
    "RgSearchArgs",
    "TapdStoryListArgs",
    "WecomTodoCreateArgs",
    "WecomTodoGetArgs",
    "WecomTodoUpdateArgs",
    "WecomTodoUserSearchArgs",
]
