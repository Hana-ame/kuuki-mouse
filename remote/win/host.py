"""Windows 桥的 Python 客户端: 常驻 PowerShell 子进程 + 行式 JSON。

为什么要有它
------------
WSL 里的 pynput 只能碰 WSLg 的 X 屏幕 —— 实测那个屏幕上**没有窗口管理器**,
X 程序都连得上却一个都不 map, 截屏恒定 5.5KB 全黑。所以要让 agent 真正
"看到屏幕 / 操作这台电脑", 必须把输入与截屏交给 Windows 侧。

`winhost.ps1` 是常驻的 PowerShell 进程 (省掉每次 300-800ms 的启动 + Add-Type 编译),
本模块负责拉起它、按行收发 JSON、并把 Windows 的截图字节交给 ScreenCapture 处理。
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

__all__ = ["WindowsHost", "find_powershell", "is_windows_bridge_available"]

#: 常见 Windows 互操作路径 (WSL 下 /mnt/c 挂载)
_POWERSHELL_CANDIDATES = (
    "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
    "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/pwsh.exe",
    "powershell.exe",
    "pwsh.exe",
)


def find_powershell() -> Optional[str]:
    """找到可用的 Windows PowerShell 可执行文件 (WSL 互操作)。"""
    if sys.platform.startswith("win"):
        found = shutil.which("powershell") or shutil.which("pwsh")
        if found:
            return found
    for candidate in _POWERSHELL_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return shutil.which("powershell.exe") or shutil.which("pwsh.exe")


def _to_windows_path(path: str) -> str:
    """把 /mnt/c/... 或 /home/... 转成 Windows 能认的路径。"""
    if shutil.which("wslpath"):
        try:
            out = subprocess.run(["wslpath", "-w", path], capture_output=True, timeout=10)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.decode().strip()
        except Exception:
            pass
    if path.startswith("/mnt/") and len(path) > 6:
        drive = path[5].upper()
        return f"{drive}:\\" + path[7:].replace("/", "\\")
    return path


def is_windows_bridge_available() -> bool:
    return find_powershell() is not None


class WindowsHost:
    """常驻 PowerShell 桥: 行式 JSON 请求/响应。"""

    def __init__(self, script: Optional[str] = None, timeout: float = 60.0):
        self.script = script or os.path.join(os.path.dirname(os.path.abspath(__file__)), "winhost.ps1")
        self.timeout = timeout
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._next_id = 0
        self._stderr_lines: List[str] = []
        self.start()

    # ---------------- 生命周期 ----------------
    def start(self) -> None:
        powershell = find_powershell()
        if powershell is None:
            raise RuntimeError("找不到 Windows PowerShell (WSL 互操作不可用)")
        if not os.path.isfile(self.script):
            raise RuntimeError(f"winhost.ps1 不存在: {self.script}")
        self._proc = subprocess.Popen(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                _to_windows_path(self.script),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        # 预热: 等 P/Invoke 编译完成 (Add-Type 首次要几百 ms)
        deadline = time.time() + self.timeout
        last: Optional[Exception] = None
        while time.time() < deadline:
            try:
                self.call("ping", {}, timeout=min(10.0, self.timeout))
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
                if self._proc.poll() is not None:
                    raise RuntimeError(f"winhost.ps1 启动即退出: {self.stderr_tail()}")
                time.sleep(0.3)
        raise RuntimeError(f"winhost.ps1 预热超时: {last}; stderr={self.stderr_tail()}")

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            self._stderr_lines.append(line.rstrip())
            del self._stderr_lines[:-50]

    def stderr_tail(self, n: int = 5) -> str:
        return " | ".join(self._stderr_lines[-n:])

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    # ---------------- 调用 ----------------
    def call(self, op: str, args: Optional[dict] = None, timeout: Optional[float] = None) -> Any:
        with self._lock:
            if not self.alive:
                raise RuntimeError(f"Windows 桥已退出: {self.stderr_tail()}")
            self._next_id += 1
            req_id = self._next_id
            payload = {"id": req_id, "op": op, **(args or {})}
            assert self._proc and self._proc.stdin and self._proc.stdout
            self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()

            deadline = time.time() + (timeout or self.timeout)
            while time.time() < deadline:
                line = self._proc.stdout.readline()
                if not line:
                    raise RuntimeError(f"Windows 桥无响应/已关闭: {self.stderr_tail()}")
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue  # PowerShell 偶尔会混入非 JSON 行
                if msg.get("id") != req_id:
                    continue
                if not msg.get("ok"):
                    error = msg.get("error") or {}
                    raise RuntimeError(f"{error.get('code')}: {error.get('message')}")
                return msg.get("result")
            raise TimeoutError(f"Windows 桥 {op} 超时 ({timeout or self.timeout}s)")

    # ---------------- 便捷封装 ----------------
    def ping(self) -> dict:
        return self.call("ping")

    def info(self) -> dict:
        return self.call("info")

    def screenshot_png(self, region: Optional[dict] = None) -> bytes:
        args: Dict[str, Any] = {}
        if region:
            args = {
                "left": int(region.get("left", 0)),
                "top": int(region.get("top", 0)),
                "width": int(region.get("width", 0)),
                "height": int(region.get("height", 0)),
            }
        result = self.call("screenshot", args)
        return base64.b64decode(result["b64"])

    def grab_image(self):
        """给 ScreenCapture 用: 直接返回 PIL.Image。"""
        from PIL import Image

        image = Image.open(io.BytesIO(self.screenshot_png()))
        image.load()
        return image.convert("RGB")

    def position(self) -> dict:
        return self.call("position")

    def move(self, x: int, y: int) -> dict:
        return self.call("move", {"x": int(x), "y": int(y)})

    def click(self, button: str = "left", clicks: int = 1, hold_ms: int = 60) -> dict:
        """点击。``hold_ms`` 是按下与抬起之间的间隔, 默认 60ms。

        **不要传 0**: 瞬时 down+up 会被前端框架当成无效点击 —— 坐标正确、调用成功,
        但按钮只触发 hover 不触发 click (实测 DSH 发送按钮)。
        """
        return self.call(
            "click", {"button": button, "clicks": int(clicks), "hold": int(hold_ms)}
        )

    def scroll(self, dx: int = 0, dy: int = 0) -> dict:
        return self.call("scroll", {"dx": int(dx), "dy": int(dy)})

    def type_text(self, text: str) -> dict:
        # base64(UTF-8) 传输: 避免 PowerShell 5.1 按本地代码页解 stdin 把中文弄乱
        return self.call(
            "type", {"b64": base64.b64encode(text.encode("utf-8")).decode("ascii")}
        )

    def key(self, key: str, action: str = "tap") -> dict:
        return self.call("key", {"key": key, "action": action})

    def hotkey(self, keys) -> dict:
        return self.call("hotkey", {"keys": list(keys)})
