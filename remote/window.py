"""窗口枚举与前台切换 (Windows / ctypes, 不引第三方依赖)。

为什么要这个模块
----------------
纯视觉定位有语义天花板: 一排彩色小图标 + 浅色界面, 浏览器标签栏和聊天工具
的会话标签栏长得一模一样, 截屏看不出「现在前台是谁」。于是「先 locate 再 click」
的自动化流程会把点击送进错误的窗口 (踩过, 见
``docs/knowledge/gui-window-focus-gap.md``)。

补上这一层之后, 调用方可以先问「有哪些窗口、前台是哪个」, 用**标题 / 进程名**
而不是坐标来认窗口, 再把它切到前台 —— 视觉定位只负责「窗口里的哪里」。

实现取舍
--------
- 只用 ctypes 调 Win32, 不加 pywin32 / psutil: 受控端要能塞进最小依赖里跑。
- 只列**用户看得到**的顶层窗口: 隐藏的、被 DWM 隐藏 (cloaked, UWP 后台窗口)
  的一律过滤掉; 要了也没意义, 还会把列表搅乱。
- 切前台不能只调一次 ``SetForegroundWindow``: Windows 有前台锁, 后台进程直接
  抢前台会被拒绝。这里按「正常 → 挂输入队列 → 敲一下 Alt」三级退让, 并把
  最终**真的**是不是前台回报给调用方 (不撒谎说成功了)。
"""

from __future__ import annotations

import ctypes
import os
import time
from typing import Any, Dict, List, Optional

__all__ = [
    "window_supported",
    "list_windows",
    "foreground_window",
    "focus_window",
    "WindowError",
]

#: ``window.focus`` 切完之后最多等多久再确认前台 (秒)
DEFAULT_FOCUS_WAIT = 0.5

_SW_RESTORE = 9
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_DWMWA_CLOAKED = 14


class WindowError(Exception):
    """窗口操作的业务异常: 没找到 / 有多个候选 / 系统不支持。"""

    def __init__(self, code: str, message: str, candidates: Optional[List[dict]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.candidates = candidates or []


# ================================================================ Win32 绑定


class _Win32:
    """惰性加载的 Win32 绑定。非 Windows 上一切成员都是 None。"""

    def __init__(self):
        self.ok = False
        self.user32 = None
        self.kernel32 = None
        self.dwmapi = None
        if os.name != "nt":
            return
        try:
            self.user32 = ctypes.WinDLL("user32", use_last_error=True)
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except OSError:  # pragma: no cover - 极端环境
            return
        # dwmapi 只在 Win7+ 上有; 没有就退化成"不检查 cloaked", 不是致命问题
        try:
            self.dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        except OSError:  # pragma: no cover
            self.dwmapi = None
        self._declare()
        self.ok = True

    def _declare(self) -> None:
        user32, kernel32 = self.user32, self.kernel32
        # EnumWindows 的回调必须保持引用, 否则被 GC 后回调会崩
        self._enum_proc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
        )
        user32.EnumWindows.argtypes = [self._enum_proc, ctypes.c_void_p]
        user32.EnumWindows.restype = ctypes.c_bool
        user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        user32.IsWindowVisible.restype = ctypes.c_bool
        user32.IsIconic.argtypes = [ctypes.c_void_p]
        user32.IsIconic.restype = ctypes.c_bool
        user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        user32.SetForegroundWindow.restype = ctypes.c_bool
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.ShowWindow.restype = ctypes.c_bool
        user32.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool]
        user32.AttachThreadInput.restype = ctypes.c_bool
        user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ulong, ctypes.c_ulong]
        user32.keybd_event.restype = None
        user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
        user32.GetWindowRect.restype = ctypes.c_bool

        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = ctypes.c_ulong
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        kernel32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)
        ]
        kernel32.QueryFullProcessImageNameW.restype = ctypes.c_bool
        if self.dwmapi is not None:
            self.dwmapi.DwmGetWindowAttribute.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong
            ]
            self.dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


_WIN = _Win32()


def window_supported() -> bool:
    """这台机器能不能枚举/切换窗口 (目前只有 Windows 支持)。"""
    return bool(_WIN.ok)


