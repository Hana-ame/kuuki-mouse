"""截屏: 一个后端 (Pillow ImageGrab), 一个尺寸来源 (截图本身)。

设计原则
--------
**只用两个 API**: ``PIL.ImageGrab.grab()`` 拿图, ``pynput.mouse.Controller`` 拿光标。

不做的事 (以及为什么):
  * **不做后端降级链** —— 曾经有 mss → Pillow → ffmpeg 三级 + 失败拉黑,
    但实测只有一个能跑, 另外两个纯属负担。真跑不了就报错, 让人看见。
  * **不探测屏幕尺寸** —— 曾经有 Xlib / xdpyinfo / GetSystemMetrics / 环境变量
    四套探测, 还会和截图实际尺寸不一致 (实测报 1920x1080 而截图是 1680x1050)。
    截图返回的图, ``.size`` 就是屏幕尺寸 —— 一条路, 永远准。
  * **不自己发明多显示器布局** —— 要问"有几块屏、边界在哪"用 ``remote/monitor.py``
    (``screen.monitors`` op)。这里只负责: 给定一块屏 (或整个虚拟桌面), 把图抓回来,
    并**报出这一帧左上角在虚拟桌面坐标系里的位置** (``Capture.origin``)。

.. warning::

   ``ImageGrab.grab()`` 无参数调用抓的是**主显示器**, 不是整个虚拟桌面
   (Pillow 走的是 ``SM_CXSCREEN`` —— 主屏分辨率; 要虚拟桌面得显式
   ``all_screens=True``)。单屏机器上两者一样, 所以这个区别很容易被忽略;
   **多屏时它意味着"图上的 (0,0)"不是"鼠标的 (0,0)"** —— 差的就是主屏左上角
   在虚拟桌面里的偏移, 也就是 ``origin``。

pynput 没有屏幕尺寸 API (``Controller`` 只有 position/move/click/press/release/scroll,
Windows 后端只用 GetCursorPos/SetCursorPos), 所以尺寸只从截图拿。
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from PIL import Image, ImageGrab

__all__ = ["Region", "Capture", "ScreenCapture"]

#: 截图上画的光标标记 (``draw_cursor=True``) 长什么样。提到模块级是因为
#: ``remote/calibrate.py`` 认标记时要照着同一个值去找 —— 两边各写一份的话, 改了
#: 颜色那边就会"突然认不出标记", 而且很难想到根因在另一个文件里。
CURSOR_COLOR: Tuple[int, int, int] = (255, 0, 0)
CURSOR_ARM: int = 12
CURSOR_HALO: int = 5


# ---------------------------------------------------------------- 数据结构


@dataclass
class Region:
    """屏幕区域 (左上角 + 宽高, 单位像素, 绝对坐标)。"""

    left: int
    top: int
    width: int
    height: int

    @classmethod
    def from_any(cls, value) -> Optional["Region"]:
        """接受 None / dict / [l,t,w,h] / (l,t,w,h)。"""
        if value is None:
            return None
        if isinstance(value, Region):
            return value
        if isinstance(value, dict):
            left = int(value.get("left", value.get("x", 0)) or 0)
            top = int(value.get("top", value.get("y", 0)) or 0)
            width = int(value.get("width", value.get("w", 0)) or 0)
            height = int(value.get("height", value.get("h", 0)) or 0)
        elif isinstance(value, (list, tuple)) and len(value) == 4:
            left, top, width, height = (int(v) for v in value)
        else:
            raise ValueError(f"无法解析区域: {value!r}")
        if width <= 0 or height <= 0:
            raise ValueError(f"区域宽高必须为正: {width}x{height}")
        if left < 0 or top < 0:
            raise ValueError(f"区域左上角不能为负: ({left},{top})")
        return cls(left, top, width, height)

    def as_tuple(self) -> Tuple[int, int, int, int]:
        """Pillow bbox 形式: (left, top, right, bottom)。"""
        return (self.left, self.top, self.left + self.width, self.top + self.height)

    def to_dict(self) -> dict:
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }


@dataclass
class Capture:
    """一次抓屏的结果 (含编码后的图片字节)。"""

    data: bytes
    format: str
    width: int
    height: int
    source_width: int
    source_height: int
    region: Optional[Region] = None
    scale: float = 1.0
    captured_at: float = 0.0
    duration_ms: float = 0.0
    cursor: Optional[dict] = None
    #: 这一帧左上角在**虚拟桌面坐标系**里的位置。抓主屏且主屏在原点时是 (0,0),
    #: 抓副屏/抓虚拟桌面时不是 —— 它是"图坐标 <-> 鼠标坐标"换算里那个必须显式
    #: 带上的平移量, 丢了它 region 与光标都会整体偏移一整块屏。
    origin: Tuple[int, int] = (0, 0)
    #: 抓屏用的后端。现在只有一个 (PIL.ImageGrab), 早先的 mss / ffmpeg x11grab 降级链
    #: 已经不存在了 —— 但字段留着: 协议头里要报它, 让控制端知道这一帧是怎么来的。
    #: (wincheck.py 曾经读 capture.backend 直接 AttributeError, 就是因为只有协议头里
    #:  硬编码了一份 "pillow", Capture 上反而没有。真值只能有一份。)
    backend: str = "pillow"

    @property
    def bytes(self) -> int:
        return len(self.data)

    def to_dict(self, image_b64: Optional[str] = None) -> dict:
        out = {
            "format": self.format,
            "width": self.width,
            "height": self.height,
            "source_width": self.source_width,
            "source_height": self.source_height,
            "region": self.region.to_dict() if self.region else None,
            "scale": round(self.scale, 4),
            "captured_at": self.captured_at,
            "duration_ms": round(self.duration_ms, 2),
            "bytes": self.bytes,
            "cursor": self.cursor,
            # 与 cursor / region 同一个坐标系问题: 必须随帧一起走, 否则控制端
            # 拿到图却不知道 (0,0) 对应屏幕的哪里
            "origin": {"x": int(self.origin[0]), "y": int(self.origin[1])},
            # 必须在这里: JSON 通道 (screen.screenshot) 与二进制帧头都走这个方法,
            # 以前漏了它, 于是同一帧走 op 拿不到 backend、走二进制才拿得到。
            "backend": self.backend,
        }
        if image_b64 is not None:
            out["image_b64"] = image_b64
        return out


# ---------------------------------------------------------------- 管理器


class ScreenCapture:
    """抓屏 + 裁剪 + 缩放 + 编码 + 光标叠加。

    尺寸的唯一来源是截图本身: ``grab_image().size``。
    """

    FORMATS = {"png": "PNG", "jpg": "JPEG", "jpeg": "JPEG", "webp": "WEBP"}

    def __init__(self, display: Optional[str] = None):
        self.display = display

    # ---- 抓图 ----

    def frame_origin(self) -> Tuple[int, int]:
        """默认那一帧 (``grab_image()``) 的左上角在虚拟桌面坐标系里的位置。

        主屏不在虚拟桌面原点时 (比如副屏接在左边, 主屏 rect 的 left 是 0 而虚拟
        桌面 left 是 -1920), 这个值不是 (0,0)。问不出来就给 (0,0) —— 拿不到布局
        时宁可按单屏处理, 也不要让抓屏这件事失败。
        """
        try:
            from .monitor import list_monitors, monitor_supported

            if not monitor_supported():
                return (0, 0)
            info = list_monitors()
            index = info.get("primary_index")
            if index is None:
                return (0, 0)
            rect = info["monitors"][index]["rect"]
            return (int(rect["left"]), int(rect["top"]))
        except Exception:  # noqa: BLE001 - 布局问不出来不该让抓屏失败
            return (0, 0)

    def _grab_bbox(self, box: Tuple[int, int, int, int]) -> Image.Image:
        """抓虚拟桌面坐标系里的一块 (``box`` 是 Pillow bbox, 允许负坐标)。

        单独一个方法是为了让测试能替换它 —— 多显示器布局没法在 CI 上造, 只能打桩。
        """
        return ImageGrab.grab(bbox=box, all_screens=True)

    def grab_frame(
        self,
        monitor: Optional[int] = None,
        all_screens: bool = False,
    ) -> Tuple[Image.Image, Tuple[int, int]]:
        """抓一帧, 并给出它在虚拟桌面坐标系里的原点。

        * 都不给: 抓主显示器 (与 ``grab_image()`` 同一条路, origin 是主屏左上角)
        * ``all_screens=True``: 抓整个虚拟桌面 (origin 是虚拟桌面左上角, 可能是负的)
        * ``monitor=<index>``: 抓第 index 块屏 (origin 是那块屏的左上角)

        返回的图统一转成 RGB。
        """
        if all_screens and monitor is not None:
            raise ValueError("monitor 与 all_screens 只能给一个")

        if not all_screens and monitor is None:
            return self.grab_image(), self.frame_origin()

        from .monitor import list_monitors, MonitorError

        try:
            info = list_monitors()
        except MonitorError as exc:
            raise ValueError(f"问不出显示器布局: {exc.message}") from exc

        if all_screens:
            rect = info["virtual_screen"]
        else:
            monitors = info["monitors"]
            if not (0 <= int(monitor) < len(monitors)):
                raise ValueError(
                    f"没有第 {monitor} 块显示器 (一共 {len(monitors)} 块)"
                )
            rect = monitors[int(monitor)]["rect"]

        left, top = int(rect["left"]), int(rect["top"])
        box = (left, top, left + int(rect["width"]), top + int(rect["height"]))
        image = self._grab_bbox(box)
        return (image if image.mode == "RGB" else image.convert("RGB"), (left, top))

    def grab_image(self) -> Image.Image:
        """抓整个屏幕。

        受控端只支持 Windows, 正常路径就是 ``ImageGrab.grab()`` 无参数调用。
        下面的 ``xdisplay`` 分支是为曾经在 WSLg(X11) 上跑而留 —— 服务端已不会在
        非 Windows 启动 (见 ``remote/__main__.py`` 的 ``platform_refusal()``),
        而且那条路截到的也不是 Windows 桌面。"""
        kwargs: Dict[str, object] = {}
        import sys

        # 不再维护: Linux/X11 分支 (_LINUX 恒 False, 走不到)
        if sys.platform.startswith("linux"):
            kwargs["xdisplay"] = self.display or ":0"
        image = ImageGrab.grab(**kwargs)
        return image if image.mode == "RGB" else image.convert("RGB")

    def screen_size(self) -> Tuple[int, int]:
        """屏幕尺寸 —— 直接来自一次截图, 不探测。"""
        image = self.grab_image()
        size = image.size
        image.close()
        return size

    # ---- 抓一帧 ----
    def capture(
        self,
        region=None,
        fmt: str = "png",
        quality: int = 80,
        max_width: Optional[int] = None,
        max_height: Optional[int] = None,
        scale: Optional[float] = None,
        allow_upscale: bool = False,
        draw_cursor: bool = False,
        cursor_position: Optional[Tuple[int, int]] = None,
        monitor: Optional[int] = None,
        all_screens: bool = False,
    ) -> Capture:
        """抓一帧并编码。``region`` 为 None 时抓整屏。

        ``monitor`` / ``all_screens`` 见 :meth:`grab_frame`。``region`` 一律是
        **帧内坐标** (相对这一帧的左上角, 不是虚拟桌面坐标) —— 抓的是哪块屏由
        ``monitor``/``all_screens`` 决定, 换算回鼠标坐标要加上 :attr:`Capture.origin`。
        """
        started = time.time()
        fmt = (fmt or "png").lower()
        if fmt == "jpg":
            fmt = "jpeg"
        if fmt not in self.FORMATS:
            raise ValueError(f"不支持的图片格式 {fmt!r}, 可选: {', '.join(sorted(self.FORMATS))}")

        region_obj = Region.from_any(region)
        image, origin = self.grab_frame(monitor=monitor, all_screens=all_screens)
        source_width, source_height = image.size

        if region_obj is not None:
            right = min(region_obj.left + region_obj.width, source_width)
            bottom = min(region_obj.top + region_obj.height, source_height)
            if region_obj.left >= source_width or region_obj.top >= source_height:
                raise ValueError(
                    f"区域 {region_obj.to_dict()} 超出屏幕 {source_width}x{source_height}"
                )
            image = image.crop((region_obj.left, region_obj.top, right, bottom))

        # 缩放
        factor = float(scale) if scale else 1.0
        if max_width:
            factor = min(factor, float(max_width) / image.width)
        if max_height:
            factor = min(factor, float(max_height) / image.height)
        if not allow_upscale:
            factor = min(factor, 1.0)
        if abs(factor - 1.0) > 1e-6 and factor > 0:
            image = image.resize(
                (max(1, round(image.width * factor)), max(1, round(image.height * factor))),
                Image.LANCZOS,
            )
        else:
            factor = 1.0

        cursor_info = None
        if draw_cursor:
            cursor_info = self._draw_cursor(
                image, region_obj, cursor_position, factor, origin
            )

        data = self._encode(image, fmt, quality)
        return Capture(
            data=data,
            format=fmt,
            width=image.width,
            height=image.height,
            source_width=source_width,
            source_height=source_height,
            region=region_obj,
            scale=factor,
            captured_at=started,
            duration_ms=(time.time() - started) * 1000.0,
            cursor=cursor_info,
            origin=origin,
        )

    @staticmethod
    def _encode(image: Image.Image, fmt: str, quality: int) -> bytes:
        buf = io.BytesIO()
        if fmt in ("jpeg", "webp"):
            img = image if image.mode == "RGB" else image.convert("RGB")
            img.save(buf, ScreenCapture.FORMATS[fmt], quality=int(quality), optimize=True)
        else:
            image.save(buf, "PNG", optimize=True)
        return buf.getvalue()

    @staticmethod
    def _draw_cursor(
        image: Image.Image,
        region: Optional[Region],
        cursor_position: Optional[Tuple[int, int]],
        factor: float,
        origin: Tuple[int, int] = (0, 0),
    ) -> Optional[dict]:
        """把当前光标画成十字+圆圈 (截图本身不含光标)。

        ``pynput`` 在这里是唯一需要它的地方 —— 拿光标位置。

        **光标坐标是虚拟桌面坐标**: 抓的不是原点那一帧时, 得先减掉 ``origin``
        才能落到图上 —— 否则光标会被画偏一整块屏 (副屏在左边时差 1920px)。
        """
        pos = cursor_position
        if pos is None:
            try:
                from pynput.mouse import Controller

                pos = Controller().position
            except Exception:
                return None
        try:
            x, y = int(pos[0]), int(pos[1])
        except Exception:
            return None

        left = (region.left if region else 0) + int(origin[0])
        top = (region.top if region else 0) + int(origin[1])
        rel_x, rel_y = (x - left) * factor, (y - top) * factor
        if not (0 <= rel_x < image.width and 0 <= rel_y < image.height):
            return {"x": x, "y": y, "in_frame": False}

        from PIL import ImageDraw

        draw = ImageDraw.Draw(image)
        cx, cy = int(rel_x), int(rel_y)
        arm, color = CURSOR_ARM, CURSOR_COLOR
        draw.line((cx - arm, cy, cx + arm, cy), fill=color, width=2)
        draw.line((cx, cy - arm, cx, cy + arm), fill=color, width=2)
        draw.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), outline=color, width=2)
        return {"x": x, "y": y, "in_frame": True, "frame_x": cx, "frame_y": cy}
