"""中文粘贴 MOT (留证脚本): 证明 keyboard.paste 不会产成 GBK 乱码。

为什么专门留这条证据
--------------------
修复前 paste_text() 走 ``clip.exe``, 中文 Windows 的控制台代码页是 GBK(936),
UTF-8 字节会被当成 GBK: "你好" 粘出来是 "浣犲ソ"。它还是**静默出错**的 ——
op 返回 ``chars: N`` 看着成功, 错的内容却已经进了目标输入框。所以不能只看返回值,
必须截图看字。

用法 (受控端已在 ws://127.0.0.1:8765 跑着)::

    .venv-win/Scripts/python.exe -u evidence/cjk_paste_check.py

产物:
    evidence/cjk-paste-evidence.png   屏幕截图 (记事本里的实际文字)
    evidence/op-log.json              每一步的 op / 参数 / 返回

安全措施:
    1. 用**进程方式**开记事本 (Win+R 那套 GUI 路线实测 Enter 不生效);
    2. 每次按键前用 Win32 ``GetForegroundWindow`` 核对前台窗口标题,
       不是沙箱就立刻中止 —— 绝不把按键发到用户的 Chrome / IDE 里;
    3. 收尾全选删空再关, 不弹保存对话框。
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from remote.client import WsClient  # noqa: E402

URL = os.environ.get("KUUKI_WS", "ws://127.0.0.1:8765")
SAMPLE = "中文粘贴 🎉 kuuki Test"
LOG: list[dict] = []


def log(kind: str, payload) -> None:
    LOG.append(payload)
    text = json.dumps(payload, ensure_ascii=False)
    print(f"  [{kind}] {text[:150]}")


def foreground_title() -> str:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    hwnd = user32.GetForegroundWindow()
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def assert_sandbox(title: str) -> None:
    if "记事本" not in title and "Notepad" not in title:
        raise SystemExit(
            f"!! 前台窗口是 {title!r}, 不是记事本 —— 已中止, 未发送任何按键。"
            f"（这一步是安全网: 宁可停, 也不能把按键打进用户的窗口）"
        )


async def main() -> int:
    subprocess.Popen(["notepad.exe"])
    time.sleep(1.2)
    title = foreground_title()
    log("前台窗口", title)
    assert_sandbox(title)

    async with WsClient(URL, timeout=30) as client:
        # 1) 新修的 mouse.click(x, y): 以前会把 x/y 吃掉, 只点当前光标位置
        res = await client.call("mouse.click", {"x": 840, "y": 420})
        log("mouse.click(x,y)", res)
        await asyncio.sleep(0.3)
        assert_sandbox(foreground_title())

        # 2) 老: type 只能处理 ASCII, 这里故意混排, 中文字走 paste
        res = await client.call("keyboard.type", {"text": "ascii-part ", "interval": 0.004})
        log("keyboard.type", res)

        # 3) 关键一步
        res = await client.call("keyboard.paste", {"text": SAMPLE})
        log("keyboard.paste", res)
        await asyncio.sleep(0.5)

        # 4) 截图留证
        header, payload = await client.screenshot({"max_width": 1280, "format": "png"})
        png = os.path.join(_HERE, "cjk-paste-evidence.png")
        with open(png, "wb") as handle:
            handle.write(payload)
        log("screenshot", {"width": header.get("width"), "height": header.get("height"),
                           "bytes": len(payload), "path": png,
                           "backend": header.get("backend")})
        print(f"\n  证据截图: {png}")
        print(f"  预期在记事本里看到: ascii-part {SAMPLE}")
        print("  （修复前这一步会显示成 GBK 乱码, 例如 '涓枃绮樿屯'）")

        # 5) 收尾: 焦点确认还在沙箱才敢发这些键
        assert_sandbox(foreground_title())
        await client.call("keyboard.hotkey", {"keys": "ctrl+a"})
        await client.call("keyboard.key", {"key": "delete"})
        await asyncio.sleep(0.2)
        await client.call("keyboard.hotkey", {"keys": "alt+f4"})
        await asyncio.sleep(0.5)
        log("收尾", {"前台现在是": foreground_title()})

    with open(os.path.join(_HERE, "op-log.json"), "w", encoding="utf-8") as handle:
        json.dump(LOG, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