def _require() -> _Win32:
    if not _WIN.ok:
        raise WindowError("unsupported", "窗口操作需要被控端是 Windows (当前不是)")
    return _WIN


# ================================================================ 枚举


def _title_of(win: _Win32, hwnd: int) -> str:
    length = win.user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    win.user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _process_name_of(win: _Win32, pid: int) -> str:
    """进程名 (不含路径与 .exe 后缀)。拿不到就返回空串 —— 标题已经够用了。"""
    handle = win.kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = ctypes.c_ulong(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        ok = win.kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        )
        if not ok:
            return ""
        name = os.path.basename(buffer.value)
        if name.lower().endswith(".exe"):
            name = name[:-4]
        return name
    finally:
        win.kernel32.CloseHandle(handle)


def _is_cloaked(win: _Win32, hwnd: int) -> bool:
    """被 DWM 隐藏的窗口 (UWP 后台、部分系统窗口) 用户看不见, 要排除。"""
    if win.dwmapi is None:
        return False
    value = ctypes.c_ulong(0)
    try:
        win.dwmapi.DwmGetWindowAttribute(
            hwnd, _DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value)
        )
    except OSError:  # pragma: no cover
        return False
    return bool(value.value)


def _rect_of(win: _Win32, hwnd: int) -> Dict[str, int]:
    rect = _RECT()
    if not win.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return {"left": 0, "top": 0, "width": 0, "height": 0}
    return {
        "left": int(rect.left),
        "top": int(rect.top),
        "width": max(0, int(rect.right - rect.left)),
        "height": max(0, int(rect.bottom - rect.top)),
    }


def _info_of(win: _Win32, hwnd: int, foreground: int) -> Dict[str, Any]:
    pid = ctypes.c_ulong(0)
    win.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return {
        "hwnd": int(hwnd),
        "title": _title_of(win, hwnd),
        "process": _process_name_of(win, int(pid.value)),
        "pid": int(pid.value),
        "rect": _rect_of(win, hwnd),
        "visible": bool(win.user32.IsWindowVisible(hwnd)),
        "minimized": bool(win.user32.IsIconic(hwnd)),
        "foreground": int(hwnd) == int(foreground or 0),
    }


def _matches(item: dict, title: str = "", process: str = "") -> bool:
    """标题 / 进程名子串匹配 (不区分大小写)。两个条件都给则都要满足。"""
    if title and title.lower() not in (item.get("title") or "").lower():
        return False
    if process:
        haystack = f"{item.get('process') or ''} {item.get('pid') or ''}".lower()
        if process.lower() not in haystack:
            return False
    return True


def list_windows(
    title: str = "",
    process: str = "",
    include_hidden: bool = False,
    limit: int = 0,
) -> List[Dict[str, Any]]:
    """列出顶层窗口, 按 Z 序 (最靠前的在最前)。

    :param title: 标题子串过滤
    :param process: 进程名或 pid 子串过滤 (``msedge`` / ``1204``)
    :param include_hidden: 把隐藏的、被 DWM 隐藏的也算进来 (调试用)
    :param limit: 最多返回几个, 0 = 不限
    """
    win = _require()
    foreground = int(win.user32.GetForegroundWindow() or 0)
    found: List[Dict[str, Any]] = []

    @_WIN._enum_proc  # type: ignore[attr-defined]
    def _callback(hwnd, _lparam):
        if not include_hidden:
            if not win.user32.IsWindowVisible(hwnd) or _is_cloaked(win, hwnd):
                return True
        item = _info_of(win, hwnd, foreground)
        if not include_hidden and not item["title"]:
            # 无标题窗口通常是隐形helper窗口 —— 用户看不到也点不到
            return True
        if _matches(item, title, process):
            found.append(item)
        return True

    win.user32.EnumWindows(_callback, 0)
    if limit and limit > 0:
        found = found[:limit]
    return found


def foreground_window() -> Optional[Dict[str, Any]]:
    """当前前台窗口; 拿不到 (锁屏 / 无窗口) 返回 None。"""
    win = _require()
    hwnd = int(win.user32.GetForegroundWindow() or 0)
    if not hwnd:
        return None
    return _info_of(win, hwnd, hwnd)


