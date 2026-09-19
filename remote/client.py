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

带 token 时加 ``--token XXX`` (WS 会拼进 URL, gRPC 会放进 metadata)。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any, Dict, Optional

# 允许 `python remote/client.py` 直接跑
if __package__ in (None, ""):  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from remote.service import VERSION  # noqa: E402
from remote.ws_server import decode_frame  # noqa: E402

try:
    from websockets.asyncio.client import connect as ws_connect
except Exception:  # pragma: no cover
    from websockets.client import connect as ws_connect


# ================================================================ WebSocket


class WsClient:
    """极简 WebSocket 客户端: 一次请求一次响应 + 二进制截屏帧。"""

    def __init__(self, url: str = "ws://127.0.0.1:8765", token: Optional[str] = None, timeout: float = 30.0):
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
            self.ws = await ws_connect(url, additional_headers=headers or None, max_size=64 * 1024 * 1024)
        except TypeError:  # pragma: no cover - websockets 13 及更早
            self.ws = await ws_connect(url, extra_headers=headers or None, max_size=64 * 1024 * 1024)
        return self

    async def __aexit__(self, *exc) -> None:
        if self.ws is not None:
            await self.ws.close()
            self.ws = None

    async def _send(self, op: str, args: Optional[dict] = None, req_id: Optional[int] = None) -> int:
        self._next_id += 1
        rid = self._next_id if req_id is None else req_id
        await self.ws.send(json.dumps({"id": rid, "op": op, "args": args or {}}, ensure_ascii=False))
        return rid

    async def call(self, op: str, args: Optional[dict] = None) -> dict:
        """发一条请求, 返回 result; 服务端报错则抛 RuntimeError。"""
        rid = await self._send(op, args)
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
            return msg.get("result") or {}

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

    def __init__(self, target: str = "127.0.0.1:50051", token: Optional[str] = None, timeout: float = 30.0):
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
        # 默认 MessageToDict 会把字段转成 lowerCamelCase, 与 WS 侧的 snake_case 不一致;
        # 这里保留 proto 字段名, 让两种传输吐出来的 JSON 长得一样。
        self._to_dict = lambda message: MessageToDict(message, preserving_proto_field_name=True)

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
            monitor=int(args.get("monitor", 0)),
            max_width=int(args.get("max_width", 0) or 0),
            max_height=int(args.get("max_height", 0) or 0),
            scale=float(args.get("scale", 0) or 0),
            draw_cursor=bool(args.get("draw_cursor", False)),
        )
        rect = self._rect(args.get("region"))
        if rect is not None:
            request.region.CopyFrom(rect)
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
            "screen.monitors": lambda: self.stub.GetMonitors(empty, **self._kwargs()),
            "screen.screenshot": lambda: self.stub.Screenshot(self._shot_request(args), **self._kwargs()),
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
                    interval=float(args.get("interval", 0.05) or 0.05),
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
                pb.ScrollRequest(dx=int(args.get("dx", 0)), dy=int(args.get("dy", 0))),
                **self._kwargs(),
            ),
            "mouse.drag": lambda: self.stub.Drag(
                pb.DragRequest(
                    x1=int(args.get("x1", 0)),
                    y1=int(args.get("y1", 0)),
                    x2=int(args.get("x2", 0)),
                    y2=int(args.get("y2", 0)),
                    button=args.get("button", "left"),
                    duration=float(args.get("duration", 0.3) or 0.3),
                ),
                **self._kwargs(),
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
                pb.HotkeyRequest(
                    keys=args["keys"] if isinstance(args.get("keys"), list) else [args.get("keys", "")]
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
            "kuuki": lambda: self.stub.SendKuukiMessage(
                pb.KuukiMessageRequest(json=json.dumps(args.get("message", args), ensure_ascii=False)),
                **self._kwargs(),
            ),
        }
        if op not in table:
            raise ValueError(f"gRPC 客户端不支持 op {op!r}; 支持: {', '.join(sorted(table))}")
        message = table[op]()
        try:
            return self._to_dict(message)
        except Exception:
            return message


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
    return parser


def _add_actions(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("ping", help="连通性检查")
    sub.add_parser("info", help="服务/环境信息")

    op = sub.add_parser("op", help="发一条任意 op")
    op.add_argument("name")
    op.add_argument("--args", default="{}", help="JSON 参数")

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


def _add_shot_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", default="png", choices=["png", "jpeg", "jpg", "webp"])
    parser.add_argument("--quality", type=int, default=80)
    parser.add_argument("--max-width", type=int, default=0)
    parser.add_argument("--max-height", type=int, default=0)
    parser.add_argument("--monitor", type=int, default=0)
    parser.add_argument("--draw-cursor", action="store_true")
    parser.add_argument("--region", default=None, help="left,top,width,height")


def _shot_args(args: argparse.Namespace) -> dict:
    out: Dict[str, Any] = {"format": args.format, "quality": args.quality, "monitor": args.monitor}
    if args.max_width:
        out["max_width"] = args.max_width
    if args.max_height:
        out["max_height"] = args.max_height
    if args.draw_cursor:
        out["draw_cursor"] = True
    if args.region:
        out["region"] = [int(v) for v in args.region.split(",")]
    return out


async def _run_ws(args: argparse.Namespace) -> int:
    async with WsClient(args.url, token=args.token, timeout=args.timeout) as client:
        if args.action in ("ping", "info"):
            print(json.dumps(await client.call(args.action), ensure_ascii=False, indent=2))
            return 0
        if args.action == "op":
            payload = json.loads(args.args)
            print(json.dumps(await client.call(args.name, payload), ensure_ascii=False, indent=2))
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
    return 2


def _run_grpc(args: argparse.Namespace) -> int:
    with GrpcClient(args.target, token=args.token, timeout=args.timeout) as client:
        if args.action in ("ping", "info"):
            print(json.dumps(client.call(args.action), ensure_ascii=False, indent=2))
            return 0
        if args.action == "op":
            print(json.dumps(client.call(args.name, json.loads(args.args)), ensure_ascii=False, indent=2))
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
                    "backend": image.backend,
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
                print(f"  #{counter['n']} {image.width}x{image.height} {len(image.data)}B -> {path}")
                return True

            frames = client.stream(_shot_args(args) | {"fps": args.fps}, on_frame, args.count)
            print(f"共 {frames} 帧, 用时 {time.time() - started:.1f}s")
            return 0
    return 2


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.transport == "ws":
            return asyncio.run(_run_ws(args))
        return _run_grpc(args)
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"失败: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
