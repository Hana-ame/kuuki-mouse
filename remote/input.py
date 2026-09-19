"""输入控制: 在 ``controller.PynputMouseController`` 之上补全远程控制需要的动作。

为什么是**扩展**而不是重写
--------------------------
``controller.py`` 已经把"鼠标/键盘各自一个 pynput 控制器"这个坑处理掉了
(双继承时 ``KeyboardController.tap`` 会解析到鼠标版 ``press``)。这里直接继承它,
只补远程控制需要的东西: 绝对/相对移动、按住/松开、拖拽、带时长的平滑移动、
更全的特殊键映射、组合键、剪贴板粘贴。

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
    raw = str(name).strip()
    if not raw:
        raise ValueError("键名不能为空")
    key = raw.lower()
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
    """探测可用的剪贴板写入工具, 返回 (名字, 命令前缀) 或 None。"""
    for name, cmd, _kind in _CLIPBOARD_CANDIDATES:
        if shutil.which(name):
            return name, list(cmd)
    return None


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

    def click(self, button: str = "left", clicks: int = 1, interval: float = 0.05) -> int:
        btn = self._button(button)
        for i in range(max(1, int(clicks))):
            if i:
                time.sleep(max(0.0, interval))
            self.mouse.click(btn)
        return max(1, int(clicks))

    def scroll(self, dx: int = 0, dy: int = 0) -> None:
        """滚轮。``dy>0`` 向上, ``dx>0`` 向右。"""
        self.mouse.scroll(int(dx), int(dy))

    def drag(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        button: str = "left",
        duration: float = 0.3,
    ) -> dict:
        """按下 -> 平滑拖动 -> 松开。"""
        btn = self._button(button)
        self.move_absolute(x1, y1)
        time.sleep(0.05)
        self.mouse.press(btn)
        try:
            self.move_smooth(x2, y2, duration)
        finally:
            time.sleep(0.05)
            self.mouse.release(btn)
        return {"from": [int(x1), int(y1)], "to": [int(x2), int(y2)], "button": button}

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

    def hotkey(self, keys: Iterable[str]) -> dict:
        """组合键: ``hotkey(["ctrl", "shift", "s"])`` 或 ``hotkey("ctrl+shift+s")``。"""
        if isinstance(keys, str):
            parts = [p for p in keys.replace(" ", "").split("+") if p]
        else:
            parts = [str(k) for k in keys]
        if not parts:
            raise ValueError("组合键不能为空")
        resolved = [self._ensure_key(p) for p in parts]

        def run():
            for key in resolved:
                self.keyboard.press(key)
            for key in reversed(resolved):
                self.keyboard.release(key)

        self._dispatch(run)
        return {"keys": parts}

    # ---------------- 剪贴板粘贴 ----------------
    def paste_text(self, text: str) -> dict:
        """把文本写进剪贴板并发粘贴快捷键 (中文/emoji 的可靠输入方式)。"""
        tool = clipboard_tool()
        if tool is None:
            raise RuntimeError(
                "没有可用的剪贴板工具 (需要 wl-copy / xclip / xsel / pbcopy / clip 之一)"
            )
        name, cmd = tool
        proc = subprocess.run(cmd, input=str(text).encode("utf-8"), capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"{name} 写入剪贴板失败: {proc.stderr.decode('utf-8', 'replace')[:200]}"
            )
        time.sleep(0.05)
        paste_key = "cmd+v" if sys.platform == "darwin" else "ctrl+v"
        self.hotkey(paste_key)
        return {"chars": len(str(text)), "clipboard_tool": name, "hotkey": paste_key}
