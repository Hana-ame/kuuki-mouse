"""命令行客户端 (WS + gRPC): 调试与冒烟验证用。

    python -m remote.client ws ping
    python -m remote.client ws info
    python -m remote.client ws op mouse.position
    python -m remote.client ws op mouse.move --args '{"x":400,"y":300,"duration":0.2}'
    python -m remote.client ws screenshot /tmp/shot.png --max-width 1280
    python -m remote.client ws watch /tmp/frames --fps 2 --count 5 --format jpeg
    python -m remote.client grpc info
    python -m remote.client grpc screenshot /tmp/shot.png
    python -m remote.client grpc stream /tmp/frames --fps 2 --count 5
    python -m remote.client peerjs --peer kuuki-mouse-ABCDE info

带 token 时加 ``--token XXX`` (WS 会拼进 URL, gRPC 会放进 metadata)。

``locate`` 是给**自己看不了图**的调用方准备的: 它把"屏幕上哪个坐标是那个按钮"
算出来, 直接吐可以喂给 ``mouse.click`` 的真实屏幕坐标::

    python -m remote.client ws locate --describe              # 这一屏大概是什么样
    python -m remote.client ws locate --dominant              # 主色调有哪几种
    python -m remote.client ws locate --color '#1a73e8'       # 找蓝色按钮
    python -m remote.client ws locate --template icon.png     # 找图标 (模板匹配)
    python -m remote.client ws locate --diff-with prev.png    # 哪一坨变了
    python -m remote.client ws locate --save-template btn.png --box 900,540,40,40

输出的每个矩形都带 ``screen.x``/``screen.y``: 那是乘过缩放系数的**真实屏幕坐标**,
可以直接拿去点。``x``/``y`` 是截图自身的展示坐标, 别混用。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any, Dict, Optional, Tuple

# 允许 `python remote/client.py` 直接跑
if __package__ in (None, ""):  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from remote.service import VERSION, parse_points  # noqa: E402
from remote.ws_server import decode_frame  # noqa: E402

try:
    from websockets.asyncio.client import connect as ws_connect
except Exception:  # pragma: no cover
    from websockets.client import connect as ws_connect


# ================================================================ WebSocket


class WsClient:
    """极简 WebSocket 客户端: 一次请求一次响应 + 二进制截屏帧。"""

    def __init__(
        self,
        url: str = "ws://127.0.0.1:8765",
        token: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self.url = url
        self.token = token or os.environ.get("KUUKI_REMOTE_TOKEN")
        self.timeout = timeout
        self.ws = None
        self._next_id = 0

    async def __aenter__(self) -> "WsClient":
        url = self.url
        headers = {}
        if self.token:
            if "token=" not in url:
                url = url + ("&" if "?" in url else "?") + "token=" + self.token
            headers["Authorization"] = f"Bearer {self.token}"
        try:  # websockets >= 14
            self.ws = await ws_connect(
                url, additional_headers=headers or None, max_size=64 * 1024 * 1024
            )
        except TypeError:  # pragma: no cover - websockets 13 及更早
            self.ws = await ws_connect(
                url, extra_headers=headers or None, max_size=64 * 1024 * 1024
            )
        return self

    async def __aexit__(self, *exc) -> None:
        if self.ws is not None:
            await self.ws.close()
            self.ws = None

    async def _send(self, op: str, args: Optional[dict] = None, req_id: Optional[int] = None,
                    top: Optional[dict] = None) -> int:
        self._next_id += 1
        rid = self._next_id if req_id is None else req_id
        envelope: dict = {"id": rid, "op": op, "args": args or {}}
        # 少数字段必须在顶层 (如 batch), 与 PeerJS 客户端保持同一套信封写法
        if top:
            envelope.update(top)
        await self.ws.send(json.dumps(envelope, ensure_ascii=False))
        return rid

    async def call(self, op: str, args: Optional[dict] = None, top: Optional[dict] = None) -> dict:
        """发一条请求, 返回 result; 服务端报错则抛 RuntimeError。"""
        rid = await self._send(op, args, top=top)
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=self.timeout)
            if isinstance(raw, (bytes, bytearray)):
                continue  # 二进制帧不是请求响应
            msg = json.loads(raw)
            if msg.get("event"):
                continue
            if msg.get("id") != rid:
                continue
            if not msg.get("ok"):
                error = msg.get("error") or {}
                raise RuntimeError(f"{error.get('code')}: {error.get('message')}")
            # 不能用 `or {}`: 会吞掉 0 / False / "" / [] 这类合法结果 (与 PeerJS 一致)
            return msg["result"] if "result" in msg else {}

    async def batch(self, items: list) -> dict:
        """一次下发多个 op, 压掉 N-1 次往返。"""
        return await self.call("batch", {}, top={"batch": items})

    async def screenshot(self, args: Optional[dict] = None):
        """走二进制通道抓一帧, 返回 (header, bytes)。"""
        rid = await self._send("screen.grab", args or {})
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=self.timeout)
            if isinstance(raw, (bytes, bytearray)):
                return decode_frame(bytes(raw))
            msg = json.loads(raw)
            if msg.get("id") == rid and not msg.get("ok"):
                error = msg.get("error") or {}
                raise RuntimeError(f"{error.get('code')}: {error.get('message')}")

    async def watch(self, args: dict, on_frame, count: int = 0) -> int:
        """订阅推流; ``on_frame(header, payload)`` 返回 False 可提前停止。"""
        ack = await self.call("screen.watch", args)
        watch_id = ack.get("watch_id")
        frames = 0
        try:
            while count == 0 or frames < count:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=self.timeout)
                if not isinstance(raw, (bytes, bytearray)):
                    msg = json.loads(raw)
                    if msg.get("event") == "error":
                        raise RuntimeError(str(msg.get("error")))
                    continue
                header, payload = decode_frame(bytes(raw))
                frames += 1
                if on_frame(header, payload) is False:
                    break
        finally:
            try:
                await self.call("screen.unwatch", {"watch_id": watch_id})
            except Exception:
                pass
        return frames


# ================================================================ gRPC


class GrpcClient:
    """把 ``RemoteService`` 的 op 名映射到 ``RemoteControl`` 的 RPC。"""

    def __init__(
        self,
        target: str = "127.0.0.1:50051",
        token: Optional[str] = None,
        timeout: float = 30.0,
    ):
        import grpc
        from google.protobuf.json_format import MessageToDict

        from remote.proto import kuuki_remote_pb2 as pb
        from remote.proto import kuuki_remote_pb2_grpc as pb_grpc

        self._grpc = grpc
        self._pb = pb
        self._dict = MessageToDict
        self.token = token or os.environ.get("KUUKI_REMOTE_TOKEN")
        self.timeout = timeout
        self.channel = grpc.insecure_channel(target)
        self.stub = pb_grpc.RemoteControlStub(self.channel)

        def _to_dict(message):
            """protobuf -> dict, 尽量与 WS 侧的 JSON 长得一样。

            两个默认行为都要改:
            - 字段名默认转 lowerCamelCase, 与 WS 的 snake_case 不一致;
            - **取默认值的字段默认被省略** —— proto3 标量没有存在性, 光标恰好在
              ``y=0`` 时 ``{"x":1396,"y":0}`` 会变成只有 x, 控制端读 ``pos["y"]``
              就 KeyError。WS 侧永远给全, 所以这里也必须给全。
            """
            try:
                return MessageToDict(
                    message,
                    preserving_proto_field_name=True,
                    always_print_fields_with_no_presence=True,
                )
            except TypeError:  # pragma: no cover - 老版本 protobuf 没这个参数
                return MessageToDict(message, preserving_proto_field_name=True)

        self._to_dict = _to_dict

    def __enter__(self) -> "GrpcClient":
        return self

    def __exit__(self, *exc) -> None:
        self.channel.close()

    def _metadata(self):
        return [("authorization", f"Bearer {self.token}")] if self.token else None

    def _kwargs(self):
        return {"timeout": self.timeout, "metadata": self._metadata()}

    def _rect(self, region):
        if not region:
            return None
        pb = self._pb
        if isinstance(region, dict):
            return pb.Rect(
                left=int(region.get("left", 0)),
                top=int(region.get("top", 0)),
                width=int(region.get("width", 0)),
                height=int(region.get("height", 0)),
            )
        left, top, width, height = (int(v) for v in region)
        return pb.Rect(left=left, top=top, width=width, height=height)

    def _shot_request(self, args: dict):
        pb = self._pb
        request = pb.ScreenshotRequest(
            format=args.get("format", "png"),
            quality=int(args.get("quality", 80)),
            max_width=int(args.get("max_width", 0) or 0),
            max_height=int(args.get("max_height", 0) or 0),
            scale=float(args.get("scale", 0) or 0),
            draw_cursor=bool(args.get("draw_cursor", False)),
        )
        rect = self._rect(args.get("region"))
        if rect is not None:
            request.region.CopyFrom(rect)
        return request

    def _scroll_request(self, args: dict):
        pb = self._pb
        request = pb.ScrollRequest(
            dx=int(args.get("dx", 0) or 0),
            dy=int(args.get("dy", args.get("delta", 0)) or 0),
            steps=int(args.get("steps", 0) or 0),
        )
        # interval / x / y 是 optional 字段: 只在 args 里真的有时才设,
        # 让"没给"与"显式给 0"在服务端仍能区分开 (与 WS 侧一致)。
        if args.get("interval") is not None:
            request.interval = float(args["interval"])
        if args.get("x") is not None:
            request.at_x = int(args["x"])
        if args.get("y") is not None:
            request.at_y = int(args["y"])
        return request

    def _focus_request(self, args: dict):
        """``window.focus`` 参数 -> protobuf。

        ``hwnd`` 只有真的给了才写: HWND 不会是 0, 写 0 就等于"找句柄为 0 的窗口",
        必然 not_found。``wait`` 是 optional, 同理只对显式传入生效 (缺省留给服务
        端默认的 0.5 秒)。
        """
        pb = self._pb
        request = pb.FocusWindowRequest(
            title=args.get("title", ""),
            process=args.get("process", args.get("proc", "")),
            index=int(args.get("index", 0) or 0),
        )
        handle = args.get("hwnd", args.get("handle"))
        if handle:
            request.hwnd = int(handle)
        if args.get("wait") is not None:
            request.wait = float(args["wait"])
        return request

    def _calibrate_request(self, args: dict):
        """``screen.calibrate`` 参数 -> protobuf。

        标量为 0 的一律按"没给"走服务端默认 (``cols`` / ``rows`` / ``margin`` …
        都不存在"显式 0 另有含义")。只有 ``restore`` 是真 bool 语义: 显式 false
        要能表达"别把我光标挪回去", 所以它在 proto 里是 optional。
        """
        pb = self._pb
        request = pb.CalibrateRequest(
            cols=int(args.get("cols", 0) or 0),
            rows=int(args.get("rows", 0) or 0),
            margin=float(args.get("margin", 0) or 0),
            settle=float(args.get("settle", 0) or 0),
            tolerance=float(args.get("tolerance", 0) or 0),
            max_width=int(args.get("max_width", 0) or 0),
        )
        if args.get("restore") is not None:
            request.restore = bool(args["restore"])
        return request

    def _drag_request(self, args: dict):
        pb = self._pb
        request = pb.DragRequest(button=args.get("button", "left"))
        # 不要用 ``args.get("duration", 0.3) or 0.3``: 那样显式传 0 会被换成 0.3。
        if args.get("duration") is not None:
            request.duration = float(args["duration"])
        points = args.get("points")
        if points is None and args.get("path") is not None:
            points = args.get("path")
        if points:
            for px, py in parse_points(points):
                request.points.append(pb.Point(x=int(px), y=int(py)))
        else:
            request.x1 = int(args.get("x1", 0) or 0)
            request.y1 = int(args.get("y1", 0) or 0)
            request.x2 = int(args.get("x2", 0) or 0)
            request.y2 = int(args.get("y2", 0) or 0)
        return request

    def screenshot(self, args: Optional[dict] = None) -> bytes:
        """返回图片字节; 元数据在 ``self.last_image``。"""
        image = self.stub.Screenshot(self._shot_request(args or {}), **self._kwargs())
        self.last_image = image
        return image.data

    def stream(self, args: dict, on_frame, count: int = 0) -> int:
        pb = self._pb
        request = pb.StreamScreenshotsRequest(
            shot=self._shot_request(args),
            fps=float(args.get("fps", 0) or 0),
            interval_ms=int(args.get("interval_ms", 0) or 0),
            count=int(count or args.get("count", 0) or 0),
        )
        frames = 0
        for image in self.stub.StreamScreenshots(request, **self._kwargs()):
            frames += 1
            if on_frame(image) is False:
                break
        return frames

    def call(self, op: str, args: Optional[dict] = None) -> Any:
        pb = self._pb
        args = args or {}
        empty = pb.Empty()
        table = {
            "ping": lambda: self.stub.Ping(empty, **self._kwargs()),
            "info": lambda: self.stub.GetInfo(empty, **self._kwargs()),
            "screen.screenshot": lambda: self.stub.Screenshot(
                self._shot_request(args), **self._kwargs()
            ),
            "screen.monitors": lambda: self.stub.Monitors(
                pb.MonitorsRequest(**_optional_int(args, "x", "y")), **self._kwargs()
            ),
            "mouse.position": lambda: self.stub.GetMousePosition(empty, **self._kwargs()),
            "mouse.move": lambda: self.stub.MoveMouse(
                pb.MoveMouseRequest(
                    x=int(args.get("x", 0)),
                    y=int(args.get("y", 0)),
                    duration=float(args.get("duration", 0) or 0),
                ),
                **self._kwargs(),
            ),
            "mouse.move_rel": lambda: self.stub.MoveMouseRelative(
                pb.MoveMouseRelativeRequest(
                    dx=int(args.get("dx", 0)),
                    dy=int(args.get("dy", 0)),
                    duration=float(args.get("duration", 0) or 0),
                ),
                **self._kwargs(),
            ),
            "mouse.click": lambda: self.stub.ClickMouse(
                pb.ClickMouseRequest(
                    button=args.get("button", "left"),
                    clicks=int(args.get("clicks", 1) or 1),
                    # interval / hold 是 optional 且 0 是合法值: 只在 args 里真的
                    # 给了才写进请求, 否则服务端会用默认 (50ms / 60ms)。
                    # 写成 `or 默认` 会把 --interval 0 / --hold 0 静默改成默认值,
                    # 于是同一条命令在 WS 与 gRPC 上行为不一样。
                    **_optional_number(args, "interval", "hold"),
                    **_optional_at(args),
                ),
                **self._kwargs(),
            ),
            "mouse.down": lambda: self.stub.MouseDown(
                pb.MouseButtonRequest(button=args.get("button", "left")), **self._kwargs()
            ),
            "mouse.up": lambda: self.stub.MouseUp(
                pb.MouseButtonRequest(button=args.get("button", "left")), **self._kwargs()
            ),
            "mouse.scroll": lambda: self.stub.Scroll(
                self._scroll_request(args), **self._kwargs()
            ),
            "mouse.scroll_h": lambda: self.stub.ScrollHorizontal(
                self._scroll_request(args), **self._kwargs()
            ),
            "mouse.drag": lambda: self.stub.Drag(
                self._drag_request(args), **self._kwargs()
            ),
            "keyboard.type": lambda: self.stub.TypeText(
                pb.TypeTextRequest(
                    text=args.get("text", ""), interval=float(args.get("interval", 0) or 0)
                ),
                **self._kwargs(),
            ),
            "keyboard.key": lambda: self.stub.PressKey(
                pb.KeyRequest(
                    key=args.get("key", ""),
                    action=args.get("action", "tap"),
                    modifiers=list(args.get("modifiers", []) or []),
                ),
                **self._kwargs(),
            ),
            "keyboard.hotkey": lambda: self.stub.Hotkey(
                pb.HotkeyRequest(keys=_combo_keys(args)),
                **self._kwargs(),
            ),
            "keyboard.combo": lambda: self.stub.Combo(
                pb.ComboRequest(
                    keys=_combo_keys(args),
                    hold_ms=float(args.get("hold_ms", args.get("hold", 0)) or 0),
                ),
                **self._kwargs(),
            ),
            "keyboard.hold": lambda: self.stub.HoldKey(
                pb.KeyHoldRequest(
                    key=str(args.get("key", "")),
                    ms=float(args.get("ms", args.get("hold_ms", args.get("duration", 0))) or 0),
                ),
                **self._kwargs(),
            ),
            "keyboard.paste": lambda: self.stub.PasteText(
                pb.PasteTextRequest(text=args.get("text", "")), **self._kwargs()
            ),
            "keyboard.check": lambda: self.stub.CheckKeys(
                pb.KeyCheckRequest(
                    keys=(
                        args["keys"]
                        if isinstance(args.get("keys"), list)
                        else [args.get("keys") or args.get("key") or ""]
                    )
                ),
                **self._kwargs(),
            ),
            "window.list": lambda: self.stub.ListWindows(
                pb.ListWindowsRequest(
                    title=args.get("title", ""),
                    process=args.get("process", args.get("proc", "")),
                    limit=int(args.get("limit", 0) or 0),
                    include_hidden=bool(args.get("include_hidden", False)),
                ),
                **self._kwargs(),
            ),
            "window.foreground": lambda: self.stub.GetForegroundWindow(empty, **self._kwargs()),
            "window.focus": lambda: self.stub.FocusWindow(
                self._focus_request(args), **self._kwargs()
            ),
            "screen.calibrate": lambda: self.stub.Calibrate(
                self._calibrate_request(args), **self._kwargs()
            ),
            "notify": lambda: self.stub.Notify(
                pb.NotifyRequest(
                    message=args.get("message", args.get("text", "")),
                    detail=args.get("detail", ""),
                    seconds=float(args.get("seconds", 0) or 0),
                    corner=args.get("corner", "br"),
                ),
                **self._kwargs(),
            ),
            "kuuki": lambda: self.stub.SendKuukiMessage(
                pb.KuukiMessageRequest(
                    json=json.dumps(args.get("message", args), ensure_ascii=False)
                ),
                **self._kwargs(),
            ),
        }
        if op not in table:
            raise ValueError(f"gRPC 客户端不支持 op {op!r}; 支持: {', '.join(sorted(table))}")
        message = table[op]()
        try:
            converted = self._to_dict(message)
        except Exception:
            return message
        return _unwrap_ack(converted)


def _unwrap_ack(payload: Any) -> Any:
    """把 ``Ack{ok, message}`` 拆回 WS 那种结构化字典。

    gRPC 侧**没有返回值**的那些 RPC (mouse.move / mouse.click / ping …) 统一用
    ``Ack`` 兜底, 而 handle 出来的结果被 ``json.dumps`` 塞进 ``message`` 一串 JSON;
    WS / PeerJS 是直接返回对象。控制端要拿三传输的结果互相比较, 形状必须一致,
    所以这里把 JSON 串拆回来 —— 拆不动就原样返回, 不会比之前更差。
    """
    if not isinstance(payload, dict) or set(payload) != {"ok", "message"}:
        return payload
    raw = payload.get("message")
    if not raw:
        return payload
    try:
        inner = json.loads(raw)
    except (TypeError, ValueError):
        return payload
    if not isinstance(inner, dict):
        return payload
    return {**inner, "ok": bool(payload.get("ok", True))}


# ================================================================ CLI


def _write_frame(directory: str, prefix: str, index: int, header: dict, payload: bytes) -> str:
    os.makedirs(directory, exist_ok=True)
    fmt = header.get("format", "png")
    path = os.path.join(directory, f"{prefix}_{index:04d}.{fmt}")
    with open(path, "wb") as handle:
        handle.write(payload)
    return path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m remote.client",
        description="kuuki remote 客户端 (WS / gRPC)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"kuuki remote client {VERSION}")
    sub = parser.add_subparsers(dest="transport", required=True)

    ws = sub.add_parser("ws", help="WebSocket 客户端")
    ws.add_argument("--url", default="ws://127.0.0.1:8765")
    ws.add_argument("--token", default=None)
    ws.add_argument("--timeout", type=float, default=30.0)
    _add_actions(ws)

    grpc_parser = sub.add_parser("grpc", help="gRPC 客户端")
    grpc_parser.add_argument("--target", default="127.0.0.1:50051")
    grpc_parser.add_argument("--token", default=None)
    grpc_parser.add_argument("--timeout", type=float, default=30.0)
    _add_actions(grpc_parser)

    # PeerJS: 服务端必须跑在**被控机器**上 (两端各自连 broker, 不需要端口)
    peer = sub.add_parser("peerjs", help="PeerJS 客户端 (连到被控机器)")
    peer.add_argument("--peer", required=True, help="被控端 id, 形如 kuuki-mouse-ABCDE")
    peer.add_argument("--token", default=None)
    peer.add_argument("--timeout", type=float, default=30.0)
    _add_actions(peer)
    return parser


def _add_actions(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("ping", help="连通性检查")
    sub.add_parser("info", help="服务/环境信息")

    op = sub.add_parser("op", help="发一条任意 op")
    op.add_argument("name")
    op.add_argument("--args", default="{}", help="JSON 参数")

    # 窗口: 先认窗口再视觉定位 —— 光看截图分不清"这块界面属于哪个应用"
    win = sub.add_parser("windows", help="列出被控端顶层窗口")
    win.add_argument("--title", default="", help="标题子串过滤 (不区分大小写)")
    win.add_argument("--process", default="", help="进程名或 pid 子串, 例如 msedge")
    win.add_argument("--limit", type=int, default=0, help="最多列几个 (0 = 不限)")
    win.add_argument("--include-hidden", action="store_true", help="连隐藏窗口一起列")

    # 显示器: 多屏机器上先看一眼虚拟桌面边界 (原点可以是负的), 再谈坐标校准
    mon = sub.add_parser("monitors", help="列出被控端显示器与虚拟桌面边界")
    mon.add_argument("--x", type=int, default=None, help="只查这个点在哪块屏上")
    mon.add_argument("--y", type=int, default=None, help="与 --x 一起给")

    cal = sub.add_parser("calibrate", help="实测图坐标<->鼠标坐标的换算 (会动鼠标)")
    cal.add_argument("--cols", type=int, default=3, help="靶点网格列数")
    cal.add_argument("--rows", type=int, default=3, help="靶点网格行数")
    cal.add_argument("--margin", type=float, default=0.12, help="留边比例 (贴边标记会被裁)")
    cal.add_argument("--settle", type=float, default=0.1, help="移过去后等多久再抓帧 (秒)")
    cal.add_argument("--tolerance", type=float, default=2.0, help="判定没偏的残差上限 (像素)")
    cal.add_argument("--max-width", type=int, default=0, help="顺带按这个宽度做缩放校准")
    cal.add_argument("--no-restore", action="store_true", help="校准后不把鼠标挪回原位")
    cal.add_argument("--save", default=None, help="把拟合结果存成 json, 供 locate 用")

    foc = sub.add_parser("focus", help="把某个窗口切到前台")
    foc.add_argument("--hwnd", type=int, default=None, help="直接给窗口句柄 (最可靠)")
    foc.add_argument("--title", default="", help="标题子串")
    foc.add_argument("--process", default="", help="进程名或 pid 子串")
    foc.add_argument("--index", type=int, default=0, help="命中多个时取第几个 (按 Z 序)")
    foc.add_argument("--wait", type=float, default=None, help="切换后最多等多久确认 (秒)")

    shot = sub.add_parser("screenshot", help="抓一帧存文件")
    shot.add_argument("path")
    _add_shot_args(shot)

    watch = sub.add_parser("watch", help="连续抓帧存目录")
    watch.add_argument("directory")
    watch.add_argument("--fps", type=float, default=2.0)
    watch.add_argument("--count", type=int, default=5)
    _add_shot_args(watch)

    stream = sub.add_parser("stream", help="gRPC 服务端流式截屏存目录")
    stream.add_argument("directory")
    stream.add_argument("--fps", type=float, default=2.0)
    stream.add_argument("--count", type=int, default=5)
    _add_shot_args(stream)

    loc = sub.add_parser("locate", help="在屏幕里定位目标 (颜色/模板/帧差/概览)")
    loc.add_argument("--describe", action="store_true", help="输出网格概览 (默认就是这个)")
    loc.add_argument("--dominant", action="store_true", help="列出主色调")
    loc.add_argument("--color", default=None, help="按颜色找色块: #1a73e8 或 26,115,232")
    loc.add_argument("--template", default=None, help="按形状找图标: 模板图路径")
    loc.add_argument("--saturated", action="store_true", help="找任何颜色鲜艳的图标/按钮")
    loc.add_argument("--diff-with", default=None, help="与这一帧比较, 找出变化的区域")
    loc.add_argument("--save-template", default=None, help="配合 --box 把区域存成模板")
    loc.add_argument("--box", default=None, help="x,y,w,h (给 --save-template 用)")
    loc.add_argument("--tol", type=int, default=24, help="颜色/帧差阈值")
    loc.add_argument("--min-pixels", type=int, default=60, help="小于这个面积的忽略")
    loc.add_argument("--threshold", type=float, default=0.85, help="模板匹配得分下限")
    loc.add_argument("--top", type=int, default=10, help="最多返回几个")
    loc.add_argument("--save-frame", default=None, help="把这帧存盘, 供下次 --diff-with")
    loc.add_argument("--calib", default=None, help="用 calibrate --save 的 json 换算坐标")
    _add_shot_args(loc)


def _parse_color(text: str) -> Tuple[int, int, int]:
    """``#1a73e8`` / ``1a73e8`` / ``26,115,232`` 三种写法都认。"""
    raw = text.strip().lstrip("#")
    if "," in raw:
        parts = [int(float(p.strip())) for p in raw.split(",")]
        if len(parts) != 3:
            raise ValueError("--color 需要 3 个通道: 26,115,232")
        return (parts[0], parts[1], parts[2])
    if len(raw) != 6:
        raise ValueError(f"--color 需要 6 位十六进制: #1a73e8，收到 {text!r}")
    return (int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16))


