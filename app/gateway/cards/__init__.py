"""卡片构造与绑定。"""

from app.gateway.cards.binding import CardBinding, CardClaimResult, new_card_task_id
from app.gateway.cards.builder import (
    BUTTON_LIMIT,
    HORIZONTAL_CONTENT_LIMIT,
    TASK_ID_MAX_BYTES,
    ButtonStyle,
    CardButton,
    CardItem,
    build_alert_card,
    build_confirm_card,
    build_markdown,
    build_processing_card,
    require_valid_task_id,
)

__all__ = [
    "BUTTON_LIMIT",
    "ButtonStyle",
    "CardBinding",
    "CardButton",
    "CardClaimResult",
    "CardItem",
    "HORIZONTAL_CONTENT_LIMIT",
    "TASK_ID_MAX_BYTES",
    "build_alert_card",
    "build_confirm_card",
    "build_markdown",
    "build_processing_card",
    "new_card_task_id",
    "require_valid_task_id",
]
