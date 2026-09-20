"""gRPC 服务端: ``kuuki.remote.v1.RemoteControl`` 的实现。

与 WebSocket 侧共用 ``RemoteService`` (见 ``service.py``), 这里只做两件事:
① 把 protobuf 请求翻译成 service 的 args dict; ② 把返回的 dict 填回 protobuf。

比 WebSocket 多出来的能力是 **服务端流式截屏** (``StreamScreenshots``),
适合做低帧率的屏幕监看, 不用自己轮询。

存根生成: ``bash remote/proto/gen_proto.sh`` (proto 见 ``proto/kuuki_remote.proto``)。
"""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import time
from concurrent import futures
from typing import Optional

import grpc

from .service import RemoteError, RemoteService

try:  # 生成存根缺失时给出可执行的提示, 而不是 ImportError 堆栈
    from .proto import kuuki_remote_pb2 as pb
    from .proto import kuuki_remote_pb2_grpc as pb_grpc
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "gRPC 存根不存在或导入失败; 请在仓库根目录执行 `bash remote/proto/gen_proto.sh` "
        f"(原始错误: {exc})"
    ) from exc

log = logging.getLogger("kuuki.remote.grpc")

__all__ = ["GrpcServer", "RemoteControlServicer"]

_STATUS_BY_CODE = {
    "bad_request": grpc.StatusCode.INVALID_ARGUMENT,
    "unknown_op": grpc.StatusCode.UNIMPLEMENTED,
    "unauthorized": grpc.StatusCode.UNAUTHENTICATED,
    "unsupported": grpc.StatusCode.FAILED_PRECONDITION,
    "backend_unavailable": grpc.StatusCode.UNAVAILABLE,
    "internal": grpc.StatusCode.INTERNAL,
}


@contextlib.contextmanager
def _translate(context):
    """把业务异常翻成 gRPC status。"""
    try:
        yield
    except RemoteError as exc:
        context.abort(_STATUS_BY_CODE.get(exc.code, grpc.StatusCode.INTERNAL), exc.message)
    except ValueError as exc:
        context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
    except Exception as exc:  # noqa: BLE001
        context.abort(grpc.StatusCode.INTERNAL, f"{exc.__class__.__name__}: {exc}")


def _point_to(value: Optional[dict]):
    if not value:
        return None
    return pb.Point(x=int(value.get("x", 0)), y=int(value.get("y", 0)))


def _rect_from(value: Optional[dict]):
    if not value:
        return None
    return pb.Rect(
        left=int(value.get("left", 0)),
        top=int(value.get("top", 0)),
        width=int(value.get("width", 0)),
        height=int(value.get("height", 0)),
    )


