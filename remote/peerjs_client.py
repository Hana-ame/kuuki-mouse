"""PeerJS 客户端: 连到 ``PeerJsServer`` 并调用同一套 op。

用途有两个:
1. **端到端测试** —— 用两个 Peer 通过公开 broker 互连, 验证整条链路。
2. **实际控制** —— 从另一台机器/另一个进程发 ``mouse.move`` / ``screen.screenshot``。

注意: Python 版 fork 只能 ``serialization: "json"``, 且库内分块被注释掉,
所以接收大响应(截图)时要用 ``PeerJsChunker`` 自己重组。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .peerjs_server import PeerJsChunker

log = logging.getLogger("kuuki.remote.peerjs.client")

__all__ = ["PeerJsClient"]


class PeerJsClient:
    """连到 kuuki-mouse 的 PeerJS 主机并收发消息。"""

    def __init__(
        self,
        peer_id: str,
        token: Optional[str] = None,
        secure: bool = True,
        timeout: float = 30.0,
        serialization: str = "json",
    ):
        self.peer_id = peer_id  # 目标主机 id, 形如 kuuki-mouse-ABCDE
        self.token = token
        self.secure = secure
        self.timeout = timeout
        self.serialization = serialization

        self._peer = None
        self._conn = None
        self._chunker = PeerJsChunker()
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._next_id = 0
        self._connected = asyncio.Event()

    # ---------------- 连接 ----------------
    async def connect(self, timeout: Optional[float] = None) -> None:
        from peerjs.enums import ConnectionEventType, PeerEventType
        from peerjs.peer import Peer, PeerOptions

        from .ice import patch_aioice_addresses

        patch_aioice_addresses()

        # 客户端用随机 id (空 id 让 broker 分配)
        self._peer = Peer(None, PeerOptions(secure=self.secure))

        def on_open(_peer_id):
            log.debug("客户端 peer 已注册: %s", _peer_id)

        def on_error(err):
            log.warning("PeerJS 客户端错误: %s", err)

        self._peer.on(PeerEventType.Open, on_open)
        self._peer.on(PeerEventType.Error, on_error)
        await self._peer.start()

        conn = await self._peer.connect(self.peer_id, {"serialization": self.serialization})
        self._conn = conn

        def on_data(data):
            if isinstance(data, (bytes, bytearray)):
                log.warning("忽略二进制帧 (本端只支持 JSON)")
                return
            if not isinstance(data, dict):
                return
            msg = self._chunker.feed(data)
            if msg is not None:
                self._inbox.put_nowait(msg)

        def on_conn_open():
            self._connected.set()

        def on_close(*_):
            self._connected.clear()

        conn.on(ConnectionEventType.Data, on_data)
        conn.on(ConnectionEventType.Open, on_conn_open)
        conn.on(ConnectionEventType.Close, on_close)

        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout or self.timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"连接 PeerJS 主机 {self.peer_id} 超时")

        if self.token:
            await self.call("auth", {"token": self.token})

    async def close(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:
                pass
            self._conn = None
        if self._peer is not None:
            try:
                await self._peer.destroy()
            except Exception:
                pass
            self._peer = None

    async def __aenter__(self) -> "PeerJsClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ---------------- 调用 ----------------
    async def call(self, op: str, args: Optional[dict] = None, timeout: Optional[float] = None) -> dict:
        """发一条请求, 等对应 id 的响应 (自动跳过分块中间态)。"""
        self._next_id += 1
        req_id = self._next_id
        await self._conn.send({"id": req_id, "op": op, "args": args or {}})

        deadline = asyncio.get_running_loop().time() + (timeout or self.timeout)
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"{op} 等待响应超时")
            msg = await asyncio.wait_for(self._inbox.get(), timeout=remaining)
            if msg.get("id") != req_id:
                continue  # 不是这条请求的响应
            if not msg.get("ok"):
                error = msg.get("error") or {}
                raise RuntimeError(f"{error.get('code')}: {error.get('message')}")
            return msg.get("result") or {}

    async def screenshot(self, args: Optional[dict] = None) -> bytes:
        """抓一帧, 返回图片字节 (服务端返回 base64, 这里解码)。"""
        import base64

        result = await self.call("screen.screenshot", args or {})
        return base64.b64decode(result["image_b64"])
