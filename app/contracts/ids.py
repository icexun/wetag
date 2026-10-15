"""标识类型别名：WorkspaceId / ChatId / TopicId / TaskId（§4.1 术语裁决）。

把标识做成命名类型的目的不是运行时检查（``NewType`` 在运行时就是原类型），
而是让新写的代码在取名时自然对齐 §4.1 的裁决结果：

废弃写法          → 本仓唯一写法
``biz_line``        → ``workspace_id``
``scopeId``         → ``workspace_id``
``conversationId``  → ``chat_id``
``chatGroupId``     → ``chat_id``
``sessionId``       → ``topic_id``
``session_*``       → ``topic_*``

``session`` 一词不得出现在代码标识符与表名中。
"""

from __future__ import annotations

from enum import Enum
from typing import NewType
from uuid import UUID

# 归属标识（§4.2 SpaceContext 的构成）
OrganizationId = NewType("OrganizationId", str)
WorkspaceId = NewType("WorkspaceId", str)
BotId = NewType("BotId", str)
ChatId = NewType("ChatId", str)
TopicId = NewType("TopicId", str)
UserId = NewType("UserId", str)

# 执行标识
# TaskId 取 uuid 而非自增整数：0002 的 task.id 是 uuid PRIMARY KEY，由 Runtime
# 在入队时生成，跨进程不依赖库端序列。
TaskId = NewType("TaskId", UUID)
TraceId = NewType("TraceId", str)
IdemKey = NewType("IdemKey", str)

# 业务对象标识
# requirement_id 是 uuid（见 0005 的 DEFAULT gen_random_uuid()），不是自增整数：
# 它由库端生成一次后永不变，关联表等都引用它。asyncpg 取出来是
# UUID 对象，仓储层统一 str() 后包成本类型。
RequirementId = NewType("RequirementId", str)
DocumentId = NewType("DocumentId", str)
TodoId = NewType("TodoId", str)


class ChatType(str, Enum):
    """``ChatId`` 指向的会话形态。

    企微 ``chattype`` 字段与 ``aibot_send_msg`` 的 ``chat_type`` 码之间的
    映射属协议细节，归 Channel（``app/gateway/inbound.py`` 与
    ``app/gateway/outbound.py``），不写进契约。
    """

    SINGLE = "SINGLE"
    GROUP = "GROUP"


__all__ = [
    "BotId",
    "ChatId",
    "ChatType",
    "DocumentId",
    "IdemKey",
    "OrganizationId",
    "RequirementId",
    "TaskId",
    "TodoId",
    "TopicId",
    "TraceId",
    "UserId",
    "WorkspaceId",
]
