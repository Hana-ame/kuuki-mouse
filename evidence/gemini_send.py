"""端到端验收: 用 repo 的 client 在 Gemini 里发一条消息并留下截图证据。

两个阶段, 中间插入人眼复核, 避免盲操作:

    .venv-win/Scripts/python.exe -u evidence/gemini_send.py locate
    .venv-win/Scripts/python.exe -u evidence/gemini_send.py send

为什么分阶段
------------
窗口激活用本地 Win32 API (``SetForegroundWindow``), 属于"把目标窗口摆到面前"的
环境准备; 之后的点击/粘贴/回车**全部走 remote 的 op**, 才算验收了传输本身。
坐标必须先截一张图看过再点 —— 盲点输入框的下场就是上一轮的 Win+R 事故。

MESSAGE 里刻意混了中文 / emoji / 空格: 一次性验证 keyboard.paste 的 UTF-16le
剪贴板路径 (修复前经 clip.exe 会变 GBK 乱码)。
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
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
MESSAGE = "第三次端到端验收: 中文、空格、emoji 🎉 都打到输入框了吗? 请简短回复。"
#: 截图里输入框中心的坐标 (截图按 max_width 缩过, 要乘 SCALE 换回屏幕坐标)
SCREEN_W, SHOT_W = 1680, 1000
SCALE = SCREEN_W / SHOT_W

INPUT_SHOT = (520, 900)  # 在 max_width=1000 的截图里
INPUT_REAL = (int(INPUT_SHOT[0] * SCALE), int(INPUT_SHOT[1] * SCALE))

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_EnumWindowsProc = ctypes.WINFUNCTYPE(
    ctypes.c_bool, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)
)


def foreground_title() -> str:
    hwnd = _user32.GetForegroundWindow()
    n = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    _user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def find_gemini() -> int:
    hits: list[tuple[int, str]] = []

    def cb(hwnd, _lparam):
        if _user32.IsWindowVisible(hwnd):
            n = _user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                _user32.GetWindowTextW(hwnd, buf, n + 1)
                if "Google Gemini" in buf.value:
                    hits.append((int(hwnd.contents.value), buf.value))
        return True

    _user32.EnumWindows(_EnumWindowsProc(cb), 0)
    if not hits:
        raise SystemExit("没有开着 Gemini 的浏览器窗口")
    return hits[0][0]


def bring_to_front() -> str:
    hwnd = find_gemini()
    _user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    time.sleep(0.3)
    _user32.SetForegroundWindow(hwnd)
    time.sleep(0.6)
    return foreground_title()


async def shoot(client: WsClient, path: str, max_width: int = 1000) -> dict:
    header, payload = await client.screenshot({"max_width": max_width, "format": "png"})
    with open(os.path.join(_HERE, path), "wb") as handle:
        handle.write(payload)
    print(f"  截图 -> {os.path.join(_HERE, path)} ({header.get('width')}x{header.get('height')})")
    return header


async def locate() -> int:
    title = bring_to_front()
    print(f"前台窗口: {title!r}")
    if "Gemini" not in title:
        print("!! 前台不是 Gemini, 中止")
        return 2
    async with WsClient(URL, timeout=25) as client:
        await shoot(client, "gemini-1-locate.png")
        pos = await client.call("mouse.position")
        print(f"当前光标: {pos}")
        print(f"计划点击输入框: {INPUT_REAL} (截图坐标 {INPUT_SHOT} x {SCALE})")
        print("确认输入框位置无误后, 再跑 send 阶段。")
    return 0


async def send() -> int:
    title = bring_to_front()
    print(f"前台窗口: {title!r}")
    if "Gemini" not in title:
        print("!! 前台不是 Gemini, 中止")
        return 2

    async with WsClient(URL, timeout=40) as client:
        # 1) 点输入框 —— 用修好的 mouse.click(x, y)
        res = await client.call("mouse.click", {"x": INPUT_REAL[0], "y": INPUT_REAL[1]})
        print(f"  mouse.click: {res}")
        await asyncio.sleep(0.4)

        # 2) 清空可能存在的旧内容 (ctrl+a -> delete)
        await client.call("keyboard.hotkey", {"keys": "ctrl+a"})
        await client.call("keyboard.key", {"key": "delete"})
        await asyncio.sleep(0.2)

        # 3) 粘贴中文消息 (走键盘路径才有意义 —— 验收 paste 的 UTF-16le 剪贴板)
        res = await client.call("keyboard.paste", {"text": MESSAGE})
        print(f"  keyboard.paste: {res}")
        await asyncio.sleep(0.6)

        # 4) 发之前先看一眼框里有啥 —— 这就是上一次翻车后再也没跳过的步骤
        await shoot(client, "gemini-2-typed.png")
        tip = input("  框里的字对吗? 回车继续发送, 输入 n 取消: ").strip().lower()
        if tip.startswith("n"):
            print("  已取消, 未发送。")
            return 1

        # 5) 发送
        await client.call("keyboard.key", {"key": "enter"})
        print("  已回车发送, 等 Gemini 回复…")

        # 6) 轮询截图直到页面不再显示"生成中"
        for index in range(1, 13):
            await asyncio.sleep(5)
            header, payload = await client.screenshot({"max_width": 1000, "format": "png"})
            path = f"gemini-3-reply-{index:02d}.png"
            with open(os.path.join(_HERE, path), "wb") as handle:
                handle.write(payload)
            print(f"  {index * 5:>3}s -> {path}")
        print(f"  共抓了 12 张, 最后一张 gemini-3-reply-12.png")
    return 0


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "locate"
    raise SystemExit(asyncio.run(send() if stage == "send" else locate()))
