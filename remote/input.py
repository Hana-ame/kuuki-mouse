"""输入控制: 在 ``controller.PynputMouseController`` 之上补全远程控制需要的动作。

为什么是**扩展**而不是重写
--------------------------
``controller.py`` 已经把"鼠标/键盘各自一个 pynput 控制器"这个坑处理掉了
(双继承时 ``KeyboardController.tap`` 会解析到鼠标版 ``press``)。这里直接继承它,
只补远程控制需要的东西: 绝对/相对移动、按住/松开、拖拽 (两点或**路径点**)、
带时长的平滑移动、**多步平滑滚动** (可先定位再滚)、更全的特殊键映射、组合键
(可**按住时长**)、**长按单键**、剪贴板粘贴。

**pynput 的坑**: ``Controller.move(dx, dy)`` 是**相对**位移, 不是绝对坐标;
绝对定位必须用 ``Controller.position = (x, y)``。原 ``controller.move_mouse()``
名字容易误读成绝对移动, 所以这里显式分成 ``move_absolute`` / ``move_relative``。

**CJK 输入**: Linux/X11 下 pynput 的 ``type()`` 依赖 keysym 重映射, 中日韩字符
常常打不出来 (取决于 X 服务器与键盘布局)。需要输入中文时用 ``paste_text()``:
写剪贴板 + 发 Ctrl+V。剪贴板工具探测顺序见 ``clipboard_tool()``。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from pynput.keyboard import Controller as KeyboardController, Key, KeyCode
from pynput.mouse import Button, Controller as MouseController

_LINUX = sys.platform.startswith("linux")
# 注意: 受控端只支持 Windows (见 remote/__main__.py 的平台门禁), 所以 ``_LINUX``
# 在服务端**恒为 False**。下面所有由它分出的 X11 分支都保留作参考但**不再维护**,
# 也不会再被执行 —— 包括 ``_x11_reason`` 的纯查询式键预检。
# 保留的意义是那几条实测教训: WSLg 的 XWayland 上 pynput 的"借键"路径会把整条
# X 连接永久挂死, 以及 X11 下 pynput 打不出中文。

# ---- 复用项目根目录下的 controller.py (以脚本方式直接跑 remote/ 时补 sys.path) ----
try:  # pragma: no cover - 取决于启动方式
    from controller import PynputMouseController
except ImportError:  # pragma: no cover
    import pathlib

    _root = str(pathlib.Path(__file__).resolve().parent.parent)
    if _root not in sys.path:
        sys.path.insert(0, _root)
    from controller import PynputMouseController

__all__ = ["InputController", "KeyUnsupported", "resolve_key", "KEY_ALIASES"]


class KeyUnsupported(ValueError):
    """键在当前 X 键盘映射里不存在。

    为什么必须提前拦下 (本机实测, WSLg + XWayland, 2026-09-19)
    ---------------------------------------------------------
    pynput 遇到键盘映射里没有的键会走 "借键" 路径: 临时 ``change_keyboard_mapping``
    改键盘布局。在 WSLg 的 XWayland 上这一步**会把这条 X 连接的后续调用全部挂死**
    —— 实测 ``Key.f13`` 直接抛 ``AttributeError: 'BadRRModeError' object has no
    attribute 'sequence_number'``, 之后同一个连接上连 ``esc``/``a``/``enter``
    全部永久阻塞 (换新进程则一切正常, 说明是连接级污染)。

    所以这里在按键之前先做纯查询式的预检: 键解析不出 keysym, 或
    ``keysym_to_keycode`` 返回 0, 就直接拒绝, 绝不进入 borrowing 路径。
    中文/emoji 走 ``paste_text()``。
    """


# ---------------------------------------------------------------- 键名映射

#: 别名 -> pynput Key 的规范名 (小写)。规范名本身来自 ``pynput.keyboard.Key``。
KEY_ALIASES = {
    "control": "ctrl",
    "ctrl_l": "ctrl",
    "ctrl_r": "ctrl",
    "shift_l": "shift",
    "shift_r": "shift",
    "alt_l": "alt",
    "alt_r": "alt",
    "option": "alt",
    "altgr": "alt_gr",
    "cmd_l": "cmd",
    "cmd_r": "cmd",
    "super": "cmd",
    "super_l": "cmd",
    "super_r": "cmd",
    "win": "cmd",
    "windows": "cmd",
    "meta": "cmd",
    "command": "cmd",
    "return": "enter",
    "escape": "esc",
    "del": "delete",
    "ins": "insert",
    "pageup": "page_up",
    "pgup": "page_up",
    "pagedown": "page_down",
    "pgdn": "page_down",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
    "spacebar": "space",
    "space": "space",
    "printscreen": "print_screen",
    "prtsc": "print_screen",
    "capslock": "caps_lock",
    "numlock": "num_lock",
    "scrolllock": "scroll_lock",
    "playpause": "media_play_pause",
    "nexttrack": "media_next",
    "prevtrack": "media_previous",
    "volumeup": "media_volume_up",
    "volumedown": "media_volume_down",
    "volumemute": "media_volume_mute",
    "back": "browser_back",
    "forward": "browser_forward",
    "homepage": "browser_home",
    "search": "browser_search",
}

#: 由 pynput 的 Key 枚举反查出来的规范名 -> Key 对象
_SPECIAL: dict = {}
for _member in Key:  # type: ignore[union-attr]
    _SPECIAL[_member.name.lower()] = _member


def resolve_key(name: str):
    """把字符串解析成 pynput 的 ``Key`` 或单字符。

    - ``"enter"`` / ``"ctrl"`` / ``"f5"`` / ``"media_play_pause"`` -> ``Key`` 成员
    - ``"a"`` / ``"1"`` / ``","`` -> 原样返回的字符串 (交给 pynput 按字符处理)
    - ``"ctrl+c"`` 这种组合键会报错, 请用 ``hotkey()``
    """
    if name is None:
        raise ValueError("键名不能为空")
    raw = str(name)
    # 先把纯空白字符挑出来: 空格/制表符/换行本身就是有意义的键, 但它们 strip 之后
    # 会变成空串 —— 不先认下来就会被下面当成"没给键名"误拒 (实测: 控制端发
    # {"key": " "} 想敲一个空格, 服务端回 "键名不能为空")。
    if raw.strip():
        key = raw.strip().lower()
    else:
        key = {" ": "space", "\t": "tab", "\n": "enter", "\r": "enter"}.get(raw, "")
    if not key:
        raise ValueError("键名不能为空")
    key = KEY_ALIASES.get(key, key)
    if key in _SPECIAL:
        return _SPECIAL[key]
    if len(raw) == 1:
        return raw
    raise ValueError(f"未知键名 {name!r} (特殊键见 KEY_ALIASES / pynput.keyboard.Key)")


# ---------------------------------------------------------------- 剪贴板

_CLIPBOARD_CANDIDATES = (
    ("wl-copy", ["wl-copy"], "wayland"),
    ("xclip", ["xclip", "-selection", "clipboard"], "x11"),
    ("xsel", ["xsel", "--clipboard", "--input"], "x11"),
    ("pbcopy", ["pbcopy"], "macos"),
    ("clip", ["clip"], "windows"),
)


def clipboard_tool() -> Optional[Tuple[str, List[str]]]:
    """探测可用的剪贴板**命令行**写入工具, 返回 (名字, 命令前缀) 或 None。

    注意: Windows 上有更好的写法——直接用 Win32 API (见
    :func:`_set_clipboard_windows`)。这里保留 ``clip`` 只是作为**兜底**:
    它是外部进程, 会引发下面这个函数文档里说的编码问题。
    """
    for name, cmd, _kind in _CLIPBOARD_CANDIDATES:
        if shutil.which(name):
            return name, list(cmd)
    return None


def _set_clipboard_windows(text: str, retries: int = 10, delay: float = 0.05) -> bool:
    """用 Win32 API 直接写 ``CF_UNICODETEXT``, 成功返回 True。

    为什么绕开 ``clip.exe``
    ----------------------
    ``clip`` 按**控制台当前代码页**解释 stdin 的字节。中文 Windows 的活动代码页
    是 936 (GBK), 于是任何 UTF-8 输入都会被当成 GBK: 一个三字节的汉字会被拆成
    一个半 GBK 字, 粘出来就是 "浣犲ソ" 这类乱码 (实测: ``keyboard.paste``
    发 "你好kuuki" 得到 "浣犲ソkuuki")。这件事从外部很难救 —— 临时改控制台代码页
    会污染整个会话, 而且不一定装了 UTF-8 代码页。

    直接调 ``SetClipboardData(CF_UNICODETEXT, ...)`` 写 UTF-16LE 就完全没有这条
    路径: 剪贴板原生就是 Unicode 格式, 粘贴端读到的就是原字。

    ``retries`` 是因为剪贴板是全局独占的: 别的进程 (远程桌面 rdpclip、别的复制
    操作) 可能正开着它, ``OpenClipboard`` 会失败。重试几次通常就让开了。
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # pragma: no cover - Windows 上必有 ctypes
        return False

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except Exception:  # pragma: no cover - 非 Windows 不会走到这里
        return False

    user32.OpenClipboard.argtypes = (wintypes.HWND,)
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
    kernel32.GlobalAlloc.restype = wintypes.HANDLE
    kernel32.GlobalFree.argtypes = (wintypes.HANDLE,)
    kernel32.GlobalFree.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = (wintypes.HANDLE,)
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = (wintypes.HANDLE,)
    kernel32.GlobalUnlock.restype = wintypes.BOOL

    # CF_UNICODETEXT 要求 NUL 结尾的 UTF-16LE; 末尾补一对 0 字节保证有终止符。
    encoded = str(text).encode("utf-16-le") + b"\x00\x00"

    for attempt in range(max(1, int(retries))):
        if not user32.OpenClipboard(None):
            time.sleep(delay)
            continue
        try:
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
            if not handle:
                return False
            locked = kernel32.GlobalLock(handle)
            if not locked:
                kernel32.GlobalFree(handle)
                return False
            try:
                ctypes.memmove(locked, encoded, len(encoded))
            finally:
                kernel32.GlobalUnlock(handle)
            # 顺序不能反: EmptyClipboard 必须在 SetClipboardData 之前,
            # 之后这块内存归系统所有 —— 不要再 GlobalFree, 否则会双重释放。
            if not user32.EmptyClipboard():
                return False
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                return False
            return True
        finally:
            user32.CloseClipboard()
    return False