def _focus_args(args: argparse.Namespace) -> dict:
    """``focus`` 子命令 -> ``window.focus`` 的 args。

    ``--wait`` 没给时**不要**填 0: 传 0 等于"不等待确认", 而缺省是服务端默认的
    0.5 秒轮询 —— 前者会让 ``focused`` 永远是 False (切前台是异步的)。
    """
    out: Dict[str, Any] = {"title": args.title, "process": args.process}
    if args.hwnd is not None:
        out["hwnd"] = int(args.hwnd)
    out["index"] = int(args.index or 0)
    if args.wait is not None:
        out["wait"] = float(args.wait)
    if not out["title"] and not out["process"] and "hwnd" not in out:
        raise ValueError("focus 需要 --hwnd / --title / --process 之一")
    return out


def _monitors_args(args: argparse.Namespace) -> dict:
    """``monitors`` 子命令 -> ``screen.monitors`` 的 args。

    只给了 ``--x`` 或 ``--y`` 其中一个时按"没给"处理: 单点查询要两个都有意义,
    而 0 是合法坐标 (虚拟桌面原点就可能落在 0 上), 不能用 ``or`` 兜底。
    """
    if getattr(args, "x", None) is not None and getattr(args, "y", None) is not None:
        return {"x": args.x, "y": args.y}
    return {}


