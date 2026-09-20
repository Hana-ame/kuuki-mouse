"""端到端验收: 纯 repo 能力"认窗口 -> 切前台 -> 在窗口里发消息"。

以前这一步会翻车: 只有视觉定位时, 脚本把 WorkBuddy 的会话标签栏当成浏览器标签栏,
消息发进了错误的窗口 (见 docs/knowledge/gui-window-focus-gap.md)。补上
window.list / window.foreground / window.focus 之后, 窗口身份由**标题与进程名**
确定, 不再靠猜 —— 本脚本全程只用 repo 的 op, 不借外部工具。

跑法 (受控端已在 127.0.0.1:8765):
    python evidence/verify_window_fix.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from remote import vision  # noqa: E402
from remote.client import WsClient  # noqa: E402

URL = "ws://127.0.0.1:8765/"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "window-fix")
MESSAGE = (
    "kuuki-mouse 验收：这条消息由受控端的 window.focus 认窗口 + keyboard.paste 输入，"
    "全程没有人工给坐标。"
)


def step(text: str) -> None:
    print(f"\n=== {text}", flush=True)


async def shot(client, name: str) -> bytes:
    header, payload = await client.screenshot({"format": "png"})
    path = os.path.join(OUT, f"{name}.png")
    with open(path, "wb") as handle:
        handle.write(payload)
    print(f"    帧 {name}: {header.get('width')}x{header.get('height')}"
          f" (原屏 {header.get('source_width')}x{header.get('source_height')}) -> {path}")
    return payload


async def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    async with WsClient(URL, timeout=30) as client:
        step("① 先把前台切到 VS Code —— 证明 focus 真的会换窗口")
        moved = await client.call("window.focus", {"process": "Code"})
        print(f"    focus -> focused={moved['focused']} process={moved['process']}"
              f" method={moved['method']} hwnd={moved['hwnd']}")
        foreground = await client.call("window.foreground", {})
        print(f"    foreground -> {foreground['process']} | {foreground['title'][:50]}")
        assert foreground["process"] == "Code", "focus 没把 VS Code 切到前台"

        step("② 再切回 Edge —— 之后的输入都落在这里")
        moved = await client.call("window.focus", {"process": "msedge"})
        print(f"    focus -> focused={moved['focused']} process={moved['process']}"
              f" method={moved['method']}")
        foreground = await client.call("window.foreground", {})
        print(f"    foreground -> {foreground['process']} | {foreground['title'][:50]}")
        assert foreground["process"] == "msedge", "focus 没把 Edge 切到前台"

        step("③ 新标签页打开 Gemini (ctrl+t -> 输入网址 -> enter), 必须确认真的到了")
        await client.call("keyboard.hotkey", {"keys": "ctrl+t"})
        time.sleep(1.2)
        await client.call("keyboard.type", {"text": "gemini.google.com"})
        time.sleep(0.6)
        await client.call("keyboard.key", {"key": "enter"})

        async def gemini_arrived() -> bool:
            named = await client.call("window.list", {"title": "gemini"})
            return bool(named["windows"])

        # 上一次跑: Enter 被吞掉, 地址栏还停在文字上, 之后的粘贴全进了搜索框。
        # 所以这里必须轮询确认 "标题里有 Gemini" 才继续, 等不到就补一次回车。
        arrived = False
        for attempt in range(3):
            deadline = time.time() + 10.0
            while time.time() < deadline:
                if await gemini_arrived():
                    arrived = True
                    break
                await asyncio.sleep(1.0)
            if arrived:
                break
            print(f"    第 {attempt + 1} 次还没看到 Gemini 标题, 补一次回车")
            await client.call("keyboard.key", {"key": "enter"})
        if not arrived:
            print("    !! 打不开 Gemini (没登录/没网), 到此为止, 不粘贴任何东西")
            return 1
        named = await client.call("window.list", {"title": "gemini"})
        print(f"    已到达: {[(w['process'], w['title'][:40]) for w in named['windows']]}")
        foreground = await client.call("window.foreground", {})
        print(f"    foreground -> {foreground['process']} | {foreground['title'][:60]}")
        assert foreground["process"] == "msedge", "打开网页后前台跑掉了"
        time.sleep(3.0)  # 给页面把输入框渲染出来的时间

        step("④ 粘贴前先存一帧, 点进 Gemini 的提问框 (窗口身份已被 window.focus 确认)")
        before = await shot(client, "1-before-paste")
        foreground = await client.call("window.foreground", {})
        rect = foreground["rect"]
        # Gemini 的提问框在窗口中部偏下 (约 57% 宽 / 56% 高)。窗口是哪一个已经
        # 由 window.focus + window.foreground 确认, 这里只解决"框在窗口哪里";
        # 点没点中由第⑥步的帧差否证, 不靠假设。
        box_x = int(rect["left"] + rect["width"] * 0.574)
        box_y = int(rect["top"] + rect["height"] * 0.563)
        clicked = await client.call("mouse.click", {"x": box_x, "y": box_y, "interval": 0})
        print(f"    click -> {clicked}")
        time.sleep(1.0)

        step("⑤ keyboard.paste 写入消息 (不回车, 先看有没有落进去)")
        pasted = await client.call("keyboard.paste", {"text": MESSAGE})
        print(f"    paste -> {pasted}")
        time.sleep(1.5)
        after = await shot(client, "2-after-paste")

        step("⑥ 用 repo 自己的 vision.diff 否证: 提问框附近必须真的变了")
        changed = vision.diff(vision.load(before), vision.load(after), tol=24, min_pixels=200)
        print(f"    变化区域 {len(changed)} 处")
        landed = any(
            abs(r.to_dict(1.0)["center"]["x"] - box_x) < 220
            and abs(r.to_dict(1.0)["center"]["y"] - box_y) < 120
            for r in changed
        )
        if not landed:
            print(f"    !! 提问框 (≈{box_x},{box_y}) 附近没有变化 —— 焦点不在那里, 到此为止")
            for r in changed[:8]:
                print(f"       变化点: {r.to_dict(1.0)['center']}")
            return 1
        print(f"    提问框附近确有变化 -> 粘贴落进去了")

        step("⑦ 回车发送")
        await client.call("keyboard.key", {"key": "enter"})
        time.sleep(10.0)
        await shot(client, "3-after-send")

        after_send = await client.call("window.foreground", {})
        print(f"    foreground -> {after_send['process']} | {after_send['title'][:60]}")
    step("完成: 全部步骤只用 repo 的 op (window.* / keyboard.* / screen.screenshot)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