# ---------------------------------------------------------------- 控制器


class InputController(PynputMouseController):
    """鼠标 + 键盘远程控制。所有坐标都是**屏幕绝对坐标**。"""

    #: 平滑移动的默认步进频率 (Hz)
    MOVE_HZ = 60.0

    def __init__(self, mouse: Optional[MouseController] = None, keyboard: Optional[KeyboardController] = None):
        super().__init__()
        if mouse is not None:
            self.mouse = mouse
        if keyboard is not None:
            self.keyboard = keyboard
        self._key_cache: Dict[str, Optional[str]] = {}
        self._key_cache_lock = threading.Lock()

    # ---------------- X11 键预检 (见 KeyUnsupported 的说明) ----------------
    @staticmethod
    def _to_keycode(resolved) -> KeyCode:
        if isinstance(resolved, KeyCode):
            return resolved
        if isinstance(resolved, Key):
            return resolved.value
        if isinstance(resolved, str) and len(resolved) == 1:
            return KeyCode.from_char(resolved)
        raise ValueError(f"无法解析的键 {resolved!r}")

    def _x11_reason(self, resolved) -> Optional[str]:
        """返回不可用原因; ``None`` 表示可以安全发送。非 Linux 恒为 ``None``。"""
        if not _LINUX:
            return None
        keyboard = self.keyboard
        display = getattr(keyboard, "_display", None)
        if display is None:  # pragma: no cover - 非 X11 后端
            return None
        try:
            keycode = self._to_keycode(resolved)
            keysym = (
                keyboard._resolve_dead(keycode)
                or keyboard._resolve_special(keycode)
                or keyboard._resolve_normal(keycode)
                or keyboard._resolve_borrowed(keycode)
            )
        except Exception as exc:  # noqa: BLE001
            return f"键解析失败: {exc.__class__.__name__}: {exc}"
        if keysym is None:
            return "不在当前 X 键盘映射里 (pynput 需临时改键盘布局, 本机不支持)"
        try:
            if not display.keysym_to_keycode(keysym):
                return "在当前 X 键盘映射里没有对应 keycode"
        except Exception as exc:  # noqa: BLE001
            return f"查询 keycode 失败: {exc.__class__.__name__}: {exc}"
        return None

    def _ensure_key(self, key):
        """解析 + 预检一个键; 不可用时抛 ``KeyUnsupported``。"""
        resolved = resolve_key(key) if isinstance(key, str) else key
        reason = self._x11_reason(resolved)
        if reason:
            raise KeyUnsupported(
                f"键 {key!r} 无法发送: {reason}。中文/emoji 等文本请改用 keyboard.paste"
            )
        return resolved

    def key_supported(self, key) -> dict:
        """查询单个键是否可发送 (不实际按键)。"""
        try:
            resolved = resolve_key(key) if isinstance(key, str) else key
        except ValueError as exc:
            return {"key": key, "supported": False, "reason": str(exc)}
        reason = self._x11_reason(resolved)
        return {"key": key, "supported": reason is None, "reason": reason}

    def check_keys(self, keys: Iterable[str]) -> dict:
        return {"keys": [self.key_supported(k) for k in keys]}

    def _rebuild_keyboard(self) -> None:
        """换一条新的 X 连接, 丢掉可能已被污染的键盘控制器。"""
        try:
            old = self.keyboard
            self.keyboard = KeyboardController()
            self._key_cache.clear()
            display = getattr(old, "_display", None)
            if display is not None:
                try:
                    display.close()
                except Exception:
                    pass
        except Exception:  # pragma: no cover
            pass

    def _dispatch(self, func, *args):
        """执行一次键盘动作; 出 X 错误就重建连接, 避免后续调用被拖死。"""
        try:
            return func(*args)
        except (KeyUnsupported, ValueError):
            raise
        except Exception:
            self._rebuild_keyboard()
            raise

    # ---------------- 鼠标 ----------------
    def position(self) -> Tuple[int, int]:
        x, y = self.mouse.position
        return int(x), int(y)

    def move_absolute(self, x: int, y: int) -> Tuple[int, int]:
        """绝对定位 (pynput 的 ``position`` setter, 不是 ``move()``)。"""
        self.mouse.position = (int(x), int(y))
        return int(x), int(y)

    def move_relative(self, dx: int, dy: int) -> Tuple[int, int]:
        x, y = self.position()
        return self.move_absolute(x + int(dx), y + int(dy))

    def move_smooth(self, x: int, y: int, duration: float = 0.0) -> Tuple[int, int]:
        """带时长的插值移动 (duration<=0 时直接跳到位)。"""
        x, y = int(x), int(y)
        if duration and duration > 0:
            start_x, start_y = self.position()
            steps = max(1, int(duration * self.MOVE_HZ))
            step_sleep = duration / steps
            for i in range(1, steps + 1):
                t = i / steps
                # ease-in-out, 起停更自然
                t = t * t * (3 - 2 * t)
                self.move_absolute(round(start_x + (x - start_x) * t), round(start_y + (y - start_y) * t))
                time.sleep(step_sleep)
        return self.move_absolute(x, y)

    def move_relative_smooth(self, dx: int, dy: int, duration: float = 0.0) -> Tuple[int, int]:
        x, y = self.position()
        return self.move_smooth(x + int(dx), y + int(dy), duration)

    def press_mouse(self, button: str = "left") -> None:
        self.mouse.press(self._button(button))

    def release_mouse(self, button: str = "left") -> None:
        self.mouse.release(self._button(button))

    def click(
        self,
        button: str = "left",
        clicks: int = 1,
        interval: float = 0.05,
        hold: float = 0.06,
        x: Optional[int] = None,
        y: Optional[int] = None,
        move_duration: float = 0.0,
    ) -> int:
        """点击。``hold`` 是按下与抬起之间的间隔, 默认 60ms。

        **不要传 0**: pynput 的 ``Controller.click()`` 内部是瞬时的 down+up,
        某些前端框架拿不到有效的按下-抬起配对, 于是只触发 hover 不触发 click ——
        实测 DSH Web GUI 的发送按钮: 光标位置经读回确认到位、调用返回成功,
        但按钮毫无反应、消息发不出去。这与 ``remote/win/winhost.ps1`` 里
        ``mouse_event`` 瞬时连击是同一个坑, 两处行为保持一致。

        ``x`` / ``y`` 给了就**先移动再点**, 只给一个时另一个保持当前坐标
        (与 :meth:`scroll` 一致)。这一步不能省: 移动是鼠标的全局状态, 不挪过去
        就等于点当前位置 —— 也就是"传了坐标但点了别处"。
        """
        if x is not None or y is not None:
            current_x, current_y = self.position()
            self.move_smooth(
                current_x if x is None else int(x),
                current_y if y is None else int(y),
                move_duration,
            )
        btn = self._button(button)
        total = max(1, int(clicks))
        for i in range(total):
            if i:
                time.sleep(max(0.0, interval))
            self.mouse.press(btn)
            if hold and hold > 0:
                time.sleep(hold)
            self.mouse.release(btn)
        return total

    def scroll(
        self,
        dx: int = 0,
        dy: int = 0,
        steps: int = 1,
        interval: float = 0.05,
        x: Optional[int] = None,
        y: Optional[int] = None,
    ) -> dict:
        """滚轮。``dy>0`` 向上, ``dx>0`` 向右。

        ``steps>1`` 时把 dx/dy 拆成多步、每步之间 sleep ``interval`` 秒: 一次发出
        几十格会被前台应用当成瞬移, 分步更接近真人滚动。**总量守恒**靠"累计目标值
        取整后取差值"保证 —— 若改成每步 ``dx // steps``, 整除余数会被丢掉
        (``3 格 / 5 步`` 每步 0 格, 最后一格都不滚)。

        ``x`` / ``y`` 给了就先把光标移过去再滚 (悬停在目标区域上滚, 比如把指针
        放到代码区再滚)。只给其中一个时另一个保持当前坐标。
        """
        dx, dy = int(dx), int(dy)
        steps = max(1, int(steps))
        interval = max(0.0, float(interval))
        if x is not None or y is not None:
            current_x, current_y = self.position()
            self.move_absolute(
                current_x if x is None else int(x),
                current_y if y is None else int(y),
            )
        if steps == 1:
            self.mouse.scroll(dx, dy)
        else:
            sent_x = sent_y = 0
            for index in range(1, steps + 1):
                want_x = dx * index / steps
                want_y = dy * index / steps
                delta_x = int(round(want_x)) - sent_x
                delta_y = int(round(want_y)) - sent_y
                if delta_x or delta_y:
                    self.mouse.scroll(delta_x, delta_y)
                    sent_x += delta_x
                    sent_y += delta_y
                if index < steps:
                    time.sleep(interval)
        return {"dx": dx, "dy": dy, "steps": steps, "interval": interval}

    @staticmethod
    def _drag_path(
        points: Optional[Sequence[Sequence[int]]],
        x1: Optional[int],
        y1: Optional[int],
        x2: Optional[int],
        y2: Optional[int],
    ) -> List[Tuple[int, int]]:
        """把两种拖拽写法归一到一条路径点列表。

        - ``points=[[x, y], ...]`` (也接受 ``{"x":..,"y":..}``) —— 至少要两个点
        - ``x1, y1, x2, y2`` —— 等价于两个点的路径 (向后兼容老调用)
        """
        if points:
            raw = list(points)
            if len(raw) < 2:
                raise ValueError("points 至少需要两个点 (起点与终点)")
            path: List[Tuple[int, int]] = []
            for item in raw:
                if isinstance(item, dict):
                    px, py = item.get("x"), item.get("y")
                else:
                    try:
                        seq = list(item)
                    except TypeError:
                        raise ValueError(f"每个路径点必须是 [x, y], 收到 {item!r}")
                    if len(seq) != 2:
                        raise ValueError(f"每个路径点必须是 [x, y], 收到 {item!r}")
                    px, py = seq
                if px is None or py is None:
                    raise ValueError(f"路径点缺少坐标: {item!r}")
                path.append((int(px), int(py)))
            return path
        if x1 is None or y1 is None or x2 is None or y2 is None:
            raise ValueError("拖动需要 points=[[x,y],...], 或者完整的 x1/y1/x2/y2")
        return [(int(x1), int(y1)), (int(x2), int(y2))]

    def drag(
        self,
        x1: Optional[int] = None,
        y1: Optional[int] = None,
        x2: Optional[int] = None,
        y2: Optional[int] = None,
        button: str = "left",
        duration: float = 0.3,
        points: Optional[Sequence[Sequence[int]]] = None,
    ) -> dict:
        """按下 -> 平滑拖动 -> 松开。

        两种写法:

        - 两点: ``drag(x1, y1, x2, y2)``
        - 路径点: ``drag(points=[[x, y], [x, y], ...])`` —— 首点按下, 逐段平滑经过,
          末点松开。``duration`` 是**每段**的时长, 所以总时长 = duration × (点数 − 1)。

        整条路径只按一次、只松一次: 分段时若中途松开, 前台应用会把路径拆成多次
        独立拖拽 (画图/选文件区间这类操作就废了)。
        """
        path = self._drag_path(points, x1, y1, x2, y2)
        btn = self._button(button)
        self.move_absolute(*path[0])
        time.sleep(0.05)
        self.mouse.press(btn)
        try:
            for _, end in zip(path, path[1:]):
                self.move_smooth(end[0], end[1], duration)
        finally:
            time.sleep(0.05)
            self.mouse.release(btn)
        return {
            "from": list(path[0]),
            "to": list(path[-1]),
            "button": button,
            "points": [list(point) for point in path],
            "segments": len(path) - 1,
        }

    @staticmethod
    def _button(button: str) -> Button:
        key = (button or "left").lower()
        mapping = {
            "left": Button.left,
            "right": Button.right,
            "middle": Button.middle,
            "1": Button.left,
            "2": Button.middle,
            "3": Button.right,
            "left click": Button.left,
            "right click": Button.right,
            "middle click": Button.middle,
        }
        if key not in mapping:
            raise ValueError(f"不支持的鼠标键 {button!r}, 可选: left / middle / right")
        return mapping[key]

    # ---------------- 键盘 ----------------
    def type_text(self, text: str, interval: float = 0.0) -> int:
        """输入文本。``interval>0`` 时逐字符 sleep (更接近真人)。

        先整串预检: 只要有一个字符在当前 X 键盘映射里打不出来 (中文/emoji 最常见),
        就整体拒绝并提示改用 ``paste_text()``——否则 pynput 会在第一个坏字符上
        把 X 连接挂死 (见 ``KeyUnsupported``)。
        """
        text = "" if text is None else str(text)
        if _LINUX and text:
            unsupported: List[str] = []
            for char in dict.fromkeys(text):
                if char in ("\n", "\t"):  # pynput 会翻译成 enter / tab
                    continue
                if len(char) != 1:  # pragma: no cover - 代理对等
                    unsupported.append(char)
                    continue
                if self._x11_reason(KeyCode.from_char(char)):
                    unsupported.append(char)
            if unsupported:
                raise KeyUnsupported(
                    "这些字符在当前 X 键盘映射里打不出来: "
                    + repr("".join(unsupported))
                    + "; 请改用 keyboard.paste (写剪贴板 + Ctrl+V)"
                )
        if interval and interval > 0:
            for char in text:
                self._dispatch(self.keyboard.type, char)
                time.sleep(interval)
        else:
            self._dispatch(self.keyboard.type, text)
        return len(text)

    def press_key(self, key: Union[str, Key]) -> None:
        self._dispatch(self.keyboard.press, self._ensure_key(key))

    def release_key(self, key: Union[str, Key]) -> None:
        self._dispatch(self.keyboard.release, self._ensure_key(key))

    def tap_key(self, key: Union[str, Key]) -> None:  # type: ignore[override]
        self._dispatch(self.keyboard.tap, self._ensure_key(key))

    def key_action(self, key: str, action: str = "tap", modifiers: Sequence[str] = ()) -> dict:
        """``action``: tap / press / release; ``modifiers`` 只在 tap 时按下并松开。"""
        action = (action or "tap").lower()
        if action not in ("tap", "press", "release"):
            raise ValueError(f"未知 action {action!r}, 可选: tap / press / release")
        mods = [self._ensure_key(m) for m in (modifiers or [])]
        resolved = self._ensure_key(key)

        def run():
            for mod in mods:
                self.keyboard.press(mod)
            try:
                if action == "press":
                    self.keyboard.press(resolved)
                elif action == "release":
                    self.keyboard.release(resolved)
                else:
                    self.keyboard.tap(resolved)
            finally:
                for mod in reversed(mods):
                    self.keyboard.release(mod)

        self._dispatch(run)
        return {"key": key, "action": action, "modifiers": list(modifiers or [])}

    def hotkey(self, keys: Iterable[str], hold_ms: float = 0.0) -> dict:
        """组合键: ``hotkey(["ctrl", "shift", "s"])`` 或 ``hotkey("ctrl+shift+s")``。

        ``hold_ms>0`` 时在**全部按下之后**保持这么久再逆序松开。有些软件只在按键
        处于"持续按下"状态时才响应 (菜单快捷键、游戏里的组合键), 瞬时 down+up 会被
        忽略 —— 和 ``click()`` 的 ``hold`` 是同一类问题, 默认 0 保持原行为不变。
        """
        if isinstance(keys, str):
            parts = [p for p in keys.replace(" ", "").split("+") if p]
        else:
            parts = [str(k) for k in keys]
        if not parts:
            raise ValueError("组合键不能为空")
        hold_ms = max(0.0, float(hold_ms or 0.0))
        resolved = [self._ensure_key(p) for p in parts]

        def run():
            for key in resolved:
                self.keyboard.press(key)
            if hold_ms:
                time.sleep(hold_ms / 1000.0)
            for key in reversed(resolved):
                self.keyboard.release(key)

        self._dispatch(run)
        return {"keys": parts, "hold_ms": hold_ms}

    def hold_key(self, key: Union[str, Key], ms: float) -> dict:
        """按住 ``key`` 保持 ``ms`` 毫秒再松开。

        给"需要长按"的场景用: F2 重命名 (按早了 / 按短了会触发别的行为)、按住
        方向键连续滚动、某些游戏的长按蓄力。``ms`` 必须 > 0, 只是想敲一下请用
        ``tap_key`` / ``keyboard.key`` (action=tap)。
        """
        ms = float(ms or 0.0)
        if ms <= 0:
            raise ValueError("hold 需要 ms > 0 (只想敲一下请用 keyboard.key 的 tap)")
        resolved = self._ensure_key(key)

        def run():
            self.keyboard.press(resolved)
            time.sleep(ms / 1000.0)
            self.keyboard.release(resolved)

        self._dispatch(run)
        return {"key": key, "ms": ms}

    # ---------------- 剪贴板粘贴 ----------------
    def paste_text(self, text: str) -> dict:
        """把文本写进剪贴板并发粘贴快捷键 (中文/emoji 的可靠输入方式)。"""
        text = "" if text is None else str(text)
        name = "win32"
        if not (sys.platform == "win32" and _set_clipboard_windows(text)):
            tool = clipboard_tool()
            if tool is None:
                raise RuntimeError(
                    "写不了剪贴板 (既没有 Win32 API, 也没有 wl-copy / xclip / xsel / pbcopy / clip)"
                )
            name, cmd = tool
            proc = subprocess.run(cmd, input=text.encode("utf-8"), capture_output=True)
            if proc.returncode != 0:
                raise RuntimeError(
                    f"{name} 写入剪贴板失败: {proc.stderr.decode('utf-8', 'replace')[:200]}"
                )
        time.sleep(0.05)
        # 不再维护: macOS 分支。受控端只支持 Windows, 这里恒定走 ctrl+v。
        paste_key = "cmd+v" if sys.platform == "darwin" else "ctrl+v"
        self.hotkey(paste_key)
        return {"chars": len(text), "clipboard_tool": name, "hotkey": paste_key}