def _calibrate_args(args: argparse.Namespace) -> dict:
    """``calibrate`` 子命令 -> ``screen.calibrate`` 的 args。

    标量默认值这里的 0 与服务端默认一致, 所以可以直接传。``restore`` 反过来走
    ``--no-restore``: 默认行为是"校准完把光标挪回去", 写成一个需要显式关掉的
    开关比一个容易忘加的 flag 安全。
    """
    return {
        "cols": int(args.cols or 0),
        "rows": int(args.rows or 0),
        "margin": float(args.margin or 0),
        "settle": float(args.settle or 0),
        "tolerance": float(args.tolerance or 0),
        "max_width": int(args.max_width or 0),
        "restore": not bool(getattr(args, "no_restore", False)),
    }


def _print_calibration(result: dict, path: Optional[str] = None) -> None:
    """打印报告; 给了 ``path`` 就把 fit 存成 json 供 ``locate --calib`` 读。"""
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not path or not result.get("calibration"):
        return
    payload = {
        "calibration": result["calibration"],
        "screen": result.get("screen"),
        "declared_scale": result.get("declared_scale"),
        "max_width": result.get("max_width"),
        "verdict": result.get("verdict"),
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"校准结果已写入 {path} —— 之后 locate --calib {path} 就用它换算")


