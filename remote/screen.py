"""截屏后端与 ScreenCapture 管理器。

设计要点
--------
* **多后端自动降级**: ``mss`` -> ``Pillow.ImageGrab`` -> ``ffmpeg`` (x11grab)。
  某个后端在抓图时抛异常会被记入失败名单, 之后的抓取直接跳过它,
  不会每次都白等一次超时。
* **区域裁剪 / 缩放 / 光标叠加统一在管理器里做**——各后端只负责
  "抓一张全屏 RGB 图", 实现面最小, 也保证不同后端的行为一致。
* 后端只探测一次 (``ScreenCapture.backends()`` 带缓存), 但每次抓取都会
  重新确认可用性 (例如 DISPLAY 中途消失)。

本机实测 (WSLg, ``DISPLAY=:0``, 1680x1050, 2026-09-19)
-----------------------------------------------------
* ``PIL.ImageGrab.grab()`` -> ``OSError: X get_image failed: error 8``
  (BadMatch)。WSLg 的 XWayland 不支持在 root window 上 ``GetImage``,
  显式 ``xdisplay`` / ``bbox`` / 各种 plane_mask 都一样。
* ``python-xlib`` 直接 ``root.get_image()`` 同样 BadMatch (同一原因)。
* ``ffmpeg -f x11grab -video_size WxH -i :0.0 -frames:v 1 -f image2pipe -vcodec png -``
  **可用** —— 所以本机实际生效的是 ffmpeg 后端。
* ``mss`` 未安装 (装了就优先用它, 它走 XShm, 最快)。

屏幕尺寸探测: ``python-xlib`` (pynput 在 Linux 上的依赖, 必然存在) ->
``xdpyinfo`` -> 环境变量覆盖 ``KUUKI_SCREEN_SIZE=WxH`` -> 兜底 1920x1080。
"""

from __future__ import annotations

import io
import importlib.util
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageGrab

