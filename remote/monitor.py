"""显示器枚举与虚拟桌面边界 (Windows / ctypes, 不引第三方依赖)。

为什么要这个模块
----------------
坐标校准 (``screen.calibrate``) 回答的是「图坐标怎么换成鼠标坐标」, 而它的价值
恰恰在**虚拟桌面原点不是 (0,0)** 的时候 —— 副屏在左边时原点可能是 (-1920, 0)。
单屏机器上这件事体现不出来 (实测 origin ≈ (0.5, -0.7), 残差 0.44px), 但一到多屏,
「截图是哪块屏、鼠标坐标又是哪套坐标」就必须先说清楚, 否则校准出来的系数只对
其中一块屏成立。

所以先补这一层: 调用方可以问「有几块屏、各自的矩形、哪块是主屏、虚拟桌面边界
在哪」, 再决定校准该在哪个区域采样、截图该抓哪块。

实现取舍
--------
- 只用 ctypes 调 Win32 (``EnumDisplayMonitors`` + ``GetMonitorInfoW`` +
  ``GetSystemMetrics``), 与 ``window.py`` 同一路子。
- **只读**: 不改显示器排列, 也不动进程的 DPI 感知状态 (那个是有副作用的全局
  设置, 调用方要自己决定)。
- 非 Windows 上 ``monitor_supported()`` 返回 False —— 受控端本来就只支持
  Windows, 这里不给假数据。
"""

from __future__ import annotations

import ctypes
import os
from typing import Any, Dict, List, Optional

__all__ = [
    "monitor_supported",
    "list_monitors",
    "monitor_at",
    "virtual_screen",
    "MonitorError",
]

#: ``GetSystemMetrics`` 的索引
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79
_SM_CMONITORS = 80

#: ``MONITORINFO.dwFlags``: 主显示器
_MONITORINFOF_PRIMARY = 0x00000001
_CCHDEVICENAME = 32


class MonitorError(Exception):
    """显示器相关的业务异常: 系统不支持 / 坐标不在任何一块屏上。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MONITORINFOEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", ctypes.c_ulong),
        ("szDevice", ctypes.c_wchar * _CCHDEVICENAME),
    ]


class _Win32:
    """惰性加载的 Win32 绑定。非 Windows 上一切成员都是 None。"""

    def __init__(self):
        self.ok = False
        self.user32 = None
        if os.name != "nt":
            return
        try:
            self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        except OSError:  # pragma: no cover - 极端环境
            return
        self._declare()
        self.ok = True

    def _declare(self) -> None:
        user32 = self.user32
        # 回调类型必须挂在实例上: 被 GC 之后回调会崩
        self._enum_proc = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p,
            ctypes.POINTER(_RECT), ctypes.c_long,
        )
        user32.EnumDisplayMonitors.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, self._enum_proc, ctypes.c_long,
        ]
        user32.EnumDisplayMonitors.restype = ctypes.c_bool
        user32.GetMonitorInfoW.argtypes = [ctypes.c_ulong, ctypes.POINTER(_MONITORINFOEX)]
        user32.GetMonitorInfoW.restype = ctypes.c_bool
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.GetSystemMetrics.restype = ctypes.c_int


_WIN = _Win32()


def monitor_supported() -> bool:
    """这台机器能不能枚举显示器 (目前只有 Windows 支持)。"""
    return bool(_WIN.ok)


def _require() -> _Win32:
    if not _WIN.ok:
        raise MonitorError("unsupported", "显示器枚举只在 Windows 上可用")
    return _WIN


def _rect_of(rc: _RECT) -> Dict[str, int]:
    """RECT -> 矩形字典。

    字段**只有** left/top/width/height: 与 ``window.py`` 的窗口矩形、proto 的
    ``Rect`` 消息同一个格式。多加 right/bottom 会让 WS 侧的返回值比 gRPC 侧多
    两个键, 跨传输比对直接不等。
    """
    return {
        "left": int(rc.left),
        "top": int(rc.top),
        "width": max(0, int(rc.right - rc.left)),
        "height": max(0, int(rc.bottom - rc.top)),
    }


def _collect(win: _Win32) -> List[Dict[str, Any]]:
    """枚举所有显示器。hdc / 剪辑矩形都传 NULL —— 那才是「虚拟桌面上的全部」。"""
    found: List[Dict[str, Any]] = []

    def _callback(handle, _hdc, _lprc, _lparam):
        info = _MONITORINFOEX()
        info.cbSize = ctypes.sizeof(_MONITORINFOEX)
        if not win.user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            return 1  # 拿不到信息就跳过, 继续枚举
        found.append({
            "handle": int(handle),
            "device": info.szDevice,
            "rect": _rect_of(info.rcMonitor),
            "work": _rect_of(info.rcWork),
            "primary": bool(info.dwFlags & _MONITORINFOF_PRIMARY),
        })
        return 1

    win.user32.EnumDisplayMonitors(None, None, win._enum_proc(_callback), 0)
    return found


def virtual_screen(win: Optional[_Win32] = None) -> Dict[str, int]:
    """虚拟桌面 (所有显示器拼起来) 的边界。

    注意这里的坐标**可以是负数**: 副屏排在左边时左边界就是负的。这正是「只看
    ``screen.size`` 会算错」的根源。
    """
    win = win or _require()
    get = win.user32.GetSystemMetrics
    left = get(_SM_XVIRTUALSCREEN)
    top = get(_SM_YVIRTUALSCREEN)
    width = get(_SM_CXVIRTUALSCREEN)
    height = get(_SM_CYVIRTUALSCREEN)
    # 与 _rect_of 同款: left/top/width/height
    return {"left": left, "top": top, "width": width, "height": height}


def list_monitors() -> Dict[str, Any]:
    """列出所有显示器 + 虚拟桌面边界。

    返回::

        {"count": n, "virtual": {...}, "primary_index": i, "monitors": [...]}

    ``monitors`` 按系统枚举顺序, 每项含 ``rect`` (整块屏) / ``work`` (去掉任务栏
    的工作区) / ``primary`` / ``device``。
    """
    win = _require()
    monitors = _collect(win)
    for index, item in enumerate(monitors):
        item["index"] = index
    primary_index = next(
        (i for i, item in enumerate(monitors) if item["primary"]), None
    )
    # 键名 ``virtual_screen`` 跟着 proto 的消息字段走: 控制端会把 protobuf 转成
    # dict 与 WS 的结果逐字段比对, 键名不一致就等于结果不一致
    return {
        "count": len(monitors),
        "virtual_screen": virtual_screen(win),
        "primary_index": primary_index,
        "monitors": monitors,
    }


def monitor_at(x: int, y: int) -> Dict[str, Any]:
    """点 (虚拟桌面坐标, 可以是负数) 落在哪块屏上。

    给校准/定位用: 拿到目标坐标先问一句它在哪块屏, 再决定用哪套换算。
    点不在任何一块屏上 (比如跨屏缝隙) 时抛 ``MonitorError``。
    """
    win = _require()
    monitors = _collect(win)
    for index, item in enumerate(monitors):
        rect = item["rect"]
        if (rect["left"] <= x < rect["left"] + rect["width"]
                and rect["top"] <= y < rect["top"] + rect["height"]):
            item["index"] = index
            return item
    raise MonitorError(
        "not_found",
        f"坐标 ({x},{y}) 不在任何一块显示器上 (虚拟桌面 {virtual_screen(win)})",
    )