def _locate_result(
    args: argparse.Namespace, payload: bytes, header: Dict[str, Any]
) -> Dict[str, Any]:
    """在一帧上跑一次定位。三传输共用, 差异只在于 header 从哪来。

    PIL 在这里惰性导入 —— 只用 ping / op 的人不必背上这个依赖。
    """
    from remote import vision

    scale = vision.scale_of(header)
    image = vision.load(payload)
    region = None
    if getattr(args, "region", None):
        region = tuple(int(v) for v in args.region.split(","))
    # --calib 给了就用**实测**出来的换算 (缩放 + 平移), 没给才退回帧头声明的系数。
    # 两者差的那个平移项在多显示器上不是 0, 只用帧头会整体点偏。
    calib = _load_calibration(getattr(args, "calib", None))

    out: Dict[str, Any] = {
        "mode": "describe",
        "scale": round(scale, 4),
        "frame": {"width": image.width, "height": image.height},
        "source_width": header.get("source_width"),
        # 告诉调用方这次的 screen 坐标是哪来的 —— 两个数算出来不一样的时候,
        # 这是唯一能解释为什么的字段
        "coord_source": "calibration" if calib else "frame_header",
        "matches": [],
    }

    if args.save_template:
        if not args.box:
            raise ValueError("--save-template 必须配 --box x,y,w,h")
        box = tuple(int(v) for v in args.box.split(","))
        path = vision.save_template(image, box, args.save_template)
        out.update({"mode": "save-template", "path": path,
                    "box": {"x": box[0], "y": box[1], "w": box[2], "h": box[3]},
                    "size": {"w": image.width, "h": image.height}})
        return out

    if args.color:
        rects = vision.find_color(
            image, _parse_color(args.color), tol=args.tol, region=region,
            min_pixels=args.min_pixels, limit=args.top,
        )
        mode = "color"
    elif args.template:
        rects = vision.match_template(image, args.template, threshold=args.threshold,
                                      limit=args.top)
        mode = "template"
    elif args.saturated:
        rects = vision.find_saturated_blocks(
            image, min_saturation=args.tol, min_pixels=args.min_pixels,
            region=region, limit=args.top,
        )
        mode = "saturated"
    elif args.diff_with:
        rects = vision.diff(vision.load(args.diff_with), image, tol=args.tol,
                            min_pixels=args.min_pixels, limit=args.top)
        mode = "diff"
    elif args.dominant:
        out["mode"] = "dominant"
        out["matches"] = vision.dominant_colors(image, top=args.top)
        return out
    else:
        out["mode"] = "describe"
        blocks = vision.describe_grid(image, scale=scale)
        # 网格概览也会带 screen 坐标, 校准过就一并换成实测值 —— 否则同一份输出
        # 里两种换算混着, 用的人不知道该信哪个
        if calib is not None:
            for block in blocks:
                cx, cy = block["center"]["x"], block["center"]["y"]
                block["screen"] = dict(zip(("x", "y"), calib.to_screen(cx, cy)))
        out["matches"] = blocks
        return out

    if calib is not None:
        for rect in rects:
            cx, cy = rect.center
            screen_x, screen_y = calib.to_screen(cx, cy)
            item = rect.to_dict(scale)
            item["screen"] = {"x": screen_x, "y": screen_y}
            out["matches"].append(item)
        return out
    out["mode"] = mode
    out["matches"] = [r.to_dict(scale) for r in rects]
    return out


