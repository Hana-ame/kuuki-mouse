"""只用 repo 的能力, 在 Gemini 里发一条消息。

这条脚本存在的理由: 上一轮验收里, "屏幕上哪个坐标是输入框 / 发送按钮"是**靠肉眼
读图**硬编码出来的。换台机器换个分辨率就废, 而且调用方一旦读不了图整条链路就断。

现在链路是这样的, 每一步都由 repo 自己的代码算出答案::

    标签: remote.client 截图 -> vision.find_color 在标签栏找高饱和 favicon
    页面: vision.describe_grid 统计"浅色占比", 挑出最像 Gemini 的那个标签
    输入框: vision.describe_grid 找浅色空白块 -> click -> vision.diff 看聚焦反馈
    发送键: keyboard.paste 后 vision.diff + vision.dominant_colors 找饱和度最高的变化区
    回复: vision.diff 检测新出现的内容

本脚本**不读取图片的像素内容做语义判断**, 只消费 vision 返回的坐标与统计值。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from remote import vision  # noqa: E402
from remote.client import WsClient  # noqa: E402

ENDPOINT = os.environ.get("KUUKI_ENDPOINT", "ws://127.0.0.1:8765")
SHOT = {"max_width": 1000, "format": "png"}
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "repo-run")
MESSAGE = "第三次: 这一步全程由 repo 自己的 vision 定位, 没有硬编码坐标。请简短回复。"


async def frame(client: WsClient, name: str) -> tuple[bytes, float]:
    """抓一帧并存盘, 返回 (字节, 缩放系数)。"""
    header, payload = await client.screenshot(SHOT)
    with open(os.path.join(OUT, name), "wb") as handle:
        handle.write(payload)
    return payload, vision.scale_of(header)


def lightness(png: bytes) -> float:
    """这一屏里"浅色块"占多少 —— Gemini 是浅色主题, 整屏多半是白的。"""
    blocks = vision.describe_grid(png)
    if not blocks:
        return 0.0
    pale = [b for b in blocks if max(b["rgb"]) > 235]
    return len(pale) / len(blocks)


async def tab_candidates(client: WsClient) -> list[tuple[int, int]]:
    """在标签栏那条窄带里找 favicon, 返回真实屏幕坐标。

    做法: 标签栏背景是灰白, 所以任何**颜色鲜艳的小方块**都是一个网站的 favicon。
    真正的 favicon 会**在同一水平线上排成一排** —— 于是按 y 聚类后挑成员最多的
    那一排就是标签栏, 顺带把浏览器自己的 UI 图标 (地址栏那几个大块) 排除掉。
    """
    header, payload = await client.screenshot(
        {"max_width": 1680, "format": "png", "region": [0, 0, 1680, 90]}
    )
    strip = vision.load(payload)
    blocks = vision.find_saturated_blocks(
        strip, min_saturation=40, min_pixels=12, limit=40
    )
    rows: dict[int, list] = {}
    for rect in blocks:
        if not (8 <= rect.w <= 28 and 8 <= rect.h <= 28):
            continue
        key = rect.center[1] // 6
        rows.setdefault(key, []).append(rect)
    if not rows:
        return []
    # 成员最多的那一排才是标签栏
    best = max(rows.values(), key=len)
    if len(best) < 3:
        return []
    deduped: list[tuple[int, int]] = []
    for rect in sorted(best, key=lambda r: r.center[0]):
        point = rect.center
        if any(abs(point[0] - x) < 24 for x, _ in deduped):
            continue
        deduped.append(point)
    return deduped


async def probe_input(
    client: WsClient, png_before: bytes, point: tuple[int, int], scale: float
) -> dict | None:
    """点一下候选点, 用帧差判断它有没有"被聚焦的反馈"。

    输入框被点中的时候, 边框会高亮、光标会闪 —— 于是**不需要打任何字**就能确认。
    这样就避免了"往错误的窗口里粘一段文本"这种污染。
    """
    result = await client.call("mouse.click", {"x": point[0], "y": point[1]})
    await asyncio.sleep(0.45)
    png_after, _ = await frame(client, "probe.png")
    changed = vision.diff(png_before, png_after, tol=16, min_pixels=24)
    if not changed:
        return None
    cx, cy = point[0] / scale, point[1] / scale   # 回到展示坐标
    for rect in changed:
        if rect.x <= cx <= rect.x + rect.w and rect.y <= cy <= rect.y + rect.h:
            return {"point": point, "rect": rect.to_dict(scale), "changes": len(changed)}
    return None


async def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    async with WsClient(ENDPOINT, timeout=30) as client:
        info = await client.call("ping")
        print(f"[1] 连通: {info}")

        before, scale = await frame(client, "step0-start.png")
        print(f"[2] 起始帧浅色占比 {lightness(before):.0%}, 缩放系数 {scale}")

        tabs = await tab_candidates(client)
        print(f"[3] repo 认出的标签栏 favicon ({len(tabs)} 个): {tabs}")

        best = (0.0, None)
        for x, y in tabs:
            await client.call("mouse.click", {"x": x, "y": y})
            await asyncio.sleep(0.8)
            png, _ = await frame(client, "step1-tab.png")
            score = lightness(png)
            print(f"    标签 ({x},{y}) 浅色占比 {score:.0%}")
            if score > best[0]:
                best = (score, (x, y))
        score, tab = best
        print(f"[4] 最像 Gemini 的标签 = {tab} (浅色 {score:.0%})")
        if tab is None:
            return 1
        await client.call("mouse.click", {"x": tab[0], "y": tab[1]})
        await asyncio.sleep(1.0)

        png, scale = await frame(client, "step2-page.png")
        blocks = vision.describe_grid(png, scale=scale)
        # 输入框的候选 = 浅色、几乎没内容(content 低)的块, 越靠下越优先
        palest = sorted(
            [b for b in blocks if max(b["rgb"]) > 240 and b["content"] < 20],
            key=lambda b: (-b["y"], b["content"]),
        )
        print(f"[5] 浅色空白块 {len(palest)} 个, 依次试探能否聚焦")

        hit = None
        for block in palest[:8]:
            point = (block["screen"]["x"], block["screen"]["y"])
            got = await probe_input(client, png, point, scale)
            print(f"    ({point[0]:4d},{point[1]:4d}) -> {'聚焦成功' if got else '无反应'}")
            if got:
                hit = got
                break
        if not hit:
            print("[!] 没找到会聚焦的输入框, 中止 —— 不往页面里粘任何文本")
            return 2
        print(f"[6] 输入框定位成功: {hit['point']}")

        pasted = await client.call("keyboard.paste", {"text": MESSAGE})
        await asyncio.sleep(1.0)
        after_text, _ = await frame(client, "step3-typed.png")
        print(f"[7] 文本已入框 ({pasted})")

        changed = vision.diff(png, after_text, tol=16, min_pixels=40)
        # 发送按钮 = 变化区域里"颜色最鲜艳"的那一坨 (文字是黑白, 按钮是实心彩色)
        scored = []
        for rect in changed:
            image = vision.load(after_text)
            tile = image.crop((rect.x, rect.y, rect.x + rect.w, rect.y + rect.h))
            palette = vision.dominant_colors(tile, top=3)
            best_sat = max((p["saturation"] for p in palette), default=0)
            scored.append((best_sat, rect))
        scored.sort(key=lambda t: -t[0])
        for sat, rect in scored[:4]:
            print(f"    变化区 ({rect.x},{rect.y}) {rect.w}x{rect.h} 最高饱和 {sat}")

        if not scored or scored[0][0] < 30:
            print("[!] 没找到彩色发送按钮, 改用键盘回车发送")
            await client.call("keyboard.key", {"key": "enter"})
        else:
            _, button = scored[0]
            target = (round(button.center[0] * scale), round(button.center[1] * scale))
            res = await client.call("mouse.click", {"x": target[0], "y": target[1]})
            print(f"[8] 点击发送按钮 {target} -> {res.get('positioned_at')}")

        for index in range(1, 9):
            await asyncio.sleep(5)
            png_now, _ = await frame(client, f"step4-reply-{index:02d}.png")
            delta = vision.diff(after_text, png_now, tol=16, min_pixels=200)
            biggest = max((r.pixels for r in delta), default=0)
            print(f"    第 {index * 5:2d}s: 变化区 {len(delta)} 处, 最大 {biggest} 像素")
            with open(os.path.join(OUT, "result.json"), "w", encoding="utf-8") as handle:
                json.dump({"tab": tab, "input": hit["point"], "message": MESSAGE,
                           "waited": index * 5}, handle, ensure_ascii=False, indent=2)
            if biggest > 3000:
                print("[9] 回复出现了, 到 step4-reply-*.png 里看最终帧")
                return 0
        print("[9] 等待超时")
        return 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
