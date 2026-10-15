"""TAPD 领域归一层：与命令调用无关的那一半。

``tapd_cli.py`` 只管"让 tapd-cli 吐出字节"；拿到之后的判断都在这里：状态怎么归
三类、自定义字段怎么落到语义槽位、「文档交付链接」怎么变成 ``DocumentRef``、
哪种响应算"认不出来"。

所有"认不出来"的分支一律抛 ``PortUnavailable``，绝不返回空值。下游语义是固定的：
需求列表空 → 台账被当成"没有进行中的需求" → 变更通知静默停摆。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

from app.contracts.errors import PortUnavailable
from app.contracts.ids import DocumentId, RequirementId, WorkspaceId
from app.contracts.ports import (
    DocumentRef,
    ProjectConfig,
    Requirement,
    StatusCategory,
)

logger = logging.getLogger(__name__)

# 未落库的需求用它占位。DB 的 requirement_id 由库端 gen_random_uuid() 生成，
# TAPD 侧根本不知道这个值——Port 返回的对象必须能明确表达"还没有内部 id"，
# 否则下游会拿一个空串当 uuid 去查询，报错还算好的，静默查不到才要命。
UNASSIGNED = RequirementId("")

# --- 状态归类 ---------------------------------------------------------------
# doc2 §5.4 要求 37 个状态"动态拉取并归类，不硬编码"。动态拉取的是**状态清单**
# （``tapd-cli story fields``），归类靠下面的标记词 + 可覆盖表：项目随时
# 能加一个新状态，枚举一份 37 行的清单等于把下一次加状态变成一次改代码。
SUSPENDED_MARKERS = ("挂起", "暂停", "搁置", "冻结", "暂缓", "待定")
TERMINAL_MARKERS = (
    "关闭", "拒绝", "取消", "驳回", "废弃", "归档", "完成", "发布", "上线", "结束",
    "终止", "中止",
)
# 否定前缀：「未上线」「待发布」「非终态」含终态标记词但恰恰不是终态。
NEGATORS = ("未", "待", "非", "不", "无需")

# doc2 §5.4 的自定义字段映射。键是 TAPD 里的字段**中文名**，值是本仓的语义槽位。
# 字段被改名时改这张表（或用 field_labels 覆盖），不改解析代码。
DEFAULT_FIELD_LABELS: dict[str, str] = {
    "文档交付链接": "doc_ref",
    "需求涉及的端": "clients",
    "代码信息": "code_hints",
    "需求分类": "category",
    "负责人": "owners",
}

_URL_RE = re.compile(r"https?://[^\s,;、，]+")
# 飞书文档 token：/docx/ 或 /wiki/ 后面那一段
_DOC_TOKEN_RE = re.compile(r"/(?:docx|wiki|docs)/([A-Za-z0-9]{10,})")


# --- 凭据脱敏 ---------------------------------------------------------------


def mask(text: str) -> str:
    """掩码 token / secret。日志与异常文本的最后一道闸。"""
    text = re.sub(r"(token=)[^&\s]*", r"\1***", text)
    return re.sub(
        r"(?i)\b(secret|password|authorization|cookie)\b\s*[:=]\s*\S+",
        r"\1=***",
        text,
    )


class _RedactTokenFilter(logging.Filter):
    """把 ``token=...`` 从**别人的**日志里抹掉。

    实测两个库都会把带 token 的 URL 写进日志：``httpx`` 在 INFO 打印请求行（URL
    是 ``httpx.URL`` **对象**，不是 str），``mcp.client.streamable_http`` 在 DEBUG
    打印 ``Connecting to StreamableHTTP endpoint: <url>``。一次正常调用就足以把凭据
    写进终端和日志文件。

    不采用"把这些库调到 WARNING"的做法：那只是让泄露变得不常见（错误路径照样打
    URL），还顺手丢掉了排障需要的请求日志。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        _scrub_record(record)
        return True