class RemoteControlServicer(pb_grpc.RemoteControlServicer):
    """所有方法都委托给 ``RemoteService``。"""

    def __init__(self, service: RemoteService, token: Optional[str] = None):
        self.service = service
        self.token = token

    # ---------------- 鉴权 ----------------
    def _check_auth(self, context) -> None:
        if not self.token:
            return
        metadata = dict(context.invocation_metadata() or ())
        provided = metadata.get("authorization", "") or ""
        if provided.lower().startswith("bearer "):
            provided = provided[7:].strip()
        provided = provided or metadata.get("x-kuuki-token", "") or ""
        if not hmac.compare_digest(str(provided), str(self.token)):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid token")

    # ---------------- 元信息 ----------------
    def Ping(self, request, context):
        self._check_auth(context)
        with _translate(context):
            result = self.service.handle("ping", {})
        return pb.Ack(ok=True, message=json.dumps(result, ensure_ascii=False))

    def GetInfo(self, request, context):
        self._check_auth(context)
        with _translate(context):
            info = self.service.handle("info", {})
        message = pb.Info(
            version=info.get("version", ""),
            os=info.get("os", ""),
            platform=info.get("platform", ""),
            python=info.get("python", ""),
            hostname=info.get("hostname", ""),
            user=info.get("user", ""),
            cwd=info.get("cwd", ""),
            display=info.get("display", ""),
            clipboard_tool=info.get("clipboard_tool") or "",
            token_required=bool(info.get("token_required")),
            uptime_s=float(info.get("uptime_s", 0.0)),
        )
        cursor = _point_to(info.get("cursor"))
        if cursor is not None:
            message.cursor.CopyFrom(cursor)
        message.capabilities.extend(info.get("capabilities", []))
        return message

    # ---------------- 截屏 ----------------
    @staticmethod
    def _shot_args(request) -> dict:
        args = {
            "format": request.format or "png",
            "quality": int(request.quality) or 80,
            "draw_cursor": bool(request.draw_cursor),
        }
        if request.HasField("region"):
            args["region"] = {
                "left": request.region.left,
                "top": request.region.top,
                "width": request.region.width,
                "height": request.region.height,
            }
        if request.max_width:
            args["max_width"] = int(request.max_width)
        if request.max_height:
            args["max_height"] = int(request.max_height)
        if request.scale:
            args["scale"] = float(request.scale)
        return args

    def _to_image(self, capture, data: bytes) -> "pb.Image":
        image = pb.Image(
            data=data,
            format=capture.format,
            width=capture.width,
            height=capture.height,
            source_width=capture.source_width,
            source_height=capture.source_height,
            scale=capture.scale,
            captured_at=capture.captured_at,
            duration_ms=capture.duration_ms,
        )
        rect = _rect_from(capture.region.to_dict() if capture.region else None)
        if rect is not None:
            image.region.CopyFrom(rect)
        if capture.cursor:
            image.has_cursor = True
            image.cursor_in_frame = bool(capture.cursor.get("in_frame"))
            image.cursor.CopyFrom(
                pb.Point(x=int(capture.cursor.get("x", 0)), y=int(capture.cursor.get("y", 0)))
            )
        return image

    def Screenshot(self, request, context):
        self._check_auth(context)
        with _translate(context):
            capture, data = self.service.capture(self._shot_args(request))
        return self._to_image(capture, data)

    def StreamScreenshots(self, request, context):
        self._check_auth(context)
        args = self._shot_args(request.shot) if request.HasField("shot") else {}
        fps = float(request.fps or 0.0)
        interval = 1.0 / fps if fps > 0 else max(0.01, (int(request.interval_ms) or 500) / 1000.0)
        count = int(request.count)
        sent = 0
        while context.is_active():
            if count and sent >= count:
                break
            try:
                capture, data = self.service.capture(args)
            except RemoteError as exc:
                context.abort(_STATUS_BY_CODE.get(exc.code, grpc.StatusCode.INTERNAL), exc.message)
                return
            except Exception as exc:  # noqa: BLE001
                context.abort(grpc.StatusCode.INTERNAL, f"{exc.__class__.__name__}: {exc}")
                return
            yield self._to_image(capture, data)
            sent += 1
            if count and sent >= count:
                break
            time.sleep(interval)
        log.debug("StreamScreenshots 结束, 共 %s 帧", sent)

    # ---------------- 鼠标 ----------------
    def GetMousePosition(self, request, context):
        self._check_auth(context)
        with _translate(context):
            result = self.service.handle("mouse.position", {})
        return pb.Point(x=int(result["x"]), y=int(result["y"]))

    def MoveMouse(self, request, context):
        self._check_auth(context)
        with _translate(context):
            result = self.service.handle(
                "mouse.move", {"x": request.x, "y": request.y, "duration": request.duration}
            )
        return pb.Ack(ok=True, message=json.dumps(result, ensure_ascii=False))

    def MoveMouseRelative(self, request, context):
        self._check_auth(context)
        with _translate(context):
            result = self.service.handle(
                "mouse.move_rel", {"dx": request.dx, "dy": request.dy, "duration": request.duration}
            )
        return pb.Ack(ok=True, message=json.dumps(result, ensure_ascii=False))

    def ClickMouse(self, request, context):
        self._check_auth(context)
        with _translate(context):
            payload = {
                "button": request.button or "left",
                "clicks": int(request.clicks) or 1,
                "interval": request.interval or 0.05,
            }
            # at_x / at_y 是 optional: 只在显式给了时才传下去, 让"原点也可以是
            # 目标"这件事在 gRPC 侧与 WS 侧一致 (鼠标 click 的坐标语义: 先移动再点)
            if request.HasField("at_x"):
                payload["x"] = int(request.at_x)
            if request.HasField("at_y"):
                payload["y"] = int(request.at_y)
            result = self.service.handle("mouse.click", payload)
        return pb.Ack(ok=True, message=json.dumps(result, ensure_ascii=False))

    def MouseDown(self, request, context):
        self._check_auth(context)
        with _translate(context):
            result = self.service.handle("mouse.down", {"button": request.button or "left"})
        return pb.Ack(ok=True, message=json.dumps(result, ensure_ascii=False))

    def MouseUp(self, request, context):
        self._check_auth(context)
        with _translate(context):
            result = self.service.handle("mouse.up", {"button": request.button or "left"})
        return pb.Ack(ok=True, message=json.dumps(result, ensure_ascii=False))

    @staticmethod
    def _scroll_args(request) -> dict:
        """ScrollRequest -> service 的 args。

        ``interval`` / ``at_x`` / ``at_y`` 是 ``optional``: 只有客户端真的设了才传,
        这样"没给"落到服务的默认值、"显式给 0"保持 0 —— 与 WebSocket 侧
        (args 里有没有这个键) 完全一致。用 ``request.interval`` 的真假来判断会把
        显式 0 当成没给。
        """
        args: dict = {"dx": request.dx, "dy": request.dy}
        if request.steps > 1:
            args["steps"] = int(request.steps)
        if request.HasField("interval"):
            args["interval"] = float(request.interval)
        if request.HasField("at_x"):
            args["x"] = int(request.at_x)
        if request.HasField("at_y"):
            args["y"] = int(request.at_y)
        return args

    def Scroll(self, request, context):
        self._check_auth(context)
        with _translate(context):
            result = self.service.handle("mouse.scroll", self._scroll_args(request))
        return pb.Ack(ok=True, message=json.dumps(result, ensure_ascii=False))

    def ScrollHorizontal(self, request, context):
        self._check_auth(context)
        with _translate(context):
            result = self.service.handle("mouse.scroll_h", self._scroll_args(request))
        return pb.Ack(ok=True, message=json.dumps(result, ensure_ascii=False))

    def Drag(self, request, context):
        self._check_auth(context)
        args = {"button": request.button or "left"}
        # duration 是 optional: 没给用 0.3, 显式给 0 就是"直接跳到终点"。
        # 写成 ``request.duration or 0.3`` 会把显式 0 悄悄换成 0.3, 于是 gRPC 走
        # 插值轨迹、WS 走瞬移 —— 同样的参数两边行为不同。
        args["duration"] = float(request.duration) if request.HasField("duration") else 0.3
        if request.points:
            args["points"] = [[point.x, point.y] for point in request.points]
        else:
            args.update(
                {"x1": request.x1, "y1": request.y1, "x2": request.x2, "y2": request.y2}
            )
        with _translate(context):
            result = self.service.handle("mouse.drag", args)
        return pb.Ack(ok=True, message=json.dumps(result, ensure_ascii=False))

    # ---------------- 键盘 ----------------
    def TypeText(self, request, context):
        self._check_auth(context)
        with _translate(context):
            result = self.service.handle(
                "keyboard.type", {"text": request.text, "interval": request.interval}
            )
        return pb.Ack(ok=True, message=json.dumps(result, ensure_ascii=False))

    def PressKey(self, request, context):
        self._check_auth(context)
        with _translate(context):
            result = self.service.handle(
                "keyboard.key",
                {
                    "key": request.key,
                    "action": request.action or "tap",
                    "modifiers": list(request.modifiers),
                },
            )
        return pb.Ack(ok=True, message=json.dumps(result, ensure_ascii=False))

    def Hotkey(self, request, context):
        self._check_auth(context)
        with _translate(context):
            result = self.service.handle("keyboard.hotkey", {"keys": list(request.keys)})
        return pb.Ack(ok=True, message=json.dumps(result, ensure_ascii=False))

    def Combo(self, request, context):
        self._check_auth(context)
        with _translate(context):
            result = self.service.handle(
                "keyboard.combo",
                {"keys": list(request.keys), "hold_ms": float(request.hold_ms or 0.0)},
            )
        return pb.Ack(ok=True, message=json.dumps(result, ensure_ascii=False))

    def HoldKey(self, request, context):
        self._check_auth(context)
        with _translate(context):
            result = self.service.handle(
                "keyboard.hold", {"key": request.key, "ms": float(request.ms or 0.0)}
            )
        return pb.Ack(ok=True, message=json.dumps(result, ensure_ascii=False))

    def PasteText(self, request, context):
        self._check_auth(context)
        with _translate(context):
            result = self.service.handle("keyboard.paste", {"text": request.text})
        return pb.Ack(ok=True, message=json.dumps(result, ensure_ascii=False))

    def CheckKeys(self, request, context):
        self._check_auth(context)
        with _translate(context):
            result = self.service.handle("keyboard.check", {"keys": list(request.keys)})
        reply = pb.KeyCheckReply()
        for item in result.get("keys", []):
            reply.keys.append(
                pb.KeyCheckResult(
                    key=item.get("key", ""),
                    supported=bool(item.get("supported")),
                    reason=item.get("reason") or "",
                )
            )
        return reply

    # ---------------- 窗口 ----------------
    @staticmethod
    def _window_to(item: Optional[dict]):
        """service 返回的窗口 dict -> ``WindowInfo``; 空输入返回 None。"""
        if not item:
            return None
        message = pb.WindowInfo(
            hwnd=int(item.get("hwnd", 0)),
            title=item.get("title", ""),
            process=item.get("process", ""),
            pid=int(item.get("pid", 0)),
            visible=bool(item.get("visible")),
            minimized=bool(item.get("minimized")),
            foreground=bool(item.get("foreground")),
        )
        rect = _rect_from(item.get("rect"))
        if rect is not None:
            message.rect.CopyFrom(rect)
        return message

    def ListWindows(self, request, context):
        self._check_auth(context)
        with _translate(context):
            result = self.service.handle(
                "window.list",
                {
                    "title": request.title,
                    "process": request.process,
                    "limit": int(request.limit),
                    "include_hidden": bool(request.include_hidden),
                },
            )
        reply = pb.ListWindowsReply(count=int(result.get("count", 0)))
        for item in result.get("windows", []):
            message = self._window_to(item)
            if message is not None:
                reply.windows.append(message)
        return reply

    def GetForegroundWindow(self, request, context):
        self._check_auth(context)
        with _translate(context):
            result = self.service.handle("window.foreground", {})
        # service 返回的是平铺的窗口 dict (与 WS 一致), 直接填进 WindowInfo;
        # 没有前台窗口 (锁屏) 时 hwnd=0 一整套零值, 照样能填 —— 就是"没有"
        return self._window_to(result) or pb.WindowInfo()

    def FocusWindow(self, request, context):
        self._check_auth(context)
        args = {
            "title": request.title,
            "process": request.process,
            "index": int(request.index),
        }
        # handle=0 视为"没给": HWND 不会是 0, 直接塞进去会变成"找 hwnd 0"
        if request.hwnd:
            args["hwnd"] = int(request.hwnd)
        # wait 是 optional: 没给 = 用服务默认 0.5s, 显式给 0 = 不等待确认
        if request.HasField("wait"):
            args["wait"] = float(request.wait)
        with _translate(context):
            result = self.service.handle("window.focus", args)
        reply = pb.FocusWindowReply(
            focused=bool(result.get("focused")),
            hwnd=int(result.get("hwnd", 0)),
            title=result.get("title", ""),
            process=result.get("process", ""),
            pid=int(result.get("pid", 0)),
            method=result.get("method", ""),
            matched=int(result.get("matched", 0)),
        )
        rect = _rect_from(result.get("rect"))
        if rect is not None:
            reply.rect.CopyFrom(rect)
        return reply

    # ---------------- 被控端角标 ----------------
    def Notify(self, request, context):
        self._check_auth(context)
        args = {"message": request.message, "detail": request.detail}
        # seconds 是普通 double: 不给就是 0, 而"给 0"在 service 里也是 0 ——
        # 两边都收不到"缺省 6 秒"。所以这里反过来: 只有真的给了正数才放进 args,
        # 让"没给"落到 service 的默认, 与 WS 侧参数缺省一致。
        if request.seconds > 0:
            args["seconds"] = float(request.seconds)
        # corner 同理: proto3 的 string 分不清"没给"与"显式空串", 而 WS 侧不给
        # 就是默认 br —— 这里补成 br, 让两边缺省行为一致 (显式给非法值照样报错)
        args["corner"] = request.corner or "br"
        with _translate(context):
            result = self.service.handle("notify", args)
        return pb.Ack(ok=True, message=json.dumps(result, ensure_ascii=False))

    # ---------------- kuuki 老协议 ----------------
    def SendKuukiMessage(self, request, context):
        self._check_auth(context)
        with _translate(context):
            try:
                message = json.loads(request.json or "{}")
            except json.JSONDecodeError as exc:
                raise RemoteError("bad_request", f"json 解析失败: {exc}")
            result = self.service.handle("kuuki", {"message": message})
        return pb.KuukiMessageReply(
            handled=bool(result.get("handled")),
            json=json.dumps(result, ensure_ascii=False),
        )


