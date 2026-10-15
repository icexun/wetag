"""引用消息识别（A-7）。

引用是事项路由 L1 的命中键，也是「这句话在说哪条结论」的唯一确定信号，
因此必须在归一化阶段原样提取，不做任何语义解释。

企微在长连接文档里没有逐字段固定引用体的键名，这里按已知的两种写法取值。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class QuotedMessage:
    message_id: Optional[str] = None
    content: Optional[str] = None

    def __bool__(self) -> bool:
        return bool(self.message_id or self.content)


def extract_quote(body: Dict[str, Any]) -> QuotedMessage:
    """提取引用体。实际帧有两种形态：

    - 带 msgid 的旧形态：``{"msgid": ..., "content": ...}``
    - 长连接新形态：**不带被引用消息 msgid**，内容嵌套在类型子对象里，
      如 ``{"msgtype": "text", "text": {"content": ...}}``——此时只能靠
      内容匹配定位事项（路由 L1 的内容兜底）。
    """
    quote = body.get("quote") or {}
    content = quote.get("content")
    if content is None:
        sub = quote.get(quote.get("msgtype") or "") or {}
        content = sub.get("content")
    return QuotedMessage(
        message_id=quote.get("msgid") or quote.get("quoted_msgid"),
        content=content,
    )