def _scrub_arg(value: Any) -> Any:
    if isinstance(value, str):
        return mask(value) if "token=" in value else value
    # httpx 把 URL 作为对象传进 args，只判 isinstance(str) 会整条漏过去。
    # 只对 URL 类走 str()，避免给每条日志的每个参数都付一次字符串化代价。
    if type(value).__name__ in ("URL", "Url", "SplitResult", "ParseResult"):
        text = str(value)
        return mask(text) if "token=" in text else value
    return value


def _scrub_record(record: logging.LogRecord) -> None:
    if isinstance(record.msg, str) and "token=" in record.msg:
        record.msg = mask(record.msg)
    if record.args:
        if isinstance(record.args, tuple):
            record.args = tuple(_scrub_arg(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _scrub_arg(v) for k, v in record.args.items()}


_REDACT_FILTER = _RedactTokenFilter()
_REDACTED_LOGGERS = ("httpx", "httpcore", "mcp", "anyio", "urllib3")
_FACTORY_INSTALLED = False


def install_token_redaction() -> None:
    """给日志装上 token 掩码。幂等，可重复调用。

    **两层都装**，因为单装任何一层都不够：

    * ``LogRecord`` 工厂：logger 上的 filter 只作用于**直接**由它产生的记录，
      对子 logger 传播上来的记录无效。泄露那条正是 ``mcp.client.streamable_http``
      打的，而过滤器挂在 ``mcp`` 上——差一级，一条都没拦住。工厂是唯一与
      logger 层级无关的挂载点。
    * 库 logger 上的 filter：工厂只对 URL 类参数做字符串化（省开销），过滤器在
      已知的这几个库上做更彻底的检查。
    """
    global _FACTORY_INSTALLED
    if not _FACTORY_INSTALLED:
        old_factory = logging.getLogRecordFactory()

        def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            record = old_factory(*args, **kwargs)
            _scrub_record(record)
            return record

        logging.setLogRecordFactory(factory)
        _FACTORY_INSTALLED = True
    for name in _REDACTED_LOGGERS:
        lg = logging.getLogger(name)
        if not any(isinstance(f, _RedactTokenFilter) for f in lg.filters):
            lg.addFilter(_REDACT_FILTER)


# --- 信封解析 ---------------------------------------------------------------
# tapd-cli 原样输出 REST 信封 {"status":1,"data":[{"Story":{...}}]}；也兼容外加一层
# {"code":200,"msg":...,"data":...} 的网关形态。逐层剥壳，**认不出就抛**。
_LIST_KEYS = ("data", "items", "list", "stories", "result", "records", "rows")
_WRAPPER_KEYS = (
    "Story", "story", "Bug", "bug", "Task", "task", "WorkitemChange", "Comment",
)

_OK_CODES = ("200", "0", "1", "true", "True")
_RETRYABLE_CODES = ("429", "500", "502", "503", "504")


def check_api_error(node: Mapping[str, Any], what: str, *, source: str) -> None:
    """检查业务状态码。两层信封都要看。

    实测网关信封是 ``{"code":200,"msg":"服务运行成功","data":{...}}``，失败形如
    ``{"code":401,"msg":"没有项目权限","data":null}``；被包在里面的 TAPD REST
    原生信封则是 ``{"status":1,"info":...}``。

    **``status`` 只在它是标量时才当状态码看。** ``getProjectConfig`` 的 ``data``
    里 ``status`` 是"需求状态映射"那张表（实测是 dict），把它当状态码会把一个
    完全正常的配置判成 API 错误，然后整条索引刷新以"TAPD 不可用"收场——一个因为
    字段重名而自我致盲的 bug。
    """
    code = node.get("code")
    if code is not None and not isinstance(code, (dict, list)):
        if str(code) not in _OK_CODES:
            msg = mask(str(node.get("msg") or node.get("message") or ""))[:200]
            raise PortUnavailable(
                source,
                # 401/403 是凭据或授权问题，重试一万次也不会变好，且属于安全事件
                "AUTH_FAILED" if str(code) in ("401", "403") else "API_ERROR",
                f"{what} 返回 code={code} msg={msg}",
                retryable=str(code) in _RETRYABLE_CODES,
            )
    status = node.get("status")
    if status is None or isinstance(status, (dict, list)):
        return
    if str(status) in _OK_CODES:
        return
    raise PortUnavailable(
        source,
        "API_ERROR",
        f"{what} 返回 status={status} info={mask(str(node.get('info') or ''))[:200]}",
    )


def rows_of(payload: Any, *, what: str, source: str) -> list[dict[str, Any]]:
    """从各种信封里取出行列表。认不出结构时抛 ``PortUnavailable``。"""
    node: Any = payload
    for _ in range(4):  # 最多剥四层，防畸形数据无限绕
        if isinstance(node, list):
            return [unwrap_row(x) for x in node if isinstance(x, dict)]
        if not isinstance(node, dict):
            break
        check_api_error(node, what, source=source)
        nxt = next((node[k] for k in _LIST_KEYS if k in node), None)
        if nxt is None:
            # 单对象也算一行（getTapdStory 就是这形态）
            if any(k in node for k in _WRAPPER_KEYS):
                return [unwrap_row(node)]
            break
        node = nxt
    raise PortUnavailable(
        source,
        "SCHEMA_UNKNOWN",
        f"{what} 的返回结构无法识别，不敢当成空结果处理："
        f"{mask(json.dumps(payload, ensure_ascii=False, default=str))[:240]}",
    )


def unwrap_row(row: Any) -> dict[str, Any]:
    """剥掉 ``{"Story": {...}}`` 这层包装。"""
    if not isinstance(row, dict):
        return {}
    for key in _WRAPPER_KEYS:
        inner = row.get(key)
        if isinstance(inner, dict):
            return inner
    return row


def object_of(payload: Any, *, what: str, source: str) -> dict[str, Any]:
    """取单个对象。``getProjectConfig`` 这类返回用它。"""
    node = payload
    for _ in range(4):
        if isinstance(node, list):
            node = node[0] if node else None
            continue
        if not isinstance(node, dict):
            break
        check_api_error(node, what, source=source)
        if "data" in node and isinstance(node["data"], (dict, list)):
            node = node["data"]
            continue
        return unwrap_row(node) or node
    raise PortUnavailable(source, "SCHEMA_UNKNOWN", f"{what} 的返回结构无法识别")


# --- 状态分类器 -------------------------------------------------------------


class StatusClassifier:
    """状态显示名 → ``StatusCategory``。"""

    __slots__ = ("_overrides",)

    def __init__(
        self,
        *,
        terminal_overrides: Sequence[str] = (),
        suspended_overrides: Sequence[str] = (),
        active_overrides: Sequence[str] = (),
    ) -> None:
        self._overrides: dict[str, StatusCategory] = {}
        for name in terminal_overrides:
            self._overrides[name.strip()] = StatusCategory.TERMINAL
        for name in suspended_overrides:
            self._overrides[name.strip()] = StatusCategory.SUSPENDED
        for name in active_overrides:
            self._overrides[name.strip()] = StatusCategory.ACTIVE

    def categorize(self, status_name: str) -> StatusCategory:
        """单个状态名 → 三类。

        判定顺序（挂起 → 否定前缀 → 终态）不是随意排的：

        * 挂起先判，因为「待定」既含否定前缀「待」又确实是挂起态；
        * 否定前缀在终态之前，因为「未上线」「待发布」含终态标记词却恰恰不是终态；
        * 都不命中时归 ACTIVE。

        最后这条是刻意的不对称：把进行中的需求误判成终态，它就被踢出"进行中"
        窗口，它的文档不再被监听——漏报，而且无声。反过来把终态误判成进行中，
        代价只是多监听一篇文档。前者不可接受，后者可接受。
        """
        name = (status_name or "").strip()
        if not name:
            return StatusCategory.ACTIVE
        if name in self._overrides:  # 运维可在不改代码的前提下纠正
            return self._overrides[name]
        if any(m in name for m in SUSPENDED_MARKERS):
            return StatusCategory.SUSPENDED
        if any(name.startswith(n) for n in NEGATORS):
            return StatusCategory.ACTIVE
        if any(m in name for m in TERMINAL_MARKERS):
            return StatusCategory.TERMINAL
        return StatusCategory.ACTIVE


# --- 项目配置解析 -----------------------------------------------------------


def project_id_int(project_id: str | int, *, source: str) -> int:
    """TAPD 项目 id。多数接口的 schema 要 integer。"""
    try:
        return int(str(project_id).strip())
    except (TypeError, ValueError) as exc:
        raise PortUnavailable(
            source, "CONFIG_INVALID", f"tapd_project_id 不是整数：{project_id!r}"
        ) from exc


def status_names(raw: Mapping[str, Any]) -> tuple[str, ...]:
    """从项目配置里挖出状态清单。

    几种见过的形态都试：``{"status_map":{"key":"显示名"}}``、
    ``{"statuses":[{"name":...}]}``、以及嵌在 ``story`` 下的同名键。
    """
    # 实测 getProjectConfig 的 data 形如 {"status":{...},"story_field":{...},
    # "story_category":{...},"story_type":{...}}，所以 "status" 是**第一候选**。
    candidates = (
        "status", "status_map", "statusMap", "story_status_map", "statuses",
        "story_statuses",
    )
    nodes: list[Any] = [raw]
    for key in ("story", "stories", "requirement", "config", "data"):
        inner = raw.get(key)
        if isinstance(inner, dict):
            nodes.append(inner)
    names: list[str] = []
    for node in nodes:
        for key in candidates:
            val = node.get(key) if isinstance(node, dict) else None
            if isinstance(val, dict):
                # {内部键: 显示名} —— 归类看显示名
                names.extend(str(v).strip() for v in val.values() if str(v).strip())
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, str) and item.strip():
                        names.append(item.strip())
                    elif isinstance(item, dict):
                        got = item.get("name") or item.get("label") or item.get("value")
                        if got:
                            names.append(str(got).strip())
        if names:
            break
    # 去重保序
    seen: set[str] = set()
    return tuple(n for n in names if not (n in seen or seen.add(n)))


