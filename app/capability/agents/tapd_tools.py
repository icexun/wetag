"""对话 Agent 的 TAPD 工具（官方 ``tapd-cli`` 实现）。

调用全部经 ``CliExecutor``：命令白名单（删除类不登记）、参数 schema 校验（未登记
字段直接拒绝，自定义字段只放行 ``custom_field_*``）、凭据最小注入、审计落库。

``workspace_id`` 均可省略，默认取当前业务线配置的 TAPD 项目。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from app.capability.integration.requirement.urls import entity_url
from app.platform.cli.executor import CliExecutor
from app.platform.cli.registry import TAPD_COMMANDS

_executor: Any | None = None
_project_id: str | None = None

_WRAPPERS = ("Story", "Bug", "Task", "Comment", "Iteration", "WorkitemChange", "UserWorkspace")
_LIST_FIELDS = {
    "story": "id,name,v_status,status,owner,developer,priority_label,iteration_id,"
    "category_id,parent_id,creator,created,modified",
    "bug": "id,title,v_status,status,severity,priority_label,current_owner,reporter,"
    "iteration_id,module,created,modified",
    "task": "id,name,status,owner,story_id,iteration_id,priority_label,begin,due,"
    "created,modified",
}
_TAG_RE = re.compile(r"<[^>]+>")
_MAX_TEXT = 4000


def bind_dependencies(executor: Any | None, project_id: str | None) -> None:
    """由 Container.build 接线：共用 capability 的 executor（凭据与审计一致）。"""
    global _executor, _project_id
    _executor = executor
    _project_id = project_id


def _get_executor() -> Any:
    global _executor
    if _executor is None:
        _executor = CliExecutor(
            workspace_id=os.environ.get("WORKSPACE_ID", "default"),
            binaries={"tapd": os.environ.get("TAPD_CLI_BIN", "tapd-cli")},
            secrets={
                k: os.environ[k]
                for k in ("TAPD_TOKEN", "TAPD_API_ENDPOINT", "TAPD_NPC_ROLE")
                if os.environ.get(k)
            },
            specs=TAPD_COMMANDS,
            timeout_s=60.0,
        )
    return _executor


def _pid(workspace_id: str) -> str:
    pid = (workspace_id or _project_id or os.environ.get("TAPD_WORKSPACE_IDS", "")).strip()
    return pid.split(",")[0].strip()


def _text(value: Any) -> ToolResponse:
    return ToolResponse(
        content=[TextBlock(type="text", text=json.dumps(value, ensure_ascii=False, default=str))]
    )


def _fail(result: Any) -> ToolResponse:
    return _text(
        {"ok": False, "error_code": str(result.error_code), "stderr": result.stderr_digest}
    )


def _unwrap(node: Any) -> dict[str, Any]:
    if isinstance(node, dict):
        for key in _WRAPPERS:
            if isinstance(node.get(key), dict):
                return node[key]
        return node
    return {}


def _rows(data: Any) -> list[dict[str, Any]]:
    inner = data.get("data") if isinstance(data, dict) else data
    if isinstance(inner, list):
        return [_unwrap(x) for x in inner]
    if isinstance(inner, dict):
        return [_unwrap(inner)]
    return []


def _plain(html: Any) -> str:
    text = _TAG_RE.sub(" ", str(html or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_MAX_TEXT] + ("…" if len(text) > _MAX_TEXT else "")


def _decorate(row: dict[str, Any], kind: str | None, pid: str) -> dict[str, Any]:
    out = {k: v for k, v in row.items() if v not in (None, "", "0", 0) or k == "id"}
    if "field_changes" in out:
        out.pop("changes", None)
    if "description" in out:
        out["description"] = _plain(out["description"])
    if kind:
        url = entity_url(kind, pid, str(row.get("id") or ""))
        if url:
            out["url"] = url
    return out


async def _call(
    key: str, args: dict[str, Any], *, kind: str | None = None, pid: str = ""
) -> ToolResponse:
    clean = {k: v for k, v in args.items() if v not in (None, "")}
    if not clean.get("workspaceid") and key != "tapd.user.info":
        return _text({"ok": False, "error_code": "CONFIG_MISSING", "stderr": "未配置 TAPD 项目 ID"})
    r = await _get_executor().run(key, clean)
    if not r.ok:
        return _fail(r)
    rows = [_decorate(x, kind, pid) for x in _rows(r.data)]
    return _text({"ok": True, "count": len(rows), "items": rows})


def _fields_arg(fields: dict[str, Any] | str | None) -> dict[str, Any]:
    if not fields:
        return {}
    if isinstance(fields, str):
        try:
            parsed = json.loads(fields)
        except ValueError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return dict(fields)


# --- 需求 ---------------------------------------------------------------


async def tapd_story_list(
    story_id: str = "",
    name: str = "",
    v_status: str = "",
    owner: str = "",
    iteration_id: str = "",
    limit: int = 20,
    page: int = 1,
    workspace_id: str = "",
) -> ToolResponse:
    """查询 TAPD 需求。按 story_id 查详情（含描述纯文本）；name 支持模糊；v_status 为中文状态名；owner 为处理人昵称。"""
    pid = _pid(workspace_id)
    args = {
        "workspaceid": pid, "id": story_id, "name": name, "v_status": v_status, "owner": owner,
        "iteration_id": iteration_id, "limit": limit, "page": page, "with_v_status": True,
    }
    if not story_id:
        args["fields"] = _LIST_FIELDS["story"]
    return await _call("tapd.story.list", args, kind="story", pid=pid)


async def tapd_story_create(
    name: str,
    description_html: str = "",
    owner: str = "",
    developer: str = "",
    cc: str = "",
    priority_label: str = "",
    category_id: str = "",
    iteration_id: str = "",
    parent_id: str = "",
    custom_fields: dict[str, Any] | None = None,
    workspace_id: str = "",
) -> ToolResponse:
    """创建 TAPD 需求。description_html 为 HTML；多人用 ; 分隔；自定义字段用 custom_fields={"custom_field_xxx": 值}，键先用 tapd_fields 查。"""
    pid = _pid(workspace_id)
    args = {
        **_fields_arg(custom_fields),
        "workspaceid": pid, "name": name, "description": description_html, "owner": owner,
        "developer": developer, "cc": cc, "priority_label": priority_label,
        "category_id": category_id, "iteration_id": iteration_id, "parent_id": parent_id,
    }
    return await _call("tapd.story.add", args, kind="story", pid=pid)


async def tapd_story_update(
    story_id: str, fields: dict[str, Any], workspace_id: str = ""
) -> ToolResponse:
    """更新 TAPD 需求字段。fields 如 {"v_status": "开发中", "owner": "张三;", "custom_field_one": "…"}；description 须为 HTML。返回服务端回读值，以它为准汇报。"""
    pid = _pid(workspace_id)
    args = {**_fields_arg(fields), "workspaceid": pid, "id": story_id}
    return await _call("tapd.story.update", args, kind="story", pid=pid)


async def tapd_story_changes(story_id: str, workspace_id: str = "") -> ToolResponse:
    """查询需求的变更历史（谁在何时改了哪些字段）。"""
    pid = _pid(workspace_id)
    return await _call("tapd.story.changes", {"workspaceid": pid, "story-id": story_id}, pid=pid)


async def tapd_story_related_bugs(story_id: str, workspace_id: str = "") -> ToolResponse:
    """查询需求关联的缺陷。"""
    pid = _pid(workspace_id)
    return await _call(
        "tapd.story.related_bugs", {"workspaceid": pid, "story-id": story_id}, pid=pid
    )


async def tapd_fields(entity: str = "story", workspace_id: str = "") -> ToolResponse:
    """查询需求（entity=story）或缺陷（entity=bug）的字段定义：状态、分类等候选值与 custom_field_* 的中文名。写自定义字段前必查。"""
    pid = _pid(workspace_id)
    key = "tapd.bug.fields" if entity == "bug" else "tapd.story.fields"
    if not pid:
        return _text({"ok": False, "error_code": "CONFIG_MISSING", "stderr": "未配置 TAPD 项目 ID"})
    r = await _get_executor().run(key, {"workspaceid": pid})
    if not r.ok:
        return _fail(r)
    data = r.data.get("data") if isinstance(r.data, dict) else None
    out: dict[str, Any] = {}
    for name, node in (data or {}).items():
        if not isinstance(node, dict):
            continue
        opts = node.get("options")
        if opts or name.startswith("custom_"):
            out[name] = {"label": node.get("label"), "options": opts or None}
    return _text({"ok": True, "fields": out})


# --- 缺陷 ---------------------------------------------------------------


async def tapd_bug_list(
    bug_id: str = "",
    title: str = "",
    v_status: str = "",
    severity: str = "",
    current_owner: str = "",
    iteration_id: str = "",
    limit: int = 20,
    page: int = 1,
    workspace_id: str = "",
) -> ToolResponse:
    """查询 TAPD 缺陷。按 bug_id 查详情；severity 取 fatal/serious/normal/prompt/advice。"""
    pid = _pid(workspace_id)
    args = {
        "workspaceid": pid, "id": bug_id, "title": title, "v_status": v_status,
        "severity": severity, "current_owner": current_owner,
        "iteration_id": iteration_id, "limit": limit, "page": page,
    }
    if not bug_id:
        args["fields"] = _LIST_FIELDS["bug"]
    return await _call("tapd.bug.list", args, kind="bug", pid=pid)


async def tapd_bug_create(
    title: str,
    description_html: str = "",
    severity: str = "",
    priority_label: str = "",
    current_owner: str = "",
    reporter: str = "",
    iteration_id: str = "",
    module: str = "",
    custom_fields: dict[str, Any] | None = None,
    workspace_id: str = "",
) -> ToolResponse:
    """创建 TAPD 缺陷。severity 取 fatal/serious/normal/prompt/advice；priority_label 取 urgent/high/medium/low；description_html 为 HTML。"""
    pid = _pid(workspace_id)
    args = {
        **_fields_arg(custom_fields),
        "workspaceid": pid, "title": title, "description": description_html,
        "severity": severity, "priority_label": priority_label,
        "current_owner": current_owner, "reporter": reporter,
        "iteration_id": iteration_id, "module": module,
    }
    return await _call("tapd.bug.add", args, kind="bug", pid=pid)


async def tapd_bug_update(bug_id: str, fields: dict[str, Any], workspace_id: str = "") -> ToolResponse:
    """更新 TAPD 缺陷字段，如 {"v_status": "已解决", "resolution": "fixed", "current_owner": "张三"}。"""
    pid = _pid(workspace_id)
    args = {**_fields_arg(fields), "workspaceid": pid, "id": bug_id}
    return await _call("tapd.bug.update", args, kind="bug", pid=pid)


# --- 任务 ---------------------------------------------------------------


async def tapd_task_list(
    task_id: str = "",
    name: str = "",
    status: str = "",
    owner: str = "",
    story_id: str = "",
    iteration_id: str = "",
    limit: int = 20,
    page: int = 1,
    workspace_id: str = "",
) -> ToolResponse:
    """查询 TAPD 任务。status 取 open/progressing/done；story_id 查某需求下的任务。"""
    pid = _pid(workspace_id)
    args = {
        "workspaceid": pid, "id": task_id, "name": name, "status": status, "owner": owner,
        "story_id": story_id, "iteration_id": iteration_id, "limit": limit, "page": page,
    }
    if not task_id:
        args["fields"] = _LIST_FIELDS["task"]
    return await _call("tapd.task.list", args, kind="task", pid=pid)


async def tapd_task_create(
    name: str,
    story_id: str = "",
    owner: str = "",
    description_html: str = "",
    priority_label: str = "",
    iteration_id: str = "",
    begin: str = "",
    due: str = "",
    workspace_id: str = "",
) -> ToolResponse:
    """创建 TAPD 任务。挂到需求下时传 story_id；begin/due 为 YYYY-MM-DD。"""
    pid = _pid(workspace_id)
    args = {
        "workspaceid": pid, "name": name, "story_id": story_id, "parent_id": story_id,
        "owner": owner, "description": description_html, "priority_label": priority_label,
        "iteration_id": iteration_id, "begin": begin, "due": due,
    }
    return await _call("tapd.task.add", args, kind="task", pid=pid)


async def tapd_task_update(task_id: str, fields: dict[str, Any], workspace_id: str = "") -> ToolResponse:
    """更新 TAPD 任务字段，如 {"status": "done", "owner": "张三"}。"""
    pid = _pid(workspace_id)
    args = {**_fields_arg(fields), "workspaceid": pid, "id": task_id}
    return await _call("tapd.task.update", args, kind="task", pid=pid)


# --- 评论 / 迭代 / 成员 ----------------------------------------------------


async def tapd_comment_list(
    entry_type: str, entry_id: str, limit: int = 20, workspace_id: str = ""
) -> ToolResponse:
    """查询评论。entry_type 取 stories/bug/tasks。"""
    pid = _pid(workspace_id)
    args = {
        "workspaceid": pid, "entry-type": entry_type, "entry-id": entry_id,
        "limit": limit, "order": "created desc",
    }
    return await _call("tapd.comment.list", args, pid=pid)


async def tapd_comment_add(
    entry_type: str, entry_id: str, content_markdown: str, workspace_id: str = ""
) -> ToolResponse:
    """给需求/缺陷/任务加评论。entry_type 取 stories/bug/tasks；内容用 Markdown（自动转 HTML）。"""
    pid = _pid(workspace_id)
    args = {
        "workspaceid": pid, "entry-type": entry_type, "entry-id": entry_id,
        "description": content_markdown,
    }
    return await _call("tapd.comment.add", args, pid=pid)


async def tapd_iteration_list(
    status: str = "", name: str = "", limit: int = 20, workspace_id: str = ""
) -> ToolResponse:
    """查询迭代。status 取 open/done；返回 id 供需求/缺陷/任务的 iteration_id 使用。"""
    pid = _pid(workspace_id)
    args = {
        "workspaceid": pid, "status": status, "name": name, "limit": limit,
        "order": "startdate desc",
    }
    return await _call("tapd.iteration.list", args, pid=pid)


async def tapd_member_list(user: str = "", workspace_id: str = "") -> ToolResponse:
    """查询项目成员（昵称即 owner/current_owner 的取值）。user 为昵称，多个用逗号分隔。"""
    pid = _pid(workspace_id)
    args = {"workspaceid": pid, "user": user, "fields": "user,name,email,role_id"}
    return await _call("tapd.user.members", args, pid=pid)


#: utils.ensure_tools_loaded 注册到共享 Toolkit 的全部 TAPD 工具
AGENT_TAPD_TOOLS = (
    tapd_story_list,
    tapd_story_create,
    tapd_story_update,
    tapd_story_changes,
    tapd_story_related_bugs,
    tapd_fields,
    tapd_bug_list,
    tapd_bug_create,
    tapd_bug_update,
    tapd_task_list,
    tapd_task_create,
    tapd_task_update,
    tapd_comment_list,
    tapd_comment_add,
    tapd_iteration_list,
    tapd_member_list,
)

__all__ = ["AGENT_TAPD_TOOLS", "bind_dependencies"]
