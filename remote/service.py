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


def _as_bool(value: Any, default: bool = False) -> bool:
    """把 ``True`` / ``1`` / ``"true"`` / ``"no"`` 这类写法归一成 bool。

    不能只写 ``bool(args.get("x"))``: 那样 ``"false"`` 也会被判成 True。跨了
    JSON / protobuf 两道序列化之后 bool 变字符串是很常见的事 (PeerJS 只走 JSON,
    手写调试也常常给 ``--restore false``), 所以显式认一遍再报错。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "y", "on"):
        return True
    if text in ("false", "0", "no", "n", "off", ""):
        return False
    raise RemoteError("bad_request", f"参数应是 true/false, 收到 {value!r}")


def parse_points(value: Any) -> list:
    """把路径点归一成 ``[[x, y], ...]``。

    包内共享: 服务端要解析请求里的 points, gRPC 客户端也要把同样的写法填进
    protobuf, 两边用同一份实现才不会出现"WS 认得、gRPC 不认得"的偏差。

    接受两种写法 (手写调试时后者省事):
    - ``[[100, 200], [300, 400]]`` / ``[{"x":100,"y":200}, ...]``
    - ``"100,200;300,400"``
    """
    if isinstance(value, str):
        text = value.replace(" ", "")
        if not text:
            return []
        raw: list = []
        for chunk in text.split(";"):
            if not chunk:
                continue
            parts = chunk.split(",")
            if len(parts) != 2:
                raise RemoteError(
                    "bad_request", f"路径点格式应为 x,y;x,y, 收到 {chunk!r}"
                )
            raw.append(parts)
        value = raw
    if not isinstance(value, (list, tuple)):
        raise RemoteError("bad_request", f"points 必须是列表, 收到 {type(value).__name__}")
    out = []
    for item in value:
        if isinstance(item, dict):
            px, py = item.get("x"), item.get("y")
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            px, py = item
        else:
            raise RemoteError("bad_request", f"每个路径点应为 [x, y], 收到 {item!r}")
        try:
            out.append([int(px), int(py)])
        except (TypeError, ValueError):
            raise RemoteError("bad_request", f"路径点坐标必须是整数, 收到 {item!r}")
    return out


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
            "screen.monitors": self._op_screen_monitors,
            "screen.screenshot": self._op_screen_screenshot,
            "screen.ocr": self._op_screen_ocr,
            "screen.calibrate": self._op_screen_calibrate,
            "mouse.position": self._op_mouse_position,
            "mouse.move": self._op_mouse_move,
            "mouse.move_rel": self._op_mouse_move_rel,
            "mouse.click": self._op_mouse_click,
            "mouse.down": self._op_mouse_down,
            "mouse.up": self._op_mouse_up,
            "mouse.scroll": self._op_mouse_scroll,
            "mouse.scroll_h": self._op_mouse_scroll_h,
            "mouse.drag": self._op_mouse_drag,
            "keyboard.type": self._op_keyboard_type,
            "keyboard.key": self._op_keyboard_key,
            "keyboard.hotkey": self._op_keyboard_hotkey,
            "keyboard.combo": self._op_keyboard_combo,
            "keyboard.hold": self._op_keyboard_hold,
            "keyboard.paste": self._op_keyboard_paste,
            "keyboard.check": self._op_keyboard_check,
            "window.list": self._op_window_list,
            "window.foreground": self._op_window_foreground,
            "window.focus": self._op_window_focus,
            "kuuki": self._op_kuuki,
            "notify": self._op_notify,
        }
        self.aliases = {
            "screenshot": "screen.screenshot",
            "capture": "screen.screenshot",
            "ocr": "screen.ocr",
            "read": "screen.ocr",
            "size": "screen.size",
            "monitors": "screen.monitors",
            "monitor": "screen.monitors",
            "screens": "screen.monitors",
            "calibrate": "screen.calibrate",
            "calib": "screen.calibrate",
            "position": "mouse.position",
            "move": "mouse.move",
            "move_rel": "mouse.move_rel",
            "click": "mouse.click",
            "mousedown": "mouse.down",
            "mouseup": "mouse.up",
            "scroll": "mouse.scroll",
            "scroll_h": "mouse.scroll_h",
            "drag": "mouse.drag",
            "dragp": "mouse.drag",
            "type": "keyboard.type",
            "text": "keyboard.type",
            "key": "keyboard.key",
            "hotkey": "keyboard.hotkey",
            "combo": "keyboard.combo",
            "hold": "keyboard.hold",
            "paste": "keyboard.paste",
            "check": "keyboard.check",
            "keys": "keyboard.check",
            "sensor": "kuuki",
            "popup": "notify",
            "windows": "window.list",
            "foreground": "window.foreground",
            "focus": "window.focus",
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
        # monitor / all_screens: 抓哪一块。索引 0 是合法值 (第一块屏), 所以只能判
        # None, 不能 `or 默认` —— 否则"抓第一块屏"会被当成"没给"。
        shot_kwargs: Dict[str, object] = {}
        if args.get("monitor") is not None:
            shot_kwargs["monitor"] = _as_int(args.get("monitor"), "monitor")
        if args.get("all_screens") is not None:
            shot_kwargs["all_screens"] = _as_bool(args.get("all_screens"), False)

        capture = self.screen.capture(
            region=args.get("region"),
            fmt=_as_str(args.get("format", args.get("fmt")), "format", "png"),
            quality=_as_int(args.get("quality"), "quality", 80),
            max_width=args.get("max_width"),
            max_height=args.get("max_height"),
            scale=args.get("scale"),
            allow_upscale=bool(args.get("allow_upscale", False)),
            draw_cursor=bool(args.get("draw_cursor", False)),
            **shot_kwargs,
        )
        return capture, capture.data

    def _op_screen_size(self, args: dict) -> dict:
        width, height = self.screen.screen_size()
        return {"width": width, "height": height}

    def _op_screen_monitors(self, args: dict) -> dict:
        """显示器与虚拟桌面边界 —— 多屏机器上校准/定位的前置信息。

        不给 ``x``/``y`` 就是列全部; 给了就只回「这个点在哪块屏上」(点不在任何
        一块屏上时报错, 而不是猜一个)。两种调用的返回结构一致, 调用方不必分支。
        """
        from . import monitor

        if not monitor.monitor_supported():
            raise RemoteError("unsupported", "显示器枚举需要被控端是 Windows")
        x = args.get("x")
        y = args.get("y")
        try:
            if x is not None and y is not None:
                item = monitor.monitor_at(_as_int(x, "x"), _as_int(y, "y"))
                # 单点查询不给 primary_index / virtual_screen: 两块屏的信息在这
                # 种调用里没意义, 而 proto 的 optional 字段"没设"会被省略 ——
                # 塞个 None 进去反而让两条传输的返回值对不上
                return {"count": 1, "monitors": [item]}
            return monitor.list_monitors()
        except monitor.MonitorError as exc:
            raise RemoteError(exc.code, exc.message)

    def _op_screen_screenshot(self, args: dict) -> dict:
        capture, data = self.capture(args)
        payload = capture.to_dict()
        if args.get("include_image", True):
            payload["image_b64"] = base64.b64encode(data).decode("ascii")
        payload["ok"] = True
        return payload

    def _op_screen_ocr(self, args: dict) -> dict:
        """认出这一屏上的**文字** (Windows 内置 OCR) —— 见 ``remote/ocr.py``。

        视觉定位 (``locate`` / ``remote/vision.py``) 只能说"这里有一块像输入框的
        东西", 说不出里面写着什么。这一层把"图上有什么字"补上, 而且每行都给出
        **能直接点的屏幕坐标**, 于是"找到写着'发送'的那块并点它"一次调用就能闭环。

        坐标换算的三步 (缩放 / 裁剪 / 帧原点) 全在这里做掉: 调用方拿到的是鼠标
        坐标系里的数, 不需要自己再乘系数 —— 那个乘法漏一次就点偏一整块屏 (踩过,
        见 ``docs/knowledge/gui-screenshot-scale.md``)。
        """
        from . import ocr

        if not ocr.ocr_supported():
            raise RemoteError("unsupported", "文字识别需要被控端是 Windows (走系统内置 OCR)")
        # OCR 吃的帧强制 png: 调用方指定 jpeg/webp 是为了省带宽, 但压缩噪声会直接
        # 变成认错的字。这一帧不回给调用方, 编码格式的取舍只该由识别质量决定。
        shot = dict(args)
        shot["format"] = "png"
        capture, data = self.capture(shot)
        try:
            result = ocr.recognize(data, lang=_as_str(args.get("lang"), "lang", ""))
        except ocr.OcrError as exc:
            raise RemoteError(exc.code, exc.message)

        include_words = _as_bool(args.get("include_words"), True)
        offset = (capture.region.left, capture.region.top) if capture.region else (0, 0)
        lines = []
        for line in result.get("lines") or []:
            item: Dict[str, Any] = {
                "text": line["text"],
                "x": line["x"],
                "y": line["y"],
                "w": line["w"],
                "h": line["h"],
                # 行中心那一点的屏幕坐标 —— 大多数时候要点的就是这个
                "screen": ocr.to_screen_rect(line, capture.scale, capture.origin, offset),
            }
            # "词"这个键**永远给** (不要词的时候给空列表): protobuf 的空 repeated
            # 字段在 MessageToDict 里照样会出现 (``"words": []``), 而 WS 侧是普通
            # dict —— 一边给空列表、一边不给键, 跨传输比对就红了 (就是
            # test_new_ops_agree_across_transports 抓出来的那种不一致)。
            item["words"] = (
                [
                    {
                        "text": word["text"],
                        "x": word["x"],
                        "y": word["y"],
                        "w": word["w"],
                        "h": word["h"],
                        "screen": ocr.to_screen_rect(
                            word, capture.scale, capture.origin, offset
                        ),
                    }
                    for word in line.get("words") or []
                ]
                if include_words
                else []
            )
            lines.append(item)
        return {
            "ok": True,
            "text": result.get("text", ""),
            "language": result.get("language", ""),
            "lines": lines,
            "count": len(lines),
            "width": capture.width,
            "height": capture.height,
            "scale": round(float(capture.scale), 4),
            "origin": {"x": int(capture.origin[0]), "y": int(capture.origin[1])},
        }

    def _op_screen_calibrate(self, args: dict) -> dict:
        """跑一次坐标校准 —— 见 ``remote/calibrate.py`` 的模块说明。

        **会动鼠标**: 要移过去才知道"图上的 (x,y)"到底对应"屏幕的哪个点"。
        默认 ``restore=true`` 把光标挪回原位。
        """
        from . import calibrate

        return calibrate.run(
            self.screen,
            self.controller,
            cols=_as_int(args.get("cols"), "cols", 3),
            rows=_as_int(args.get("rows"), "rows", 3),
            margin=_as_float(args.get("margin"), "margin", 0.12),
            settle=_as_float(args.get("settle"), "settle", 0.1),
            tolerance=_as_float(args.get("tolerance"), "tolerance", 2.0),
            # 0 与 None 都表示"不缩放": 这里不存在"显式 0 是别的意思",
            # 直接用 0 当缺省, 免得又搞一个 optional 字段
            max_width=_as_int(args.get("max_width"), "max_width", 0) or None,
            restore=_as_bool(args.get("restore"), True),
        )

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
        # x / y 可选: 先定位再点。缺省表示点当前位置。以前这里两个参数被忽略了,
        # 传 {"x":..,"y":..} 会静默点在当前光标处 (后来才发现的 bug, 见
        # docs/knowledge/protocol-mouse-click-ignores-xy.md)。
        x = args.get("x")
        y = args.get("y")
        x = _as_int(x, "x") if x is not None else None
        y = _as_int(y, "y") if y is not None else None
        move_duration = _as_float(args.get("move_duration"), "move_duration", 0.0)
        done = self.controller.click(button, clicks, interval, hold, x, y, move_duration)
        # interval 也回进返回值: 它是"这次点击怎么执行的"的一部分, 而且只有回进来
        # 才能跨传输比对 —— 否则 gRPC 侧把显式 0 换成默认值这种偏差测不出来
        # (hold 同理, 它就是在补 hold 字段时靠返回值抓出来的)
        result = {"button": button, "clicks": done, "hold": hold, "interval": interval}
        if x is not None or y is not None:
            result["positioned_at"] = {"x": x, "y": y}
        return result

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
        steps = _as_int(args.get("steps"), "steps", 1)
        interval = _as_float(args.get("interval"), "interval", 0.05)
        # x / y 可选: 先定位再滚。缺省表示保持当前坐标 (不是 0)
        x = args.get("x")
        y = args.get("y")
        x = _as_int(x, "x") if x is not None else None
        y = _as_int(y, "y") if y is not None else None
        result = self.controller.scroll(dx, dy, steps, interval, x, y)
        if x is not None or y is not None:
            result["positioned_at"] = {"x": x, "y": y}
        return result

    def _op_mouse_scroll_h(self, args: dict) -> dict:
        """横向滚动 (= ``mouse.scroll`` 的 dx)。

        单独给个 op 是为了让控制端能写 ``scroll_h --dx 3`` 而不必记"横向要传 dx、
        dy 留 0" —— 参数名即语义。``delta`` 也认, 按横向处理。
        """
        merged = dict(args)
        if "dx" not in merged:
            merged["dx"] = merged.pop("delta", 0)
        merged.setdefault("dy", 0)
        result = self._op_mouse_scroll(merged)
        result["axis"] = "h"
        return result

    def _op_mouse_drag(self, args: dict) -> dict:
        button = _as_str(args.get("button"), "button", "left")
        duration = _as_float(args.get("duration"), "duration", 0.3)
        points = args.get("points")
        if points is None and args.get("path") is not None:
            points = args.get("path")
        if points is not None:
            # path 字符串写法 "x1,y1;x2,y2;x3,y3" 便于手写调试
            points = parse_points(points)
            if len(points) < 2:
                raise RemoteError("bad_request", "points 至少需要两个点 (起点与终点)")
            return self.controller.drag(button=button, duration=duration, points=points)
        x1 = _as_int(args.get("x1", args.get("from_x")), "x1")
        y1 = _as_int(args.get("y1", args.get("from_y")), "y1")
        x2 = _as_int(args.get("x2", args.get("to_x")), "x2")
        y2 = _as_int(args.get("y2", args.get("to_y")), "y2")
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
        hold_ms = _as_float(args.get("hold_ms", args.get("hold")), "hold_ms", 0.0)
        return self.controller.hotkey(keys, hold_ms)

    def _op_keyboard_combo(self, args: dict) -> dict:
        """组合键 + 按住时长。与 ``keyboard.hotkey`` 是同一个实现。

        分开只是因为语义: ``hotkey`` 是"敲一下这个组合", ``combo`` 是"按住这个组合
        一段时间" —— 后者在菜单快捷键、游戏按键这类需要持续按下的场景才生效。
        """
        keys = args.get("keys") or args.get("combo") or args.get("key")
        if not keys:
            raise RemoteError("bad_request", "缺少参数 keys (例如 \"ctrl+shift+s\")")
        hold_ms = _as_float(args.get("hold_ms", args.get("hold")), "hold_ms", 0.0)
        result = self.controller.hotkey(keys, hold_ms)
        result["op"] = "keyboard.combo"
        return result

    def _op_keyboard_hold(self, args: dict) -> dict:
        """按住单个键 ``ms`` 毫秒再松开 (F2 重命名这类长按场景)。"""
        key = _as_str(args.get("key"), "key", "")
        if not key:
            raise RemoteError("bad_request", "缺少参数 key")
        ms = _as_float(args.get("ms", args.get("hold_ms", args.get("duration"))), "ms", 0.0)
        return self.controller.hold_key(key, ms)

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

    # ---------------- 窗口 ----------------
    # 这三个 op 是"视觉定位"的补集: 视觉能告诉你"屏幕上有块像输入框的东西",
    # 但说不出"这块东西属于哪个应用"。先用标题/进程名认窗口、把它切到前台,
    # 之后的 locate / click 才有了确定的作用域 (踩过的坑见
    # docs/knowledge/gui-window-focus-gap.md)。
    def _op_window_list(self, args: dict) -> dict:
        """列出顶层窗口, 按 Z 序 (最靠前的最先)。"""
        from . import window

        if not window.window_supported():
            raise RemoteError("unsupported", "窗口枚举需要被控端是 Windows")
        title = _as_str(args.get("title"), "title", "")
        process = _as_str(args.get("process", args.get("proc")), "process", "")
        limit = _as_int(args.get("limit"), "limit", 0)
        include_hidden = bool(args.get("include_hidden", False))
        try:
            items = window.list_windows(
                title=title, process=process,
                include_hidden=include_hidden, limit=limit,
            )
        except window.WindowError as exc:
            raise RemoteError(exc.code, exc.message)
        return {"windows": items, "count": len(items)}

    def _op_window_foreground(self, args: dict) -> dict:
        """当前前台是谁。跑任何点击流程之前先问一句, 比事后核对截图便宜。

        返回**平铺**的窗口信息 (不是 ``{"window": {...}}``): gRPC 侧直接回一个
        ``WindowInfo`` 消息, 嵌套一层就没法与 WS 的返回值对齐了。没有前台窗口
        (锁屏 / 桌面) 时给全零的一组, ``hwnd == 0`` 就是"没有" —— 调用方不必
        为了键在不在而分支。
        """
        from . import window

        if not window.window_supported():
            raise RemoteError("unsupported", "前台查询需要被控端是 Windows")
        info = window.foreground_window()
        if info is None:
            return {
                "hwnd": 0,
                "title": "",
                "process": "",
                "pid": 0,
                "rect": {"left": 0, "top": 0, "width": 0, "height": 0},
                "visible": False,
                "minimized": False,
                "foreground": False,
            }
        return dict(info)

    def _op_window_focus(self, args: dict) -> dict:
        """把某个窗口切到前台 (按 hwnd / 标题 / 进程名选, 不用猜坐标)。

        ``focused`` 是**确认之后**的结果, 不是"调用成功了": Windows 的前台锁
        可能让这次切换静默失败, 调用方必须看这个字段, 不能假设。
        """
        from . import window

        if not window.window_supported():
            raise RemoteError("unsupported", "窗口切换需要被控端是 Windows")
        hwnd = args.get("hwnd", args.get("handle"))
        hwnd = _as_int(hwnd, "hwnd") if hwnd is not None else None
        title = _as_str(args.get("title"), "title", "")
        process = _as_str(args.get("process", args.get("proc")), "process", "")
        if hwnd is None and not title and not process:
            raise RemoteError("bad_request", "window.focus 需要 hwnd / title / process 之一")
        index = _as_int(args.get("index"), "index", 0)
        wait = _as_float(args.get("wait"), "wait", window.DEFAULT_FOCUS_WAIT)
        try:
            return window.focus_window(
                hwnd=hwnd, title=title, process=process, index=index, wait=wait
            )
        except window.WindowError as exc:
            raise RemoteError(exc.code, exc.message)

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