def custom_field_map(
    raw: Mapping[str, Any], labels: Mapping[str, str]
) -> dict[str, str]:
    """``{TAPD 字段键: 本仓语义槽位}``。

    配置里给的是「字段键 ↔ 中文名」，我们要的是「字段键 ↔ 语义槽位」，中间靠
    ``labels`` 这张可覆盖的对照表。中文名对不上就不映射——猜一个槽位比缺一个
    槽位危险得多（把"外部备注"当成"文档交付链接"会造出指向不存在文档的 doc_ref）。
    """
    out: dict[str, str] = {}
    for node in iter_field_defs(raw):
        key = str(
            node.get("custom_field") or node.get("field") or node.get("id") or ""
        ).strip()
        name = str(node.get("name") or node.get("label") or "").strip()
        if not key or not name:
            continue
        slot = labels.get(name)
        if slot:
            out[key] = slot
    return out


def iter_field_defs(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    # 实测键名是 story_field（与 task_field / bug_field 并列）——我们只要需求的那份。
    keys = (
        "story_field", "custom_fields", "customFields", "story_custom_fields",
        "custom_field_map", "fields",
    )
    nodes: list[Any] = [raw]
    for k in ("story", "stories", "requirement", "config", "data"):
        inner = raw.get(k)
        if isinstance(inner, dict):
            nodes.append(inner)
    out: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        for key in keys:
            val = node.get(key)
            if isinstance(val, list):
                out.extend(x for x in val if isinstance(x, dict))
            elif isinstance(val, dict):
                for fk, fv in val.items():
                    if isinstance(fv, dict):
                        out.append({"custom_field": fk, **fv})
                    elif isinstance(fv, str):
                        out.append({"custom_field": fk, "name": fv})
    return out


def category_names(raw: Mapping[str, Any]) -> tuple[str, ...]:
    for key in ("story_category", "category_map", "categories", "story_categories"):
        val = raw.get(key)
        if isinstance(val, dict):
            return tuple(str(v).strip() for v in val.values() if str(v).strip())
        if isinstance(val, list):
            out = []
            for item in val:
                if isinstance(item, str):
                    out.append(item.strip())
                elif isinstance(item, dict):
                    got = item.get("name") or item.get("label")
                    if got:
                        out.append(str(got).strip())
            return tuple(x for x in out if x)
    return ()


def fields_info_to_config(data: Mapping[str, Any]) -> dict[str, Any]:
    """``story fields`` 的 ``data`` → :func:`build_project_config` 认的原文。

    ``data`` 形如 ``{"status":{"options":{"status_2":"开发中"}}, "category_id":
    {"options":{id: 名}}, "custom_field_one":{"label":"文档交付链接"}}``。
    """
    def _options(name: str) -> dict[str, str]:
        node = data.get(name)
        opts = node.get("options") if isinstance(node, Mapping) else None
        return {str(k): str(v) for k, v in opts.items()} if isinstance(opts, Mapping) else {}

    custom = {
        key: str(node.get("label") or node.get("name") or "")
        for key, node in data.items()
        if key.startswith("custom_") and isinstance(node, Mapping)
    }
    return {
        "status": _options("status"),
        "story_field": {k: v for k, v in custom.items() if v},
        "story_category": _options("category_id"),
    }


def build_project_config(
    raw: Mapping[str, Any],
    *,
    project_id: str,
    classifier: StatusClassifier,
    labels: Mapping[str, str],
    source: str,
) -> ProjectConfig:
    """项目配置原文 → ``ProjectConfig``。

    拿不到状态清单时抛 ``PortUnavailable`` 而不是返回空配置。空 ``status_map``
    会让 ``categorize()`` 把所有状态判成 ACTIVE——那一项本身是安全方向（宁可多
    查），但空 ``custom_field_map`` 同时意味着文档链接、涉及端、代码线索全部丢失，
    需求与文档的关联断开，变更通知静默停摆。
    所以这里必须硬失败。
    """
    names = status_names(raw)
    if not names:
        raise PortUnavailable(
            source,
            "SCHEMA_UNKNOWN",
            "返回里找不到需求状态清单，拒绝用空配置继续"
            "（空配置会丢失文档链接等自定义字段）",
        )
    by_name = {name: classifier.categorize(name) for name in names}
    status_map = dict(by_name)
    # 需求行里 status 是内部键（status_2），归类表同时登记内部键 → 同一类
    keys = raw.get("status")
    if isinstance(keys, Mapping):
        for key, name in keys.items():
            status_map.setdefault(
                str(key), by_name.get(str(name).strip(), StatusCategory.ACTIVE)
            )
    fields = custom_field_map(raw, labels)
    cats = category_names(raw)
    logger.info(
        "TAPD 项目配置 project=%s 状态=%d(终态%d/挂起%d) 自定义字段=%d 分类=%d",
        project_id,
        len(by_name),
        sum(1 for v in by_name.values() if v is StatusCategory.TERMINAL),
        sum(1 for v in by_name.values() if v is StatusCategory.SUSPENDED),
        len(fields),
        len(cats),
    )
    return ProjectConfig(
        project_id=project_id,
        status_map=status_map,
        custom_field_map=fields,
        categories=cats,
    )


# --- 需求行解析 -------------------------------------------------------------


def extract_custom(
    row: Mapping[str, Any], field_map: Mapping[str, str]
) -> dict[str, Any]:
    """按语义槽位取自定义字段值（doc2 §5.4 的五项映射）。"""
    got: dict[str, Any] = {
        "doc_ref": None,
        "clients": [],
        "code_hints": [],
        "category": None,
        "owners": [],
    }
    for key, slot in field_map.items():
        value = row.get(key)
        if value in (None, ""):
            continue
        text = str(value).strip()
        if slot == "doc_ref":
            got["doc_ref"] = to_doc_ref(text)
        elif slot == "clients":
            got["clients"] = split_multi(text)
        elif slot == "code_hints":
            got["code_hints"] = split_multi(text)
        elif slot == "category":
            got["category"] = text
        elif slot == "owners":
            got["owners"] = split_multi(text)
    return got


def to_doc_ref(text: str) -> DocumentRef | None:
    """「文档交付链接」→ ``DocumentRef``。

    doc2 §5.4：**只认链接，不做标题匹配**。取不到 token 就返回 None——标题猜出来的
    关联会把两个需求的文档张冠李戴，而这类错误在证据卡上看起来和真的一样。
    """
    match = _URL_RE.search(text)
    if not match:
        return None
    url = match.group(0).rstrip("。,)）")
    token = _DOC_TOKEN_RE.search(url)
    if not token:
        return None
    return DocumentRef(document_id=DocumentId(token.group(1)), url=url)


def split_multi(value: Any) -> list[str]:
    """TAPD 多值字段是分隔符拼接的字符串（``;`` / ``,`` / ``|`` / 顿号）。"""
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    parts = re.split(r"[;,|、，\s]+", str(value))
    return [p.strip() for p in parts if p.strip()]


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def to_requirement(
    row: Mapping[str, Any],
    config: ProjectConfig,
    *,
    workspace_id: WorkspaceId,
) -> Requirement | None:
    """一行原始需求 → ``Requirement``。缺 id 或标题时返回 None（由调用方计数跳过）。"""
    tapd_id = str(row.get("id") or "").strip()
    title = str(row.get("name") or row.get("title") or "").strip()
    if not tapd_id or not title:
        return None
    # v_status 是中文显示名（list 带 with_v_status=1 时才有），否则退到内部键；
    # status_map 两种键都登记了，查不到按 ACTIVE（宁可多查）。
    status = str(row.get("v_status") or row.get("status") or "").strip()
    category = config.categorize(status)

    extracted = extract_custom(row, config.custom_field_map)
    category_name = extracted["category"] or (
        str(row.get("category_id") or "").strip() or None
    )

    return Requirement(
        requirement_id=UNASSIGNED,
        tapd_id=tapd_id,
        workspace_id=workspace_id,
        title=title,
        status=status,
        status_category=category,
        doc_ref=extracted["doc_ref"],
        code_hints=tuple(extracted["code_hints"]),
        owners=tuple(extracted["owners"] or split_multi(row.get("owner"))),
        category=category_name,
        updated_at=parse_dt(row.get("modified") or row.get("created")),
        raw=dict(row),
    )


# --- 写操作入参 -------------------------------------------------------------


def draft_to_params(draft: Any) -> dict[str, Any]:
    """``RequirementDraft`` → ``add_story`` 的 params。

    正文用 TAPD 认的 HTML 片段拼，各节标题固定，好让后续的 diff 能按节比对。
    """
    blocks: list[str] = []
    if draft.background:
        blocks.append(f"<h3>背景</h3><p>{esc(draft.background)}</p>")
    if draft.goal:
        blocks.append(f"<h3>目标</h3><p>{esc(draft.goal)}</p>")
    if draft.scope_in:
        blocks.append("<h3>范围内</h3>" + ul(draft.scope_in))
    if draft.scope_out:
        blocks.append("<h3>范围外</h3>" + ul(draft.scope_out))
    if draft.acceptance_criteria:
        blocks.append("<h3>验收标准</h3>" + ul(draft.acceptance_criteria))
    params: dict[str, Any] = {"name": draft.title.strip()}
    if blocks:
        params["description"] = "".join(blocks)
    if draft.owners:
        params["owner"] = ";".join(draft.owners)
    if draft.tapd_category:
        params["category_id"] = draft.tapd_category
    return params


def esc(text: str) -> str:
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def ul(items: Sequence[str]) -> str:
    return "<ul>" + "".join(f"<li>{esc(x)}</li>" for x in items) + "</ul>"


__all__ = [
    "DEFAULT_FIELD_LABELS",
    "NEGATORS",
    "SUSPENDED_MARKERS",
    "TERMINAL_MARKERS",
    "UNASSIGNED",
    "StatusClassifier",
    "build_project_config",
    "category_names",
    "check_api_error",
    "custom_field_map",
    "draft_to_params",
    "esc",
    "extract_custom",
    "install_token_redaction",
    "iter_field_defs",
    "mask",
    "object_of",
    "parse_dt",
    "project_id_int",
    "rows_of",
    "split_multi",
    "status_names",
    "to_doc_ref",
    "to_requirement",
    "ul",
    "unwrap_row",
]
