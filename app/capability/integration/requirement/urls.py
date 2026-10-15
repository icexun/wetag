"""TAPD 链接拼装。"""

from __future__ import annotations

import config

DEFAULT_WEB_BASE = "https://www.tapd.cn"

_PATHS = {
    "story": "prong/stories/view",
    "bug": "bugtrace/bugs/view",
    "task": "prong/tasks/view",
}


def web_base() -> str:
    return str(config.get("TAPD_WEB_BASE", DEFAULT_WEB_BASE)).rstrip("/")


def entity_url(kind: str, project_id: str | None, entity_id: str) -> str | None:
    path = _PATHS.get(kind)
    if not project_id or not path or not entity_id:
        return None
    return f"{web_base()}/{project_id}/{path}/{entity_id}"


def story_url(project_id: str | None, story_id: str) -> str | None:
    return entity_url("story", project_id, story_id)


__all__ = ["DEFAULT_WEB_BASE", "entity_url", "story_url", "web_base"]