def _load_calibration(path: Optional[str]):
    """读一份 ``calibrate --save`` 产出的 json, 失败就当没给 (而不是崩掉)。"""
    if not path:
        return None
    from remote.calibrate import Calibration

    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise ValueError(f"读不了校准文件 {path}: {exc}") from exc
    if "calibration" not in data:
        raise ValueError(f"{path} 不像校准结果 (缺 calibration 字段)")
    return Calibration.from_dict(data["calibration"])


def _add_shot_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", default="png", choices=["png", "jpeg", "jpg", "webp"])
    parser.add_argument("--quality", type=int, default=80)
    parser.add_argument("--max-width", type=int, default=0)
    parser.add_argument("--max-height", type=int, default=0)
    parser.add_argument("--draw-cursor", action="store_true")
    parser.add_argument("--region", default=None, help="left,top,width,height")


def _shot_args(args: argparse.Namespace) -> dict:
    out: Dict[str, Any] = {"format": args.format, "quality": args.quality}
    if args.max_width:
        out["max_width"] = args.max_width
    if args.max_height:
        out["max_height"] = args.max_height
    if args.draw_cursor:
        out["draw_cursor"] = True
    if args.region:
        out["region"] = [int(v) for v in args.region.split(",")]
    return out


def _optional_at(args: dict) -> dict:
    """取出 args 里"想先定位再操作"的坐标, 转成 proto 的 optional 字段 kwargs。

    为什么不能直接塞 0: ``at_x`` / ``at_y`` 是 ``optional``, 给了才写入; 直接
    ``at_x=args.get("x", 0)`` 会把"没给坐标"变成"定位到 x=0", 点在屏幕左上角。
    显式传 0 反而是合法的 —— 所以它必须真的进被调(req.HasField() 要 True)。
    """
    out: Dict[str, int] = {}
    if args.get("x") is not None:
        out["at_x"] = int(args["x"])
    if args.get("y") is not None:
        out["at_y"] = int(args["y"])
    return out