class GrpcServer:
    """gRPC 服务端生命周期封装 (``start()`` 非阻塞, 自己起线程池)。"""

    def __init__(
        self,
        service: RemoteService,
        host: str = "127.0.0.1",
        port: int = 50051,
        token: Optional[str] = None,
        max_workers: int = 8,
    ):
        self.service = service
        self.host = host
        self.port = port
        self.token = token
        self.max_workers = max_workers
        self._server: Optional[grpc.Server] = None
        self.bound_port: Optional[int] = None

    def start(self) -> int:
        options = [
            ("grpc.max_send_message_length", 64 * 1024 * 1024),
            ("grpc.max_receive_message_length", 16 * 1024 * 1024),
        ]
        self._server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=self.max_workers), options=options
        )
        pb_grpc.add_RemoteControlServicer_to_server(
            RemoteControlServicer(self.service, token=self.token), self._server
        )
        address = f"{self.host}:{self.port}"
        bound = self._server.add_insecure_port(address)
        if not bound:
            raise RuntimeError(f"gRPC 无法绑定 {address}")
        self._server.start()
        self.bound_port = bound
        log.info("gRPC 服务已启动: %s", address)
        return bound

    def stop(self, grace: float = 1.0) -> None:
        if self._server is not None:
            self._server.stop(grace)
            self._server = None

    def wait_for_termination(self, timeout: Optional[float] = None) -> None:
        if self._server is not None:
            self._server.wait_for_termination(timeout)

    def describe(self) -> dict:
        return {
            "transport": "grpc",
            "address": f"{self.host}:{self.bound_port or self.port}",
            "host": self.host,
            "port": self.bound_port or self.port,
            "service": "kuuki.remote.v1.RemoteControl",
            "token_required": bool(self.token),
        }
