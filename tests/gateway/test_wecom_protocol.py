"""长连接协议的端到端验证。

用真实的官方 SDK 连接本地 mock 企微服务端，验证整条链路：
订阅认证 → 消息回调 → 归属解析 → 流式回复 → 主动推送。

这是待确认项 P0 #3（企微长连接 SDK 的形态与鉴权方式）的实证：SDK 的出站帧
格式必须与《智能机器人长连接》文档一致，否则联调会卡在协议层。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import ssl
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import websockets

import aibot.ws as aibot_ws
from aibot import WSClient, WSClientOptions
from app.gateway.actions import CardActionHandler
from app.gateway.cards.binding import CardBinding
from app.gateway.client import WecomClient
from app.gateway.feedback import FeedbackHandler
from app.gateway.inbound import InboundDispatcher
from app.gateway.outbound import MessageSender
from app.gateway.reply_window import ReplyWindow
from app.runtime.worker.queue import InMemoryTaskSink
from app.space.context_factory import SpaceContextFactory
from app.space.registry import ChatRegistry
from app.space.resolver import BotBinding, BotBindingRegistry, ScopeType
from app.space.topic.router import TopicRouter
from tests.conftest import message_frame
from tests.gateway.conftest import activate


def _self_signed_cert(directory: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False
        )
        .sign(key, hashes.SHA256())
    )

    cert_path = directory / "cert.pem"
    key_path = directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


class MockWecomServer:
    """按《智能机器人长连接》协议应答的最小服务端。"""

    def __init__(self):
        self.received: List[Dict[str, Any]] = []
        self.subscribed = asyncio.Event()
        self._connection: Optional[Any] = None

    async def handle(self, connection, path: str = "") -> None:
        self._connection = connection
        async for raw in connection:
            frame = json.loads(raw)
            self.received.append(frame)
            req_id = (frame.get("headers") or {}).get("req_id", "")
            # 所有开发者 → 企微的命令都回同 req_id 的 ack。
            await connection.send(
                json.dumps({"headers": {"req_id": req_id}, "errcode": 0, "errmsg": "ok"})
            )
            if frame.get("cmd") == "aibot_subscribe":
                self.subscribed.set()

    async def push(self, frame: Dict[str, Any]) -> None:
        await self._connection.send(json.dumps(frame))

    def of_cmd(self, cmd: str) -> List[Dict[str, Any]]:
        return [f for f in self.received if f.get("cmd") == cmd]


async def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


@pytest.fixture
async def wired(space_repo, card_repo, feedback_repo, monkeypatch):
    # SDK 固定走 wss 并校验证书，本地自签证书需要放行。
    monkeypatch.setattr(aibot_ws, "_SSL_CONTEXT", ssl._create_unverified_context())

    server = MockWecomServer()
    with tempfile.TemporaryDirectory() as tmp:
        cert_path, key_path = _self_signed_cert(Path(tmp))
        server_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ssl.load_cert_chain(cert_path, key_path)

        async with websockets.serve(
            server.handle, "127.0.0.1", 0, ssl=server_ssl
        ) as ws_server:
            port = ws_server.sockets[0].getsockname()[1]
            raw_client = WSClient(
                WSClientOptions(
                    bot_id="bot_alpha",
                    secret="secret_alpha",
                    ws_url=f"wss://127.0.0.1:{port}",
                    heartbeat_interval=100,
                    max_reconnect_attempts=0,
                )
            )
            bindings = BotBindingRegistry(
                {"bot_alpha": BotBinding("bot_alpha", ScopeType.WORKSPACE, "alpha")}, "bot_alpha"
            )
            space = SpaceContextFactory(
                space_repo, bindings, TopicRouter(space_repo)
            )
            chats = ChatRegistry(space_repo)
            cards = CardBinding(card_repo)

            client = WecomClient(raw_client)
            window = ReplyWindow()
            sender = MessageSender(raw_client, window, chats, "alpha", "bot_alpha")
            sink = InMemoryTaskSink()
            inbound = InboundDispatcher(
                bot_id="bot_alpha",
                space=space,
                task_sink=sink,
                sender=sender,
                card_actions=CardActionHandler(cards, space, sink, sender),
                feedbacks=FeedbackHandler(cards, feedback_repo),
                reply_window=window,
            )
            client.subscribe(inbound.on_message, inbound.on_event)

            await client.start()
            await asyncio.wait_for(server.subscribed.wait(), timeout=5)
            try:
                yield server, client, sender, sink
            finally:
                await client.stop()


async def test_subscribe_frame_matches_protocol(wired):
    server, *_ = wired

    subscribe = server.of_cmd("aibot_subscribe")[0]

    assert subscribe["body"] == {"bot_id": "bot_alpha", "secret": "secret_alpha"}
    assert subscribe["headers"]["req_id"]


async def test_heartbeat_keeps_connection_alive(wired):
    server, *_ = wired

    await _wait_for(lambda: len(server.of_cmd("ping")) >= 2)

    assert server.of_cmd("ping")[0]["headers"]["req_id"].startswith("ping")


async def test_inbound_message_reaches_task_sink(wired):
    server, _, _, sink = wired

    await server.push(message_frame(text="@WeTag TAPD-1001 要改", msgid="m1"))
    await _wait_for(lambda: len(sink.accepted) == 1)

    event, context = sink.accepted[0]
    assert event.message.text == "@WeTag TAPD-1001 要改"
    assert context.workspace_id == "alpha"
    assert context.topic_id


async def test_stream_reply_frame_matches_protocol(wired):
    server, _, sender, sink = wired
    await server.push(message_frame(msgid="m1", req_id="req_abc"))
    await _wait_for(lambda: len(sink.accepted) == 1)

    await sender.reply_stream("m1", "已受理，正在处理…")
    await sender.reply_stream("m1", "已受理，正在处理…完成", finish=True)

    frames = server.of_cmd("aibot_respond_msg")
    assert len(frames) == 2
    # req_id 必须透传回调帧的值，否则企微无法把回复关联到那次会话。
    assert all(f["headers"]["req_id"] == "req_abc" for f in frames)
    assert frames[0]["body"]["msgtype"] == "stream"
    # 同一条流式消息的两次推送共用一个 stream.id。
    assert frames[0]["body"]["stream"]["id"] == frames[1]["body"]["stream"]["id"]
    assert frames[0]["body"]["stream"]["finish"] is False
    assert frames[1]["body"]["stream"]["finish"] is True


async def test_proactive_push_frame_matches_protocol(wired, space_repo):
    server, _, sender, _ = wired
    activate(space_repo)

    await sender.push_markdown("chat_001", "**变更通知**：文档已更新")

    frame = server.of_cmd("aibot_send_msg")[0]
    assert frame["body"]["chatid"] == "chat_001"
    assert frame["body"]["chat_type"] == 2
    assert frame["body"]["msgtype"] == "markdown"
    assert frame["body"]["markdown"]["content"] == "**变更通知**：文档已更新"


async def test_card_update_frame_matches_protocol(wired, card_repo):
    server, *_ = wired
    card_repo.seed("c_1")
    await server.push(
        {
            "cmd": "aibot_event_callback",
            "headers": {"req_id": "req_card"},
            "body": {
                "msgid": "evt_1",
                "aibotid": "bot_alpha",
                "chatid": "chat_001",
                "chattype": "group",
                "from": {"userid": "user_123"},
                "msgtype": "event",
                "event": {
                    "eventtype": "template_card_event",
                    "template_card_event": {
                        "task_id": "c_1",
                        "event_key": "useful",
                    },
                },
            },
        }
    )

    await _wait_for(lambda: len(server.of_cmd("aibot_respond_update_msg")) == 1)

    frame = server.of_cmd("aibot_respond_update_msg")[0]
    assert frame["headers"]["req_id"] == "req_card"
    assert frame["body"]["response_type"] == "update_template_card"
    assert frame["body"]["template_card"]["task_id"] == "c_1"