def _optional_int(args: dict, *names: str) -> dict:
    """``_optional_number`` 的 int 版: proto 里是 int32 的字段要用它。

    不能直接拿 float 版本塞 int32 字段 (类型检查会拒), 也不能塞 0 —— 这里的
    x / y 是虚拟桌面坐标, 0 是合法值 (而且副屏坐标还会是负的)。
    """
    out: Dict[str, int] = {}
    for name in names:
        if args.get(name) is not None:
            out[name] = int(args[name])
    return out


def _optional_number(args: dict, *names: str) -> dict:
    """取出 args 里若干数值参数, 只把"真的给了"的写进 proto 的 optional 字段。

    与 ``_optional_at`` 一个道理, 只是字段是浮点: 显式 0 必须真的写进去
    (``--interval 0`` = 连击之间不等, ``--hold 0`` = 按下即松开), 而"没给"
    要留给服务端默认值。``float(args.get(k) or default)`` 做不到这一点。
    """
    out: Dict[str, float] = {}
    for name in names:
        if args.get(name) is not None:
            out[name] = float(args[name])
    return out


def _combo_keys(args: dict) -> list:
    """组合键参数归一成键名列表。

    protobuf 那边是 ``repeated string keys``, 传不了 ``"ctrl+shift+s"`` 这种写法,
    所以必须在这里拆开 —— 否则它会被当成**一个**叫 ``ctrl+shift+s`` 的键,
    服务端解析报 "未知键名"。WebSocket 侧传字符串本来就能走 service 的拆分逻辑,
    两边行为对齐靠的就是这里。
    """
    raw = args.get("keys") or args.get("combo") or args.get("key") or ""
    if isinstance(raw, str):
        parts = [p for p in raw.replace(" ", "").split("+") if p]
        return parts or [""]
    return [str(k) for k in raw]


