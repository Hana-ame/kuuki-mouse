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
  * **不做多显示器枚举** —— ``ImageGrab.grab()`` 抓的是整个虚拟桌面, 这就是
    唯一有意义的"屏幕"。要抓局部用 ``region`` 裁剪。

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
    def grab_image(self) -> Image.Image:
        """抓整个屏幕。``ImageGrab.grab()`` 在 Windows/macOS 原生可用,
        Linux 上走 X11 (WSLg 的 XWayland 不支持 root GetImage, 会抛异常)。"""
        kwargs: Dict[str, object] = {}
        import sys

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
    ) -> Capture:
        """抓一帧并编码。``region`` 为 None 时抓整屏。"""
        started = time.time()
        fmt = (fmt or "png").lower()
        if fmt == "jpg":
            fmt = "jpeg"
        if fmt not in self.FORMATS:
            raise ValueError(f"不支持的图片格式 {fmt!r}, 可选: {', '.join(sorted(self.FORMATS))}")

        region_obj = Region.from_any(region)
        image = self.grab_image()
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
            cursor_info = self._draw_cursor(image, region_obj, cursor_position, factor)

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
    ) -> Optional[dict]:
        """把当前光标画成十字+圆圈 (截图本身不含光标)。

        ``pynput`` 在这里是唯一需要它的地方 —— 拿光标位置。
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

        left = region.left if region else 0
        top = region.top if region else 0
        rel_x, rel_y = (x - left) * factor, (y - top) * factor
        if not (0 <= rel_x < image.width and 0 <= rel_y < image.height):
            return {"x": x, "y": y, "in_frame": False}

        from PIL import ImageDraw

        draw = ImageDraw.Draw(image)
        cx, cy = int(rel_x), int(rel_y)
        arm, color = 12, (255, 0, 0)
        draw.line((cx - arm, cy, cx + arm, cy), fill=color, width=2)
        draw.line((cx, cy - arm, cx, cy + arm), fill=color, width=2)
        draw.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), outline=color, width=2)
        return {"x": x, "y": y, "in_frame": True, "frame_x": cx, "frame_y": cy}
