"""回复窗口：平台时限的本地表达。"""

from __future__ import annotations

from app.gateway.reply_window import (
    EVENT_REPLY_TTL_SECONDS,
    MESSAGE_REPLY_TTL_SECONDS,
    ReplyWindow,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_frame_is_available_within_ttl():
    clock = FakeClock()
    window = ReplyWindow(clock)
    window.register("e1", {"headers": {"req_id": "r1"}}, ttl_seconds=5)

    clock.advance(4.9)

    assert window.frame_of("e1")["headers"]["req_id"] == "r1"


def test_event_window_expires_after_five_seconds():
    """卡片更新与欢迎语必须在 5 秒内发出，过期即不可用。"""
    clock = FakeClock()
    window = ReplyWindow(clock)
    window.register("e1", {}, ttl_seconds=EVENT_REPLY_TTL_SECONDS)

    clock.advance(EVENT_REPLY_TTL_SECONDS)

    assert window.frame_of("e1") is None


def test_stream_window_lasts_ten_minutes():
    clock = FakeClock()
    window = ReplyWindow(clock)
    window.register("e1", {}, ttl_seconds=MESSAGE_REPLY_TTL_SECONDS)

    clock.advance(599)
    assert window.frame_of("e1") is not None

    clock.advance(2)
    assert window.frame_of("e1") is None


def test_stream_id_persists_across_refreshes():
    window = ReplyWindow()
    window.register("e1", {})

    window.set_stream_id("e1", "s_abc")

    assert window.stream_id_of("e1") == "s_abc"


def test_finish_releases_the_entry():
    window = ReplyWindow()
    window.register("e1", {})

    window.finish_stream("e1")

    assert window.frame_of("e1") is None


def test_purge_removes_only_expired_entries():
    clock = FakeClock()
    window = ReplyWindow(clock)
    window.register("short", {}, ttl_seconds=5)
    window.register("long", {}, ttl_seconds=600)

    clock.advance(10)

    assert window.purge_expired() == 1
    assert window.frame_of("long") is not None


def test_empty_window_is_falsy_but_usable():
    """__len__ 让空窗口的布尔值为假，调用方必须用 is None 判断。"""
    window = ReplyWindow()

    assert not window
    assert window is not None