async def _run_ws(args: argparse.Namespace) -> int:
    async with WsClient(args.url, token=args.token, timeout=args.timeout) as client:
        if args.action in ("ping", "info"):
            print(json.dumps(await client.call(args.action), ensure_ascii=False, indent=2))
            return 0
        if args.action == "op":
            payload = json.loads(args.args)
            print(json.dumps(await client.call(args.name, payload), ensure_ascii=False, indent=2))
            return 0
        if args.action == "windows":
            result = await client.call(
                "window.list",
                {
                    "title": args.title,
                    "process": args.process,
                    "limit": args.limit,
                    "include_hidden": args.include_hidden,
                },
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.action == "focus":
            result = await client.call("window.focus", _focus_args(args))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.action == "monitors":
            result = await client.call("screen.monitors", _monitors_args(args))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.action == "calibrate":
            result = await client.call("screen.calibrate", _calibrate_args(args))
            _print_calibration(result, args.save)
            return 0
        if args.action == "screenshot":
            header, payload = await client.screenshot(_shot_args(args))
            path = args.path
            if not os.path.splitext(path)[1]:
                path = f"{path}.{header.get('format', 'png')}"
            with open(path, "wb") as handle:
                handle.write(payload)
            print(json.dumps({**header, "path": path}, ensure_ascii=False, indent=2))
            return 0
        if args.action == "watch":
            started = time.time()

            def on_frame(header, payload):
                path = _write_frame(args.directory, "frame", header.get("seq", 0), header, payload)
                print(f"  #{header.get('seq')} {header.get('width')}x{header.get('height')} "
                      f"{len(payload)}B -> {path}")
                return True

            count = await client.watch(_shot_args(args) | {"fps": args.fps}, on_frame, args.count)
            print(f"共 {count} 帧, 用时 {time.time() - started:.1f}s")
            return 0
        if args.action == "locate":
            header, payload = await client.screenshot(_shot_args(args))
            if args.save_frame:
                with open(args.save_frame, "wb") as handle:
                    handle.write(payload)
            result = _locate_result(args, payload, header)
            if args.save_frame:
                result["saved_frame"] = args.save_frame
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
    return 2


def _run_grpc(args: argparse.Namespace) -> int:
    with GrpcClient(args.target, token=args.token, timeout=args.timeout) as client:
        if args.action in ("ping", "info"):
            print(json.dumps(client.call(args.action), ensure_ascii=False, indent=2))
            return 0
        if args.action == "op":
            print(json.dumps(
                client.call(args.name, json.loads(args.args)),
                ensure_ascii=False, indent=2,
            ))
            return 0
        if args.action == "windows":
            result = client.call(
                "window.list",
                {
                    "title": args.title,
                    "process": args.process,
                    "limit": args.limit,
                    "include_hidden": args.include_hidden,
                },
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.action == "focus":
            result = client.call("window.focus", _focus_args(args))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.action == "monitors":
            result = client.call("screen.monitors", _monitors_args(args))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.action == "calibrate":
            result = client.call("screen.calibrate", _calibrate_args(args))
            _print_calibration(result, args.save)
            return 0
        if args.action == "screenshot":
            payload = client.screenshot(_shot_args(args))
            with open(args.path, "wb") as handle:
                handle.write(payload)
            image = client.last_image
            print(json.dumps(
                {
                    "path": args.path,
                    "bytes": len(payload),
                    "format": image.format,
                    "width": image.width,
                    "height": image.height,
                    "backend": "pillow",
                    "duration_ms": round(image.duration_ms, 2),
                },
                ensure_ascii=False,
                indent=2,
            ))
            return 0
        if args.action == "stream":
            started = time.time()
            counter = {"n": 0}

            def on_frame(image):
                counter["n"] += 1
                header = {"format": image.format, "seq": counter["n"],
                          "width": image.width, "height": image.height}
                path = _write_frame(args.directory, "grpc", counter["n"], header, image.data)
                print(f"  #{counter['n']} {image.width}x{image.height} "
                      f"{len(image.data)}B -> {path}")
                return True

            frames = client.stream(_shot_args(args) | {"fps": args.fps}, on_frame, args.count)
            print(f"共 {frames} 帧, 用时 {time.time() - started:.1f}s")
            return 0
        if args.action == "locate":
            payload = client.screenshot(_shot_args(args))
            if args.save_frame:
                with open(args.save_frame, "wb") as handle:
                    handle.write(payload)
            image = client.last_image
            header = {
                "width": image.width,
                "height": image.height,
                "source_width": image.source_width,
                "source_height": image.source_height,
                "format": image.format,
            }
            result = _locate_result(args, payload, header)
            if args.save_frame:
                result["saved_frame"] = args.save_frame
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
    return 2


async def _run_peerjs(args: argparse.Namespace) -> int:
    """PeerJS 客户端: 与服务端同构的 action 集合。"""
    from remote.peerjs_client import PeerJsClient

    client = PeerJsClient(args.peer, token=args.token, timeout=args.timeout)
    await client.connect()
    try:
        if args.action in ("ping", "info"):
            print(json.dumps(await client.call(args.action), ensure_ascii=False, indent=2))
            return 0
        if args.action == "op":
            payload = json.loads(args.args)
            print(
                json.dumps(
                    await client.call(args.name, payload), ensure_ascii=False, indent=2
                )
            )
            return 0
        if args.action == "windows":
            result = await client.call(
                "window.list",
                {
                    "title": args.title,
                    "process": args.process,
                    "limit": args.limit,
                    "include_hidden": args.include_hidden,
                },
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.action == "focus":
            result = await client.call("window.focus", _focus_args(args))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.action == "monitors":
            result = await client.call("screen.monitors", _monitors_args(args))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.action == "calibrate":
            result = await client.call("screen.calibrate", _calibrate_args(args))
            _print_calibration(result, args.save)
            return 0
        if args.action == "screenshot":
            payload = await client.screenshot(_shot_args(args))
            with open(args.path, "wb") as handle:
                handle.write(payload)
            print(json.dumps(
                {"path": args.path, "bytes": len(payload)},
                ensure_ascii=False, indent=2,
            ))
            return 0
        if args.action == "locate":
            payload = await client.screenshot(_shot_args(args))
            if args.save_frame:
                with open(args.save_frame, "wb") as handle:
                    handle.write(payload)
            # PeerJS 的帧头留在 last_capture 里 (它是 JSON 通道, 不像 WS 有二进制帧);
            # 这里拼成和 WS / gRPC 同形状才能复用同一套坐标换算。少了
            # source_width 就还原不出真实坐标 —— 宁可不说话也不返回错坐标。
            header = dict(getattr(client, "last_capture", None) or {})
            if not header.get("source_width"):
                print("失败: PeerJS 这一帧没有 source_width, 换算不出真实屏幕坐标。",
                      file=sys.stderr)
                return 1
            result = _locate_result(args, payload, header)
            if args.save_frame:
                result["saved_frame"] = args.save_frame
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.action == "watch":
            # PeerJS 侧没有服务端推流, 用轮询实现 (op 语义一致)
            started = time.time()
            for index in range(1, max(1, args.count) + 1):
                payload = await client.screenshot(_shot_args(args))
                header = {"format": args.format, "seq": index}
                path = _write_frame(args.directory, "peerjs", index, header, payload)
                print(f"  #{index} {len(payload)}B -> {path}")
                if index < args.count:
                    await asyncio.sleep(1.0 / max(0.1, args.fps))
            print(f"共 {args.count} 帧, 用时 {time.time() - started:.1f}s")
            return 0
    finally:
        await client.close()
    return 2


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.transport == "ws":
            return asyncio.run(_run_ws(args))
        if args.transport == "peerjs":
            return asyncio.run(_run_peerjs(args))
        return _run_grpc(args)
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"失败: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