__all__ = [
    "Region",
    "Monitor",
    "Capture",
    "MssBackend",
    "PillowBackend",
    "FFmpegBackend",
    "ScreenCapture",
]

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
        """接受 None / dict / [l,t,w,h] / (l,t,w,h) 四种写法。"""
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
class Monitor:
    """一块屏幕。index=0 约定为"整个虚拟桌面", 1..N 为各物理屏。"""

    index: int
    left: int
    top: int
    width: int
    height: int
    primary: bool = False
    name: str = ""

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
            "primary": self.primary,
            "name": self.name,
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
    backend: str
    monitor: int = 0
    region: Optional[Region] = None
    scale: float = 1.0
    captured_at: float = field(default_factory=time.time)
    duration_ms: float = 0.0
    cursor: Optional[dict] = None

    @property
    def bytes(self) -> int:
        return len(self.data)

    def to_dict(self, include_image: bool = False, image_b64: Optional[str] = None) -> dict:
        out = {
            "format": self.format,
            "width": self.width,
            "height": self.height,
            "source_width": self.source_width,
            "source_height": self.source_height,
            "backend": self.backend,
            "monitor": self.monitor,
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


# ---------------------------------------------------------------- 后端


class _Backend:
    """后端接口: 只负责"抓一张全屏 PIL 图"与"报告屏幕尺寸/列表"。"""

    name = "base"
    #: 在自动模式里的排序权重, 越小越先试
    priority = 100

    def available(self) -> Tuple[bool, str]:
        """返回 (是否可用, 不可用原因)。"""
        raise NotImplementedError

    def screen_size(self) -> Tuple[int, int]:
        raise NotImplementedError

    def monitors(self) -> List[Monitor]:
        width, height = self.screen_size()
        return [Monitor(0, 0, 0, width, height, primary=True, name="virtual-desktop")]

    def grab_image(self) -> Image.Image:
        raise NotImplementedError


class MssBackend(_Backend):
    """``mss``: 跨平台、走 XShm / Win32 / CoreGraphics, 最快。"""

    name = "mss"
    priority = 10

    def available(self) -> Tuple[bool, str]:
        if importlib.util.find_spec("mss") is None:
            return False, "mss 未安装"
        if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
            return False, "无 DISPLAY 环境变量"
        return True, ""

    def _sct(self):
        import mss

        return mss.mss()

    def screen_size(self) -> Tuple[int, int]:
        with self._sct() as sct:
            mon = sct.monitors[0]
            return int(mon["width"]), int(mon["height"])

    def monitors(self) -> List[Monitor]:
        with self._sct() as sct:
            out: List[Monitor] = []
            for idx, mon in enumerate(sct.monitors):
                out.append(
                    Monitor(
                        index=idx,
                        left=int(mon["left"]),
                        top=int(mon["top"]),
                        width=int(mon["width"]),
                        height=int(mon["height"]),
                        primary=(idx == 1),
                        name="virtual-desktop" if idx == 0 else f"monitor-{idx}",
                    )
                )
            return out

    def grab_image(self) -> Image.Image:
        with self._sct() as sct:
            shot = sct.grab(sct.monitors[0])
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


class PillowBackend(_Backend):
    """``PIL.ImageGrab``: Windows / macOS 的首选; Linux X11 视服务器实现而定。

    注意: WSLg 的 XWayland 上会 ``BadMatch`` (见模块 docstring), 所以它只作为
    降级链的一环, 失败后会被自动跳过。
    """

    name = "pillow"
    priority = 20

    def available(self) -> Tuple[bool, str]:
        if not hasattr(ImageGrab, "grab"):  # pragma: no cover
            return False, "Pillow ImageGrab 不可用"
        if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
            return False, "无 DISPLAY 环境变量"
        return True, ""

    def screen_size(self) -> Tuple[int, int]:
        return _probe_screen_size()

    def grab_image(self) -> Image.Image:
        kwargs: Dict[str, object] = {}
        if sys.platform.startswith("linux"):
            kwargs["xdisplay"] = os.environ.get("DISPLAY") or ":0"
        img = ImageGrab.grab(**kwargs)
        if img.mode != "RGB":
            img = img.convert("RGB")
        return img


class FFmpegBackend(_Backend):
    """``ffmpeg -f x11grab``: Linux/X11 上不依赖 X 扩展, 兼容性最好。

    走 ``image2pipe`` 直接从 stdout 拿 PNG, 不落临时文件。
    """

    name = "ffmpeg"
    priority = 30

    def __init__(self, display: Optional[str] = None, size: Optional[Tuple[int, int]] = None):
        self.display = display or os.environ.get("DISPLAY") or ":0"
        self._size = size
        self._lock = threading.Lock()

    def available(self) -> Tuple[bool, str]:
        if not sys.platform.startswith("linux"):
            return False, "ffmpeg x11grab 仅用于 Linux"
        if not shutil.which("ffmpeg"):
            return False, "未找到 ffmpeg"
        if not self.display:
            return False, "无 DISPLAY 环境变量"
        return True, ""

    def screen_size(self) -> Tuple[int, int]:
        with self._lock:
            if self._size is None:
                self._size = _probe_screen_size(self.display)
            return self._size

    def grab_image(self) -> Image.Image:
        width, height = self.screen_size()
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "x11grab",
            "-video_size",
            f"{width}x{height}",
            "-i",
            self.display,
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
        if proc.returncode != 0 or not proc.stdout:
            err = proc.stderr.decode("utf-8", "replace").strip()[:400]
            raise RuntimeError(f"ffmpeg x11grab 失败 (rc={proc.returncode}): {err}")
        img = Image.open(io.BytesIO(proc.stdout))
        img.load()
        if img.mode != "RGB":
            img = img.convert("RGB")
        return img


# ---------------------------------------------------------------- 屏幕尺寸探测


def _probe_screen_size(display: Optional[str] = None) -> Tuple[int, int]:
    """探测屏幕尺寸。

    顺序: 环境变量覆盖 -> Windows (GetSystemMetrics) -> X11 (Xlib -> xdpyinfo)
    -> 兜底 1920x1080。

    注意 Windows 必须用 GetSystemMetrics(0/1) 拿**主屏**尺寸, 与
    ``PIL.ImageGrab.grab()`` 的全屏抓取范围一致; 早先漏了这个分支, Windows 上会
    静默落到 1920x1080 兜底值, 与实际屏幕(如 1680x1050)不符。
    """
    override = os.environ.get("KUUKI_SCREEN_SIZE")
    if override and "x" in override.lower():
        try:
            w, h = override.lower().split("x", 1)
            return int(w), int(h)
        except Exception:
            pass

    if sys.platform.startswith("win"):
        try:
            import ctypes

            user32 = ctypes.windll.user32
            try:  # 先声明 DPI 感知, 否则缩放屏上拿到的是虚拟化后的尺寸
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                try:
                    user32.SetProcessDPIAware()
                except Exception:
                    pass
            width, height = int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
            if width > 0 and height > 0:
                return width, height
        except Exception:
            pass

    display = display or os.environ.get("DISPLAY") or ":0"

    try:
        from Xlib import display as xdisplay

        d = xdisplay.Display(display)
        screen = d.screen()
        width, height = int(screen.width_in_pixels), int(screen.height_in_pixels)
        if width > 0 and height > 0:
            return width, height
    except Exception:
        pass

    if shutil.which("xdpyinfo"):
        try:
            out = subprocess.run(
                ["xdpyinfo", "-display", display], capture_output=True, timeout=10
            ).stdout.decode("utf-8", "replace")
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("dimensions:"):
                    dims = line.split()[1]
                    w, h = dims.split("x")[:2]
                    return int(w), int(h)
        except Exception:
            pass

    return 1920, 1080


# ---------------------------------------------------------------- 管理器


class ScreenCapture:
    """抓屏管理器: 选后端、裁剪、缩放、编码、叠加光标。"""

    FORMATS = {"png": "PNG", "jpg": "JPEG", "jpeg": "JPEG", "webp": "WEBP"}

    def __init__(self, backend: str = "auto", display: Optional[str] = None):
        self._forced = (backend or "auto").lower()
        self._display = display
        self._lock = threading.Lock()
        self._active: Optional[_Backend] = None
        self._failed: Dict[str, str] = {}
        self._catalog: List[_Backend] = [
            MssBackend(),
            PillowBackend(),
            FFmpegBackend(display=display),
        ]

    # ---- 后端选择 ----
    def _candidates(self) -> List[_Backend]:
        if self._forced in ("", "auto"):
            return sorted(self._catalog, key=lambda b: b.priority)
        picked = [b for b in self._catalog if b.name == self._forced]
        if not picked:
            raise ValueError(
                f"未知后端 {self._forced!r}, 可选: auto / " + " / ".join(b.name for b in self._catalog)
            )
        return picked

    def backend_report(self) -> List[dict]:
        """所有后端的可用性报告 (给 info / 排障用)。"""
        out = []
        for backend in self._catalog:
            ok, reason = backend.available()
            out.append(
                {
                    "name": backend.name,
                    "available": bool(ok),
                    "reason": reason,
                    "failed_before": self._failed.get(backend.name),
                }
            )
        return out

    def active_backend(self) -> _Backend:
        """返回第一个真正能用的后端 (带缓存)。"""
        with self._lock:
            if self._active is not None:
                return self._active
            errors = []
            for backend in self._candidates():
                if backend.name in self._failed:
                    continue
                ok, reason = backend.available()
                if not ok:
                    errors.append(f"{backend.name}: {reason}")
                    continue
                # available() 通过后**真抓一张图**验证一次 (只在选择时做一次)。
                # 不能只用 screen_size() 验证: Pillow 后端的 screen_size 走 Xlib 探测,
                # 在 WSLg 上能过, 但 grab_image 会 BadMatch —— 那样 info 会报一个
                # 根本抓不了图的后端, 而且每次首个请求都要白付一次失败。
                try:
                    probe = backend.grab_image()
                    probe.close()
                except Exception as exc:
                    self._failed[backend.name] = f"试抓失败: {exc}"
                    errors.append(f"{backend.name}: {exc}")
                    continue
                self._active = backend
                return backend
            raise RuntimeError("没有可用的截屏后端: " + ("; ".join(errors) or "全部已被标记失败"))

    def _mark_failed(self, backend: _Backend, exc: Exception) -> None:
        with self._lock:
            self._failed[backend.name] = f"{exc.__class__.__name__}: {exc}"
            if self._active is backend:
                self._active = None

    # ---- 对外 ----
    def monitors(self) -> List[Monitor]:
        backend = self.active_backend()
        try:
            return backend.monitors()
        except Exception as exc:
            self._mark_failed(backend, exc)
            raise

    def screen_size(self) -> Tuple[int, int]:
        backend = self.active_backend()
        try:
            return backend.screen_size()
        except Exception as exc:
            self._mark_failed(backend, exc)
            raise

    def capture(
        self,
        region=None,
        monitor: int = 0,
        fmt: str = "png",
        quality: int = 80,
        max_width: Optional[int] = None,
        max_height: Optional[int] = None,
        scale: Optional[float] = None,
        allow_upscale: bool = False,
        draw_cursor: bool = False,
        cursor_position: Optional[Tuple[int, int]] = None,
    ) -> Capture:
        """抓一帧并编码。``region`` 优先于 ``monitor``。"""
        started = time.time()
        fmt = (fmt or "png").lower()
        if fmt == "jpg":
            fmt = "jpeg"
        if fmt not in self.FORMATS:
            raise ValueError(f"不支持的图片格式 {fmt!r}, 可选: {', '.join(sorted(self.FORMATS))}")

        region_obj = Region.from_any(region)
        backend = self.active_backend()

        # 抓全屏 (带一次降级重试)
        try:
            image = backend.grab_image()
        except Exception as exc:
            self._mark_failed(backend, exc)
            backend = self.active_backend()
            image = backend.grab_image()

        source_width, source_height = image.size

        if region_obj is None and monitor:
            for mon in backend.monitors():
                if mon.index == monitor:
                    region_obj = Region(mon.left, mon.top, mon.width, mon.height)
                    break

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
            new_size = (max(1, round(image.width * factor)), max(1, round(image.height * factor)))
            image = image.resize(new_size, Image.LANCZOS)
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
            backend=backend.name,
            monitor=monitor,
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
        """把当前光标画成一个十字 + 圆圈 (截图本身不含光标)。

        返回光标在**输出图**里的坐标; 光标不在截图范围内时返回 None。
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
        rel_x = (x - left) * factor
        rel_y = (y - top) * factor
        if not (0 <= rel_x < image.width and 0 <= rel_y < image.height):
            return {"x": x, "y": y, "in_frame": False}

        from PIL import ImageDraw

        draw = ImageDraw.Draw(image)
        cx, cy = int(rel_x), int(rel_y)
        arm = 12
        color = (255, 0, 0)
        draw.line((cx - arm, cy, cx + arm, cy), fill=color, width=2)
        draw.line((cx, cy - arm, cx, cy + arm), fill=color, width=2)
        draw.ellipse((cx - 5, cy - 5, cx + 5, cy + 5), outline=color, width=2)
        return {"x": x, "y": y, "in_frame": True, "frame_x": cx, "frame_y": cy}