# ================================================================ 切前台


def _tap_alt(win: _Win32) -> None:
    """敲一下 Alt 再放开。

    Windows 的前台锁对「用户刚刚按过键」的进程网开一面, 这是后台进程抢前台的
    经典退路。这里用 ``keybd_event`` 而不是 pynput: 窗口层不该依赖输入库。
    """
    vk_menu = 0x12
    win.user32.keybd_event(vk_menu, 0, 0, 0)
    win.user32.keybd_event(vk_menu, 0, 0x0002, 0)  # KEYEVENTF_KEYUP


def _bring_to_front(win: _Win32, hwnd: int) -> str:
    """把窗口弄到前台, 返回用的是哪种办法 (失败返回空串)。"""
    if win.user32.IsIconic(hwnd):
        win.user32.ShowWindow(hwnd, _SW_RESTORE)

    if win.user32.SetForegroundWindow(hwnd):
        return "set-foreground"

    # 前台锁: 把我们的输入队列挂到当前前台线程上, 再试一次
    fg = int(win.user32.GetForegroundWindow() or 0)
    if fg and fg != hwnd:
        fg_thread = win.user32.GetWindowThreadProcessId(fg, None)
        my_thread = win.kernel32.GetCurrentThreadId()
        if fg_thread and fg_thread != my_thread:
            if win.user32.AttachThreadInput(fg_thread, my_thread, True):
                try:
                    if win.user32.SetForegroundWindow(hwnd):
                        return "attach-thread"
                finally:
                    win.user32.AttachThreadInput(fg_thread, my_thread, False)

    # 最后一招: 敲一下 Alt 骗过前台锁
    _tap_alt(win)
    if win.user32.SetForegroundWindow(hwnd):
        return "alt-key"
    return ""


def focus_window(
    hwnd: Optional[int] = None,
    title: str = "",
    process: str = "",
    index: int = 0,
    wait: float = DEFAULT_FOCUS_WAIT,
) -> Dict[str, Any]:
    """按 hwnd / 标题 / 进程名选中一个窗口并切到前台。

    :param hwnd: 直接给句柄 (最可靠); 给了它就不再按标题找
    :param title: 标题子串
    :param process: 进程名或 pid 子串
    :param index: 命中多个时取第几个 (按 Z 序, 0 = 最靠前那个)
    :param wait: 切完之后最多确认多久 (秒)
    :raises WindowError: ``not_found`` 没匹配或 index 越界 / ``unsupported`` 非
        Windows。命中多个**不报错**, 只取 ``index`` 指定的那个, 并把总数放在
        结果的 ``matched`` 里 —— 调用方看到 ``matched > 1`` 就该换更窄的过滤条件。
    """
    win = _require()

    matched = 1
    if hwnd is not None:
        target = _info_of(win, int(hwnd), int(win.user32.GetForegroundWindow() or 0))
    else:
        candidates = list_windows(title=title, process=process)
        matched = len(candidates)
        if not candidates:
            raise WindowError("not_found", f"没有匹配 title={title!r} process={process!r} 的窗口")
        if index >= len(candidates):
            raise WindowError(
                "not_found",
                f"匹配到 {len(candidates)} 个窗口, 取不到第 {index} 个",
                candidates,
            )
        target = candidates[index]

    handle = int(target["hwnd"])
    already = bool(target.get("foreground")) and not target.get("minimized")
    method = "already" if already else _bring_to_front(win, handle)

    # 切前台是异步的: 轮询确认, 但不要把"调用了"当成"生效了"
    focused = False
    deadline = time.time() + max(0.0, float(wait or 0.0))
    while True:
        focused = int(win.user32.GetForegroundWindow() or 0) == handle
        if focused or time.time() >= deadline:
            break
        time.sleep(0.02)

    return {
        "focused": focused,
        "hwnd": handle,
        "title": target.get("title", ""),
        "process": target.get("process", ""),
        "pid": target.get("pid", 0),
        "rect": target.get("rect", {}),
        "method": method,
        "matched": matched,
    }
