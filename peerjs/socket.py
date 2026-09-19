"""Convenience class to manage peer websocket connection."""
import asyncio
import json
import logging

import websockets
from pyee.asyncio import AsyncIOEventEmitter
from websockets.exceptions import ConnectionClosedError

from .enums import SocketEventType
from .servermessage import ServerMessage

log = logging.getLogger(__name__)


class Socket(AsyncIOEventEmitter):
    """An abstraction on top of WebSockets.

    Provides efficient connection for peers to the signaling server.
    """

    def __init__(
        self,
        secure: bool = True,
        host: str = None,
        port: int = None,
        path: str = None,
        key: str = None,
        pingInterval: int = 5000
    ) -> None:
        """Create new wrapper around websocket."""
        super().__init__()
        wsProtocol = "wss://" if secure else "ws://"
        self._baseUrl: str = f"{wsProtocol}{host}:{port}{path}peerjs?key={key}"
        self._disconnected: bool = True
        self._id: str = None
        self._messagesQueue: list = []
        self._websocket: websockets.client.WebSocketClientProtocol = None
        self._receiver: asyncio.Task = None

    async def _connect(self, wss_url=None):
        """Connect to WebSockets server."""
        assert wss_url
        # connect to websocket
        #
        # NOTE (本仓库补丁 9): 显式优先 IPv4。
        # 某些 Windows 环境里 0.peerjs.com 的 AAAA 记录可解析、TCP 也连得上,
        # 但 IPv6 路径实际不通 —— 表现为 TLS 握手超时:
        #   TimeoutError: timed out during opening handshake
        # 而 curl --ipv4 秒开 (实测 2s vs 20s 超时)。asyncio 默认按 getaddrinfo
        # 顺序取地址, 双栈环境里往往先试 IPv6, 于是必然超时。
        # NOTE (本仓库补丁 10): 放宽协议层 ping。
        # 上游只写 ping_interval=5 而不写 ping_timeout, websockets 默认
        # ping_timeout=20s。跨网络(尤其 Windows 侧走公网 broker)时一次抖动就会
        # 被判超时断开, 实测日志:
        #   WARNING peerjs.peer: Connection error: Lost connection to server.
        # 断开后信令丢失, 对端(控制端)连接必然超时。
        # 注意: PeerJS 官方那 5 秒是**应用层** ping ({"ping":"once"} 那条), 与
        # websockets 的协议层 ping 是两回事, 不该把两者混为一谈。
        websocket = await websockets.connect(
            wss_url,
            ping_interval=20,
            ping_timeout=60,
            close_timeout=5,
            **self._address_family_kwargs(wss_url)
        )
        self._sendQueuedMessages()
        log.debug("WebSockets open")
        await websocket.send(
            json.dumps({"ping": "once"})
        )
        self._disconnected = False
        return websocket

    @staticmethod
    def _address_family_kwargs(wss_url: str) -> dict:
        """给 websockets.connect 准备「优先 IPv4」的参数 (解析失败则不加)。"""
        try:
            import socket as _socket
            from urllib.parse import urlparse

            host = urlparse(wss_url).hostname
            if not host:
                return {}
            infos = _socket.getaddrinfo(host, None, type=_socket.SOCK_STREAM)
            families = {info[0] for info in infos}
            if _socket.AF_INET in families and _socket.AF_INET6 in families:
                # 双栈: 只留 IPv4, 绕开不通的 v6 路径
                return {"family": _socket.AF_INET}
        except Exception:
            pass
        return {}

    async def _receive(self, websocket=None):
        assert self._websocket
        try:
            # receive messages until websocket is closed
            async for message in self._websocket:
                try:
                    data = ServerMessage.from_json(message)
                    log.debug("Server message received: %s", data)
                    self.emit(SocketEventType.Message, data)
                except Exception as e:
                    log.exception("Invalid server message: %s, error %s",
                                  message, e)
                self.emit('message', message)
        except asyncio.CancelledError:
            log.debug('Websocket receive loop cancelled.')
            return
        except ConnectionClosedError as err:
            log.warning("Websocket connection closed with error. %s", err)
        except RuntimeError as e:
            log.warning("Websocket connection error: {}", e)
        finally:
            # remote peer closed websocket connection
            # or this socket was explicitly closed via close().
            # If its the former case, let's close our end and cleanup.
            if not self._disconnected:
                log.debug("Websocket connection closed")
                await self.close()

    async def start(self, id: str, token: str) -> None:
        """Start socket connection."""
        self._id = id
        _ws_url = f"{self._baseUrl}&id={id}&token={token}"
        if (self._websocket or not self._disconnected):
            # socket already connected
            return
        self._websocket = await self._connect(wss_url=_ws_url)
        # ask asyncio to schedule a receiver soon
        # it will end when the socket closes
        self._receiver = asyncio.create_task(
            self._receive())

    # Is the websocket currently open?
    def _wsOpen(self) -> bool:
        # websockets>=13 移除了 .open 属性, 改用 state
        from websockets.protocol import State
        return self._websocket is not None and self._websocket.state == State.OPEN

    # Send queued messages.
    def _sendQueuedMessages(self) -> None:
        # Create copy of queue and clear it,
        # because send method push the message back to queue
        # if something goes wrong
        copiedQueue = [*self._messagesQueue]
        self._messagesQueue = []
        for message in copiedQueue:
            self.send(message)

    async def send(self, data: any) -> None:
        """Expose send for DC & Peer."""
        # If the socket was already closed, nothing to do
        if self._disconnected:
            return

        log.debug('Socket sending data: \n%r', data)

        # If we didn't get an ID yet,
        # we can't yet send anything so we should queue
        # up these messages.
        if not self._id:
            self._messagesQueue.push(data)
            return
        # if not data['type']:
        #     self.emit(SocketEventType.Error, "Invalid message")
        #     return
        if not self._wsOpen():
            log.warning("Signaling websocket closed. Cannot send message %r.",
                        data)
            return
        message = json.dumps(data)
        log.debug('Message sent to signaling server: \n %r', message)
        await self._websocket.send(message)

    async def close(self) -> None:
        """Close socket and stop any pending communication."""
        if not self._disconnected:
            log.debug("Closing socket.")
            await self._cleanup()
            self._disconnected = True
            self.emit(SocketEventType.Disconnected)

    async def _cleanup(self) -> None:
        if self._receiver:
            self._receiver.cancel()
        if self._websocket:
            await self._websocket.close()
            self._websocket = None
