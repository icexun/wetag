"""协作工具的返回封装：JSON 文本，超长截断。"""

from __future__ import annotations

import json
from typing import Any

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from config.properties import ToolProperties


def tool_text(value: Any) -> ToolResponse:
    text = json.dumps(value, ensure_ascii=False, default=str)
    limit = int(ToolProperties.instance().office_result_max_chars or 20000)
    if len(text) > limit:
        text = (
            text[:limit]
            + f"…[结果过长，已截断 {len(text) - limit} 字符；请缩小时间范围、条数或读取区域后重试]"
        )
    return ToolResponse(content=[TextBlock(type="text", text=text)])


__all__ = ["tool_text"]
