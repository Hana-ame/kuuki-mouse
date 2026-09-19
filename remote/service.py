"""远程控制的服务层: 一个 op 注册表, WS 与 gRPC 两种传输共用。

为什么要有这一层
----------------
WebSocket 和 gRPC 只是**传输**, 真正的动作 (移动鼠标 / 输入 / 截屏) 只应该有一份
实现。两边都调用 ``RemoteService.handle(op, args)``, 返回 JSON 可序列化的 dict;
gRPC 侧再把 dict 映射进 protobuf 消息 (见 ``grpc_server.py``)。

op 命名约定: ``域.动作``, 例如 ``mouse.move`` / ``keyboard.type`` / ``screen.screenshot``。
另有一批短别名 (``move`` / ``click`` / ``screenshot``…) 方便手写调试。
"""

from __future__ import annotations

import base64
import os
import platform
import sys
import time
from typing import Any, Callable, Dict, Optional, Tuple

from .screen import Capture, ScreenCapture

__all__ = ["RemoteError", "RemoteService", "VERSION"]

VERSION = "0.1.0"

#: 服务启动时间, 用于 uptime
_STARTED_AT = time.time()


class RemoteError(Exception):
    """带错误码的业务异常。传输层负责把它变成错误响应。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


def _as_int(value: Any, name: str, default: Optional[int] = None) -> int:
    if value is None:
        if default is None:
            raise RemoteError("bad_request", f"缺少参数 {name}")
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise RemoteError("bad_request", f"参数 {name} 必须是整数, 收到 {value!r}")


def _as_float(value: Any, name: str, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        raise RemoteError("bad_request", f"参数 {name} 必须是数字, 收到 {value!r}")


def _as_str(value: Any, name: str, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


class RemoteService:
    """把输入控制与截屏包装成一组命名操作。"""

    def __init__(
        self,
        screen: Optional[ScreenCapture] = None,
        controller=None,
        token: Optional[str] = None,
    ):
        self.screen = screen or ScreenCapture()
        self._controller = controller
        self.token = token
        self.handlers: Dict[str, Callable[[dict], dict]] = {
            "ping": self._op_ping,
            "info": self._op_info,
            "screen.size": self._op_screen_size,
            "screen.screenshot": self._op_screen_screenshot,
            "mouse.position": self._op_mouse_position,
            "mouse.move": self._op_mouse_move,
            "mouse.move_rel": self._op_mouse_move_rel,
            "mouse.click": self._op_mouse_click,
            "mouse.down": self._op_mouse_down,
            "mouse.up": self._op_mouse_up,
            "mouse.scroll": self._op_mouse_scroll,
            "mouse.drag": self._op_mouse_drag,
            "keyboard.type": self._op_keyboard_type,
            "keyboard.key": self._op_keyboard_key,
            "keyboard.hotkey": self._op_keyboard_hotkey,
            "keyboard.paste": self._op_keyboard_paste,
            "keyboard.check": self._op_keyboard_check,
            "kuuki": self._op_kuuki,
            "notify": self._op_notify,
        }
        self.aliases = {
            "screenshot": "screen.screenshot",
            "capture": "screen.screenshot",
            "size": "screen.size",
            "position": "mouse.position",
            "move": "mouse.move",
            "move_rel": "mouse.move_rel",
            "click": "mouse.click",
            "mousedown": "mouse.down",
            "mouseup": "mouse.up",
            "scroll": "mouse.scroll",
            "drag": "mouse.drag",
            "type": "keyboard.type",
            "text": "keyboard.type",
            "key": "keyboard.key",
            "hotkey": "keyboard.hotkey",
            "paste": "keyboard.paste",
            "check": "keyboard.check",
            "keys": "keyboard.check",
            "sensor": "kuuki",
            "popup": "notify",
        }

    # ---------------- 控制器 (延迟创建, 避免只截屏的场景也去加载 pynput) ----------------
    @property
    def controller(self):
        if self._controller is None:
            from .input import InputController

            self._controller = InputController()
        return self._controller

    # ---------------- 调度 ----------------
    def resolve_op(self, op: str) -> str:
        if not op:
            raise RemoteError("bad_request", "缺少 op 字段")
        op = str(op).strip()
        if op in self.handlers:
            return op
        if op in self.aliases:
            return self.aliases[op]
        raise RemoteError("unknown_op", f"未知操作 {op!r}")

    def handle(self, op: str, args: Optional[dict] = None) -> dict:
        """执行一个操作。异常统一转成 ``RemoteError``。"""
        resolved = self.resolve_op(op)
        args = dict(args or {})
        try:
            return self.handlers[resolved](args)
        except RemoteError:
            raise
        except ValueError as exc:
            raise RemoteError("bad_request", str(exc))
        except Exception as exc:  # noqa: BLE001 - 传输层需要拿到稳定的错误码
            raise RemoteError("internal", f"{exc.__class__.__name__}: {exc}")

    def handle_request(self, request: dict) -> dict:
        """处理一条请求信封: ``{"id":..., "op":..., "args":{...}}``。

        ``args`` 之外的顶层键会被并入 args (``{"op":"mouse.move","x":1,"y":2}`` 也认)。
        """
        if not isinstance(request, dict):
            raise RemoteError("bad_request", "请求必须是 JSON 对象")
        if "batch" in request:
            items = request.get("batch") or []
            results = []
            for item in items:
                try:
                    results.append({"ok": True, "result": self.handle_request(item)})
                except RemoteError as exc:
                    results.append({"ok": False, "error": exc.to_dict()})
            return {"results": results}
        op = request.get("op")
        args = dict(request.get("args") or {})
        reserved = {"op", "args", "id", "batch"}
        for key, value in request.items():
            if key not in reserved:
                args.setdefault(key, value)
        result = self.handle(op, args)
        if isinstance(result, dict) and "ok" not in result:
            result = {"ok": True, **result}
        return result

    # ---------------- 基础 ----------------
    def _op_ping(self, args: dict) -> dict:
        return {
            "pong": True,
            "ts": time.time(),
            "uptime_s": round(time.time() - _STARTED_AT, 3),
            "version": VERSION,
        }

    def _op_info(self, args: dict) -> dict:
        from .input import clipboard_tool

        info = {
            "version": VERSION,
            "os": platform.system(),
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "hostname": platform.node(),
            "user": os.environ.get("USER") or os.environ.get("USERNAME") or "",
            "cwd": os.getcwd(),
            "display": os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or "",
            "clipboard_tool": (clipboard_tool() or (None, None))[0],
            "token_required": bool(self.token),
            "uptime_s": round(time.time() - _STARTED_AT, 3),
            "capabilities": sorted(self.handlers) + sorted(self.aliases),
        }
        try:
            info["cursor"] = dict(zip(("x", "y"), self.controller.position()))
        except Exception as exc:
            info["cursor_error"] = f"{exc.__class__.__name__}: {exc}"
        return info

    # ---------------- 截屏 ----------------
    def capture(self, args: dict) -> Tuple[Capture, bytes]:
        """抓一帧, 返回 (元数据, 图片字节)。WS 二进制帧与 gRPC 都走这里。"""
        capture = self.screen.capture(
            region=args.get("region"),
            fmt=_as_str(args.get("format", args.get("fmt")), "format", "png"),
            quality=_as_int(args.get("quality"), "quality", 80),
            max_width=args.get("max_width"),
            max_height=args.get("max_height"),
            scale=args.get("scale"),
            allow_upscale=bool(args.get("allow_upscale", False)),
            draw_cursor=bool(args.get("draw_cursor", False)),
        )
        return capture, capture.data

    def _op_screen_size(self, args: dict) -> dict:
        width, height = self.screen.screen_size()
        return {"width": width, "height": height}

    def _op_screen_screenshot(self, args: dict) -> dict:
        capture, data = self.capture(args)
        payload = capture.to_dict()
        if args.get("include_image", True):
            payload["image_b64"] = base64.b64encode(data).decode("ascii")
        payload["ok"] = True
        return payload

    # ---------------- 鼠标 ----------------
    def _op_mouse_position(self, args: dict) -> dict:
        x, y = self.controller.position()
        return {"x": x, "y": y}

    def _op_mouse_move(self, args: dict) -> dict:
        x = _as_int(args.get("x"), "x")
        y = _as_int(args.get("y"), "y")
        duration = _as_float(args.get("duration"), "duration", 0.0)
        self.controller.move_smooth(x, y, duration)
        return {"x": x, "y": y, "duration": duration}

    def _op_mouse_move_rel(self, args: dict) -> dict:
        dx = _as_int(args.get("dx", args.get("x")), "dx")
        dy = _as_int(args.get("dy", args.get("y")), "dy")
        duration = _as_float(args.get("duration"), "duration", 0.0)
        x, y = self.controller.move_relative_smooth(dx, dy, duration)
        return {"x": x, "y": y, "dx": dx, "dy": dy}

    def _op_mouse_click(self, args: dict) -> dict:
        button = _as_str(args.get("button"), "button", "left")
        clicks = _as_int(args.get("clicks", args.get("count")), "clicks", 1)
        interval = _as_float(args.get("interval"), "interval", 0.05)
        # hold 默认 60ms: 瞬时 down/up 会被某些前端框架当成无效点击
        hold = _as_float(args.get("hold"), "hold", 0.06)
        done = self.controller.click(button, clicks, interval, hold)
        return {"button": button, "clicks": done, "hold": hold}

    def _op_mouse_down(self, args: dict) -> dict:
        button = _as_str(args.get("button"), "button", "left")
        self.controller.press_mouse(button)
        return {"button": button, "state": "down"}

    def _op_mouse_up(self, args: dict) -> dict:
        button = _as_str(args.get("button"), "button", "left")
        self.controller.release_mouse(button)
        return {"button": button, "state": "up"}

    def _op_mouse_scroll(self, args: dict) -> dict:
        # 兼容 kuuki 老协议: {"t":"scroll","delta":N} 里 delta>0 表示向上
        if "delta" in args and "dy" not in args:
            dy = _as_int(args.get("delta"), "delta", 0)
            dx = 0
        else:
            dx = _as_int(args.get("dx"), "dx", 0)
            dy = _as_int(args.get("dy", args.get("delta")), "dy", 0)
        self.controller.scroll(dx, dy)
        return {"dx": dx, "dy": dy}

    def _op_mouse_drag(self, args: dict) -> dict:
        x1 = _as_int(args.get("x1", args.get("from_x")), "x1")
        y1 = _as_int(args.get("y1", args.get("from_y")), "y1")
        x2 = _as_int(args.get("x2", args.get("to_x")), "x2")
        y2 = _as_int(args.get("y2", args.get("to_y")), "y2")
        button = _as_str(args.get("button"), "button", "left")
        duration = _as_float(args.get("duration"), "duration", 0.3)
        return self.controller.drag(x1, y1, x2, y2, button, duration)

    # ---------------- 键盘 ----------------
    def _op_keyboard_type(self, args: dict) -> dict:
        text = _as_str(args.get("text", args.get("message")), "text", "")
        interval = _as_float(args.get("interval"), "interval", 0.0)
        count = self.controller.type_text(text, interval)
        return {"chars": count}

    def _op_keyboard_key(self, args: dict) -> dict:
        key = _as_str(args.get("key"), "key", "")
        if not key:
            raise RemoteError("bad_request", "缺少参数 key")
        # 兼容 kuuki 老协议: key == "calibrate" 时交给姿态校准
        if key.lower() in ("calibrate", "recenter") and args.get("route_calibrate", True):
            return self._op_kuuki({"message": {"t": "calibrate"}})
        action = _as_str(args.get("action"), "action", "tap")
        modifiers = args.get("modifiers") or []
        if isinstance(modifiers, str):
            modifiers = [m for m in modifiers.replace(" ", "").split("+") if m]
        return self.controller.key_action(key, action, modifiers)

    def _op_keyboard_hotkey(self, args: dict) -> dict:
        keys = args.get("keys") or args.get("combo") or args.get("key")
        if not keys:
            raise RemoteError("bad_request", "缺少参数 keys (例如 \"ctrl+shift+s\")")
        return self.controller.hotkey(keys)

    def _op_keyboard_paste(self, args: dict) -> dict:
        text = _as_str(args.get("text", args.get("message")), "text", "")
        return self.controller.paste_text(text)

    def _op_keyboard_check(self, args: dict) -> dict:
        """查询键能不能发 (不实际按键)。给 agent 在动手前自检用。"""
        keys = args.get("keys")
        if keys is None:
            keys = [args.get("key")] if args.get("key") else []
        if isinstance(keys, str):
            keys = [keys]
        return self.controller.check_keys(keys)

    # ---------------- 被控端提示 / 许可 ----------------
    def _op_notify(self, args: dict) -> dict:
        """在被控端屏幕角落弹一个无焦点角标 (只通知, 不等确认, 不阻塞)。

        用自绘窗口而不是 Windows 系统弹窗 —— 系统弹窗会抢前台焦点, 把控制端
        正要输入的内容带到别处。角标不抢焦点, 位置可选四个角, 多个通知纵向堆叠。
        """
        from .toast import notify, notify_supported

        if not notify_supported():
            raise RemoteError("unsupported", "角标通知需要被控端有 tkinter 图形环境")
        message = _as_str(args.get("message", args.get("text")), "message", "")
        if not message:
            raise RemoteError("bad_request", "缺少参数 message")
        detail = _as_str(args.get("detail"), "detail", "")
        seconds = _as_float(args.get("seconds"), "seconds", 6.0)
        corner = _as_str(args.get("corner"), "corner", "br")
        if corner not in ("br", "tr", "tl", "bl"):
            raise RemoteError("bad_request", "corner 必须是 br / tr / tl / bl 之一")
        shown = notify(message, detail, seconds, corner)
        return {"shown": shown, "corner": corner, "seconds": seconds}

    # ---------------- kuuki 老协议透传 ----------------
    def _op_kuuki(self, args: dict) -> dict:
        """把一条消息交给 ``app.handle_message`` (空气鼠标的姿态/鼠标/文本路由)。

        ``app`` 模块级会创建 ``App()`` (含 Mahony 姿态解算 + pynput 控制器),
        所以这里**延迟导入** —— 只用远程控制功能时不加载它。
        """
        message = args.get("message")
        if message is None:
            message = {k: v for k, v in args.items() if k not in ("op", "args")}
        if not isinstance(message, dict):
            raise RemoteError("bad_request", "kuuki 透传需要 message 是 JSON 对象")

        try:  # pragma: no cover - 取决于启动方式
            from app import handle_message
        except ImportError:  # pragma: no cover
            import pathlib

            root = str(pathlib.Path(__file__).resolve().parent.parent)
            if root not in sys.path:
                sys.path.insert(0, root)
            from app import handle_message

        handled = bool(handle_message(message))
        return {"handled": handled, "message": message}
