"""WebSocket 服务端: 文本 JSON 请求 + 二进制截屏帧。

协议 (全部是 UTF-8 文本帧, 除非标注"二进制")
-------------------------------------------
请求::

    {"id": 1, "op": "mouse.move", "args": {"x": 100, "y": 200}}
    {"id": 2, "op": "mouse.move", "x": 100, "y": 200}     # args 可平铺
    {"id": 3, "batch": [{"op": "ping"}, {"op": "mouse.position"}]}

响应::

    {"id": 1, "ok": true,  "result": {...}}
    {"id": 1, "ok": false, "error": {"code": "bad_request", "message": "..."}}

兼容 kuuki 老协议: 不带 ``op`` 但带 ``t``/``mouse``/``text``/``key`` 的消息会被
直接路由到 ``app.handle_message`` (空气鼠标姿态/按键/文本), 这样 ``web/`` 页面
可以不用 PeerJS, 直接把传感器数据发到本机 WS::

    {"t": "sensor", "x":0, "y":0, "z":9.8, "alpha":0, "beta":0, "gamma":0, "gx":0,"gy":0,"gz":0}
    {"t": "mouse", "button": "left"}
    {"t": "text", "text": "hello"}

二进制帧 (截屏)
--------------
``{"op":"screen.grab","args":{...}}`` 或 ``screen.screenshot`` 带 ``"binary": true``
时, 服务端回一帧二进制::

    [4 字节大端: JSON 头长度 N][N 字节 JSON 头][图片字节]

``screen.watch`` 会按 ``fps`` 持续推同样的二进制帧 (JSON 头里 ``event="frame"``)::

    {"op": "screen.watch", "args": {"fps": 2, "format": "jpeg", "quality": 60, "max_width": 1280}}
    {"op": "screen.unwatch", "args": {"watch_id": "w1"}}

鉴权
----
``--token`` 或环境变量 ``KUUKI_REMOTE_TOKEN`` 设置后, 三种方式任一即可:
``ws://host:port/?token=XXX``、握手头 ``Authorization: Bearer XXX``, 或首条消息
``{"op":"auth","args":{"token":"XXX"}}``。不设 token 时只应绑在 127.0.0.1。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import struct
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

try:  # websockets >= 13 的新 asyncio API
    from websockets.asyncio.server import serve as _ws_serve

    _NEW_API = True
except Exception:  # pragma: no cover - 兼容老版本
    from websockets.legacy.server import serve as _ws_serve

    _NEW_API = False

from .service import RemoteError, RemoteService

log = logging.getLogger("kuuki.remote.ws")

__all__ = ["WsServer", "encode_frame", "decode_frame"]


def encode_frame(header: dict, payload: bytes) -> bytes:
    """二进制帧 = 4 字节大端头长度 + JSON 头 + 图片字节。"""
    head = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(head)) + head + payload


def decode_frame(data: bytes):
    """``encode_frame`` 的逆操作, 返回 (header, payload)。客户端/测试用。"""
    if len(data) < 4:
        raise ValueError("帧太短")
    (length,) = struct.unpack(">I", data[:4])
    head = json.loads(data[4 : 4 + length].decode("utf-8"))
    return head, data[4 + length :]


class WsServer:
    """把 ``RemoteService`` 挂到一个 WebSocket 端口上。"""

    def __init__(
        self,
        service: RemoteService,
        host: str = "127.0.0.1",
        port: int = 8765,
        token: Optional[str] = None,
        max_fps: float = 30.0,
    ):
        self.service = service
        self.host = host
        self.port = port
        self.token = token
        self.max_fps = max_fps
        self._server = None

    # ---------------- 鉴权 ----------------
    def _check_token(self, provided: Optional[str]) -> bool:
        if not self.token:
            return True
        if not provided:
            return False
        return hmac.compare_digest(str(provided), str(self.token))

    @staticmethod
    def _extract_token(conn) -> str:
        request = getattr(conn, "request", None)
        path = ""
        headers = None
        if request is not None:
            path = getattr(request, "path", "") or ""
            headers = getattr(request, "headers", None)
        else:  # websockets legacy
            path = getattr(conn, "path", "") or ""
            headers = getattr(conn, "request_headers", None)
        query = parse_qs(urlparse(path).query)
        token = (query.get("token") or [""])[0]
        if token:
            return token
        if headers is not None:
            auth = headers.get("Authorization", "") or ""
            if auth.lower().startswith("bearer "):
                return auth[7:].strip()
            return headers.get("X-Kuuki-Token", "") or ""
        return ""

    # ---------------- 发送辅助 ----------------
    @staticmethod
    async def _send_json(conn, payload: dict) -> None:
        await conn.send(json.dumps(payload, ensure_ascii=False))

    async def _send_error(self, conn, req_id: Any, error: RemoteError) -> None:
        await self._send_json(
            conn, {"id": req_id, "ok": False, "error": error.to_dict()}
        )

    # ---------------- 主处理 ----------------
    async def _handler(self, conn) -> None:
        peer = getattr(conn, "remote_address", None)
        authed = self._check_token(self._extract_token(conn))
        watches: Dict[str, asyncio.Task] = {}
        watch_seq = 0
        log.info("WS 客户端接入 %s (authed=%s)", peer, authed)

        try:
            async for raw in conn:
                # ---- 解析 ----
                if isinstance(raw, (bytes, bytearray)):
                    await self._send_error(
                        conn, None, RemoteError("bad_request", "客户端二进制帧不受支持")
                    )
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError as exc:
                    await self._send_error(
                        conn, None, RemoteError("bad_request", f"JSON 解析失败: {exc}")
                    )
                    continue
                if not isinstance(msg, dict):
                    await self._send_error(
                        conn, None, RemoteError("bad_request", "请求必须是 JSON 对象")
                    )
                    continue

                req_id = msg.get("id")
                op = msg.get("op")

                # ---- 鉴权 (未通过时只接受 auth) ----
                if not authed:
                    token = None
                    if op == "auth":
                        args = msg.get("args") or {}
                        token = args.get("token") or msg.get("token")
                    if self._check_token(token):
                        authed = True
                        await self._send_json(
                            conn, {"id": req_id, "ok": True, "result": {"authenticated": True}}
                        )
                        continue
                    await self._send_error(conn, req_id, RemoteError("unauthorized", "需要 token"))
                    await conn.close(1008, "unauthorized")
                    return

                # ---- kuuki 老协议自动路由 ----
                if op is None and any(k in msg for k in ("t", "mouse", "text", "key", "calibrate")):
                    try:
                        result = await asyncio.to_thread(
                            self.service.handle, "kuuki", {"message": msg}
                        )
                        await self._send_json(conn, {"id": req_id, "ok": True, "result": result})
                    except RemoteError as exc:
                        await self._send_error(conn, req_id, exc)
                    continue

                # ---- 需要连接状态的 op ----
                if op == "auth":
                    await self._send_json(
                        conn, {"id": req_id, "ok": True, "result": {"authenticated": authed}}
                    )
                    continue

                if op in ("screen.watch", "watch"):
                    watch_seq += 1
                    watch_id = str((msg.get("args") or {}).get("watch_id") or f"w{watch_seq}")
                    args = dict(msg.get("args") or {})
                    old = watches.pop(watch_id, None)
                    if old is not None:
                        old.cancel()
                    task = asyncio.create_task(self._watch_loop(conn, watch_id, args))
                    watches[watch_id] = task
                    await self._send_json(
                        conn,
                        {
                            "id": req_id,
                            "ok": True,
                            "result": {
                                "watch_id": watch_id,
                                "fps": min(float(args.get("fps", 2) or 2), self.max_fps),
                                "format": args.get("format", "png"),
                            },
                        },
                    )
                    continue

                if op in ("screen.unwatch", "unwatch"):
                    args = dict(msg.get("args") or {})
                    watch_id = str(args.get("watch_id") or "")
                    targets = [watch_id] if watch_id else list(watches)
                    for wid in targets:
                        task = watches.pop(wid, None)
                        if task is not None:
                            task.cancel()
                    await self._send_json(
                        conn, {"id": req_id, "ok": True, "result": {"stopped": targets}}
                    )
                    continue

                if op in ("screen.grab", "grab") or (
                    op in ("screen.screenshot", "screenshot", "capture")
                    and (msg.get("args") or {}).get("binary")
                ):
                    args = dict(msg.get("args") or {})
                    try:
                        capture, data = await asyncio.to_thread(self.service.capture, args)
                    except RemoteError as exc:
                        await self._send_error(conn, req_id, exc)
                        continue
                    except Exception as exc:  # noqa: BLE001
                        await self._send_error(
                            conn, req_id,
                            RemoteError("internal", f"{exc.__class__.__name__}: {exc}"),
                        )
                        continue
                    header = {
                        "event": "image",
                        "id": req_id,
                        "format": capture.format,
                        "width": capture.width,
                        "height": capture.height,
                        # source_* 是**原始屏幕**尺寸。截图可能被 max_width 缩过,
                        # 拿它跟 width 一除才是"展示坐标 -> 真实屏幕坐标"的系数。
                        # JSON 通道 (Capture.to_dict()) 一直有这两个字段, 二进制帧
                        # 头漏了 —— 于是按帧头算系数会得到 1.0, 鼠标点偏整个缩放比
                        # (1680 的屏按 1000 宽的帧去点, 偏到屏幕中间偏左上)。
                        "source_width": capture.source_width,
                        "source_height": capture.source_height,
                        "scale": round(capture.scale, 4),
                        "bytes": len(data),
                        "ts": capture.captured_at,
                        "duration_ms": round(capture.duration_ms, 2),
                        "backend": capture.backend,
                        "region": capture.region.to_dict() if capture.region else None,
                    }
                    await conn.send(encode_frame(header, data))
                    continue

                # ---- 普通 op: 丢给线程池, 别阻塞事件循环 ----
                try:
                    result = await asyncio.to_thread(self.service.handle_request, msg)
                    await self._send_json(conn, {"id": req_id, "ok": True, "result": result})
                except RemoteError as exc:
                    await self._send_error(conn, req_id, exc)
        except Exception as exc:  # 连接异常 (含正常关闭的 ConnectionClosed)
            if exc.__class__.__name__ not in (
                "ConnectionClosed", "ConnectionClosedOK", "ConnectionClosedError"
            ):
                log.warning("WS 连接异常: %s: %s", exc.__class__.__name__, exc)
        finally:
            for task in watches.values():
                task.cancel()
            if watches:
                await asyncio.gather(*watches.values(), return_exceptions=True)
            log.info("WS 客户端断开 %s", peer)

    # ---------------- 推帧循环 ----------------
    async def _watch_loop(self, conn, watch_id: str, args: dict) -> None:
        fps = float(args.get("fps", 2) or 2)
        fps = min(max(fps, 0.1), self.max_fps)
        count = int(args.get("count", 0) or 0)
        seq = 0
        try:
            while True:
                seq += 1
                try:
                    capture, data = await asyncio.to_thread(self.service.capture, args)
                except Exception as exc:  # noqa: BLE001
                    await self._send_json(
                        conn,
                        {
                            "event": "error",
                            "watch_id": watch_id,
                            "error": {
                                "code": "internal",
                                "message": f"{exc.__class__.__name__}: {exc}",
                            },
                        },
                    )
                    return
                header = {
                    "event": "frame",
                    "watch_id": watch_id,
                    "seq": seq,
                    "format": capture.format,
                    "width": capture.width,
                    "height": capture.height,
                    "bytes": len(data),
                    "ts": capture.captured_at,
                    "duration_ms": round(capture.duration_ms, 2),
                    "backend": capture.backend,
                }
                await conn.send(encode_frame(header, data))
                if count and seq >= count:
                    break
                await asyncio.sleep(1.0 / fps)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 连接断开等
            log.debug("watch %s 结束: %s", watch_id, exc)
        finally:
            try:
                await self._send_json(
                    conn, {"event": "watch.stopped", "watch_id": watch_id, "frames": seq}
                )
            except Exception:
                pass

    # ---------------- 生命周期 ----------------
    async def start(self):
        handler = self._handler
        if not _NEW_API:  # pragma: no cover - 老 websockets 需要 (ws, path) 签名
            async def _legacy_handler(conn, path=None):  # type: ignore[misc]
                await self._handler(conn)

            handler = _legacy_handler

        self._server = await _ws_serve(
            handler,
            self.host,
            self.port,
            ping_interval=20,
            ping_timeout=20,
            max_size=16 * 1024 * 1024,
        )
        log.info("WebSocket 服务已启动: ws://%s:%s", self.host, self.port)
        return self._server

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None

    def describe(self) -> dict:
        return {
            "transport": "websocket",
            "url": f"ws://{self.host}:{self.port}/",
            "host": self.host,
            "port": self.port,
            "token_required": bool(self.token),
        }
