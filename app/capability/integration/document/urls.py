"""飞书文档链接拼装。租户域名走 ``LARK_WEB_BASE``（如 ``https://example.feishu.cn``）。"""

from __future__ import annotations

import config

DEFAULT_WEB_BASE = "https://feishu.cn"


def web_base() -> str:
    return str(config.get("LARK_WEB_BASE", DEFAULT_WEB_BASE)).rstrip("/")


def docx_url(document_id: str) -> str:
    return f"{web_base()}/docx/{document_id}"


def wiki_url(node_token: str) -> str:
    return f"{web_base()}/wiki/{node_token}"
