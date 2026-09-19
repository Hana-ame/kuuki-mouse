"""被控端的角标通知 —— 自己画一个无焦点小窗口, 贴在屏幕角落。

为什么不用 Windows 系统弹窗
--------------------------
``WScript.Shell.Popup`` / MessageBox 会**抢走前台焦点**, 于是:
  * 控制端正要往输入框打字, 焦点却被弹窗拿走 → 字符落到别处
  * 需要用户点掉才能继续 → 无人值守时把流程挂住

所以这里用一个**自绘的无边框置顶窗口**:
  * ``overrideredirect(True)`` —— 没有标题栏、不进任务栏
  * **不调用** ``focus_force()`` / ``grab_set()`` —— 不抢焦点
  * ``-topmost`` —— 浮在其它窗口之上
  * ``-alpha`` —— 半透明, 不挡视线
  * 到时间自动销毁

位置由 ``corner`` 决定: ``br`` 右下(默认) / ``tr`` 右上 / ``tl`` 左上 / ``bl`` 左下。
多个通知会**纵向堆叠**, 不会互相覆盖。

线程模型: tkinter 必须在自己的线程里跑 (主线程被 asyncio 占着),
所以每次通知起一个短命线程, 窗口显示完自动退出。
"""

from __future__ import annotations

import logging
import threading
from typing import List

log = logging.getLogger("kuuki.remote.toast")

__all__ = ["notify", "notify_supported", "active_count"]

#: 当前存活的角标数 (用于堆叠定位)
_lock = threading.Lock()
_active: List["_Toast"] = []


def notify_supported() -> bool:
    """是否支持角标通知 (需要 tkinter + 图形环境)。"""
    try:
        # 探测用: 只需知道 tkinter 在不在, 不实际使用它
        __import__("tkinter")
    except Exception:
        return False
    return True


def active_count() -> int:
    with _lock:
        return len(_active)


class _Toast:
    """一个角标窗口。"""

    def __init__(
        self,
        message: str,
        detail: str = "",
        seconds: float = 6.0,
        corner: str = "br",
        width: int = 420,
    ):
        self.message = message
        self.detail = detail
        self.seconds = seconds
        self.corner = corner
        self.width = width
        self.root = None
        self._slot = 0

    def _geometry(self, screen_w: int, screen_h: int) -> str:
        pad = 16
        # 估算高度: 标题一行 + 明细若干行
        lines = 1 + (self.detail.count("\n") + 1 if self.detail else 0)
        height = 56 + lines * 20 + (30 if self.detail else 0)

        if self.corner in ("br", "bl"):
            y = screen_h - pad - height - self._slot * (height + 10)
        else:
            y = pad + self._slot * (height + 10)

        if self.corner in ("br", "tr"):
            x = screen_w - pad - self.width
        else:
            x = pad
        return f"{self.width}x{height}+{x}+{y}"

    def show(self) -> None:
        import tkinter as tk

        with _lock:
            self._slot = len(_active)
            _active.append(self)

        root = tk.Tk()
        self.root = root
        root.overrideredirect(True)          # 无边框、不进任务栏
        root.attributes("-topmost", True)    # 置顶
        try:
            root.attributes("-alpha", 0.94)  # 半透明
        except Exception:
            pass
        root.configure(bg="#1b1b1f")

        geo = self._geometry(root.winfo_screenwidth(), root.winfo_screenheight())
        root.geometry(geo)

        frame = tk.Frame(root, bg="#1b1b1f", highlightthickness=1,
                         highlightbackground="#3a6df0")
        frame.pack(fill="both", expand=True)

        tk.Label(
            frame, text=self.message, bg="#1b1b1f", fg="#eaeaea",
            font=("Microsoft YaHei UI", 11, "bold"), anchor="w", justify="left",
            wraplength=self.width - 24,
        ).pack(fill="x", padx=12, pady=(10, 2))

        if self.detail:
            tk.Label(
                frame, text=self.detail, bg="#1b1b1f", fg="#9aa0a6",
                font=("Microsoft YaHei UI", 9), anchor="w", justify="left",
                wraplength=self.width - 24,
            ).pack(fill="x", padx=12, pady=(0, 10))
        else:
            tk.Frame(frame, bg="#1b1b1f", height=10).pack()

        # 关键: 不调 focus_force / grab_set, 让焦点留在原处
        root.after(int(max(0.5, self.seconds) * 1000), self._close)
        try:
            root.mainloop()
        except Exception as exc:  # noqa: BLE001
            log.debug("角标主循环异常: %s", exc)
        finally:
            with _lock:
                if self in _active:
                    _active.remove(self)

    def _close(self) -> None:
        try:
            if self.root is not None:
                self.root.destroy()
        except Exception:
            pass


def notify(
    message: str,
    detail: str = "",
    seconds: float = 6.0,
    corner: str = "br",
    width: int = 420,
) -> bool:
    """在屏幕角落弹一个无焦点角标; 立即返回, 不阻塞调用方。

    ``corner``: ``br`` 右下 / ``tr`` 右上 / ``tl`` 左上 / ``bl`` 左下。
    """
    if not notify_supported():
        return False
    toast = _Toast(message, detail, seconds, corner, width)
    thread = threading.Thread(target=toast.show, name="kuuki-toast", daemon=True)
    thread.start()
    log.info("角标通知已弹出 [%s]: %s", corner, message)
    return True
