"""真正在画图 (mspaint) 上**落笔画线** —— 上一轮只点了色板选中红色, 画布上没有笔迹。

流程:
1. 复用/重做坐标标定 (屏幕没变就直接用上次的 ``report.json``)
2. 把画图弄到前台
3. 确认红色已选中 (再点一次色板红块, 便宜且不会错)
4. ``mouse.drag`` 拖出红色波浪线; 若画布上没出现红色 (当前工具不是画笔),
   点一下工具区的画笔图标再画一个圆
5. 用 ``vision.find_color`` 在画布区域找红色笔迹, 对比预测路径
6. 网格证据图 + ``draw-report.json``

用法::

    python -u evidence/calibration/verify_draw.py
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
try:  # Git Bash 里 stdout 常是 GBK, 中文 print 别炸
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from verify_mspaint import ensure_paint, frame_box_of_window, shot, step  # noqa: E402

from remote import calibrate, vision  # noqa: E402
from remote.calibrate import Calibration  # noqa: E402
from remote.client import WsClient  # noqa: E402

URL = "ws://127.0.0.1:8765/"
MAX_WIDTH = 1280
RED = (237, 28, 36)
#: 画布可视区 (图坐标): 工具栏/色板都在 y<135, 从 140 起就不会误伤色板
CANVAS_BOX = (0, 140, MAX_WIDTH, 760)


def wave_points() -> list[tuple[int, int]]:
    """红色波浪线的图坐标路径: x 500->950, 基线 y=350, 振幅 60。"""
    pts = []
    for i in range(17):
        t = i / 16
        pts.append((int(500 + 450 * t), int(350 + 60 * math.sin(t * 2 * math.pi * 1.5))))
    return pts


def circle_points() -> list[tuple[int, int]]:
    """红色圆的图坐标路径: 中心 (720, 540), 半径 90, 闭合。"""
    pts = []
    for i in range(25):
        ang = 2 * math.pi * i / 24
        pts.append((int(720 + 90 * math.cos(ang)), int(540 + 90 * math.sin(ang))))
    pts.append(pts[0])
    return pts


async def reuse_or_calibrate(client: WsClient) -> Calibration:
    """屏幕没变就复用上次的标定 —— 上次 rmse 0.31px, 重测只是重复劳动。"""
    size = await client.call("screen.size", {})
    report_path = os.path.join(HERE, "report.json")
    if os.path.exists(report_path):
        with open(report_path, encoding="utf-8") as handle:
            old = json.load(handle).get("calibration", {})
        cal = old.get("calibration")
        scr = old.get("screen", {})
        if cal and scr.get("width") == size["width"] and scr.get("height") == size["height"]:
            print("    复用上次标定 (rmse={rmse:.2f}px, max={m:.2f}px)".format(
                rmse=cal["rmse"], m=cal["max_abs"]))
            return Calibration.from_dict(cal)
    print("    屏幕变了或没有旧标定, 重新 calibrate ...")
    report = await client.call(
        "screen.calibrate", {"cols": 3, "rows": 3, "max_width": MAX_WIDTH, "settle": 0.2},
    )
    if report["verdict"] != "aligned":
        raise RuntimeError(f"标定没通过: {report.get('advice')}")
    return Calibration.from_dict(report["calibration"])


def drag_args(pts_frame: list[tuple[int, int]], calib: Calibration,
              duration: float) -> dict:
    screen_pts = [calib.to_screen(x, y) for x, y in pts_frame]
    rounded = [[int(round(x)), int(round(y))] for x, y in screen_pts]
    return {"points": rounded, "duration": duration}


async def count_red(client: WsClient):
    header, payload, path = await shot(client, "draw-frame")
    image = vision.load(payload)
    hits = vision.find_color(
        image, RED, tol=36, region=CANVAS_BOX, min_pixels=20, step=1, cell=3, limit=40
    )
    return image, payload, hits


def distance_to_path(point: tuple[int, int], path: list[tuple[int, int]]) -> float:
    return min(
        ((point[0] - px) ** 2 + (point[1] - py) ** 2) ** 0.5 for px, py in path
    )


async def ensure_fg(client: WsClient) -> None:
    """确认画图在前台, 不在就抢回来。

    教训: 用户随时可能切走窗口 —— 只在开头确认一次是不够的, 后续的点击/拖动
    会落在别人身上。每个关键动作前都查一遍。
    """
    fg = await client.call("window.foreground", {})
    for attempt in range(4):
        if fg.get("process") == "mspaint":
            return
        await client.call("keyboard.key", {"key": "esc"})
        await asyncio.sleep(0.3)
        await client.call("window.focus", {"process": "mspaint"})
        await asyncio.sleep(0.8)
        fg = await client.call("window.foreground", {})
    if fg.get("process") != "mspaint":
        raise RuntimeError(f"画图没抢回前台 (现在是 {fg.get('process')!r})")


def stroke_coverage(image, path: list[tuple[int, int]], pad: int = 3) -> float:
    """沿预测路径逐点查像素: ±pad 内出现红色算命中。

    不用 find_color 的"块中心"判远近 —— 两条曲线会被聚成一簇, 外接框中心
    落在两形状之间的空白处, 看起来像"画偏了", 其实画得很准。
    """
    pixels = image.load()
    width, height = image.size
    hit = 0
    for x, y in path:
        found = False
        for xx in range(max(0, x - pad), min(width, x + pad + 1)):
            if found:
                break
            for yy in range(max(0, y - pad), min(height, y + pad + 1)):
                r, g, b = pixels[xx, yy][:3]
                if r > 180 and g < 90 and b < 90:
                    found = True
                    break
        hit += found
    return hit / len(path)


async def main() -> int:
    async with WsClient(URL, timeout=60) as client:
        info = await client.call("info", {})
        print(f"受控端 {info['hostname']} / {info['platform']}")
        size = await client.call("screen.size", {})

        step("① 画图到前台 + 标定")
        window = await ensure_paint(client, size["width"])
        calib = await reuse_or_calibrate(client)

        step("② 点色板红块, 确认前景色是红")
        header, payload, _ = await shot(client, "draw-frame")
        image = vision.load(payload)
        box = frame_box_of_window(calib, window["rect"], image)
        # 色板在窗口顶部条带里 (y<120): 不限定的话, 画布上已有的红色笔迹
        # 会比色板块大, "取最大块"就点到笔迹上去了
        strip = (box[0], box[1], box[2], min(120, box[3]))
        hits = vision.find_color(
            image, RED, tol=36, region=strip, min_pixels=60, step=1, cell=3, limit=8
        )
        if not hits:
            raise RuntimeError("画图窗口顶部没找到色板红块")
        swatch = max(hits, key=lambda rect: rect.area)
        sx, sy = calib.to_screen(*swatch.center)
        await ensure_fg(client)
        await client.call("mouse.move", {"x": int(round(sx)), "y": int(round(sy))})
        await asyncio.sleep(0.3)
        await client.call("mouse.click", {})
        await asyncio.sleep(0.5)
        print(f"    已点色板红块 图({swatch.center[0]},{swatch.center[1]}) "
              f"-> 鼠标({int(round(sx))},{int(round(sy))})")

        step("③ 落笔: 拖出红色波浪线")
        wave = wave_points()
        await ensure_fg(client)
        await client.call("mouse.drag", drag_args(wave, calib, duration=1.2))
        await asyncio.sleep(0.8)

        image2, payload2, _ = await count_red(client)
        drew = stroke_coverage(image2, wave) >= 0.5
        print(f"    波浪线预检命中率 {drew and '>=50%' or '<50%'}")

        if not drew:
            step("③b 画布上没出现红色 —— 当前工具多半不是画笔, 点工具区画笔图标再画圆")
            bx, by = calib.to_screen(183, 70)
            await ensure_fg(client)
            await client.call("mouse.move", {"x": int(round(bx)), "y": int(round(by))})
            await asyncio.sleep(0.3)
            await client.call("mouse.click", {})
            await asyncio.sleep(0.6)
        circle = circle_points()
        await ensure_fg(client)
        await client.call("mouse.drag", drag_args(circle, calib, duration=1.2))
        await asyncio.sleep(0.8)

        step("④ 验证: 画布上真的有红色笔迹吗")
        await ensure_fg(client)
        _, payload3, after_path = await shot(client, "4-after-draw")
        image3 = vision.load(payload3)
        cov_wave = stroke_coverage(image3, wave)
        cov_circle = stroke_coverage(image3, circle)
        all_hits = vision.find_color(
            image3, RED, tol=36, region=CANVAS_BOX, min_pixels=20, step=1, cell=3, limit=60
        )
        total_px = sum(h.area for h in all_hits)
        print(f"    画布红色块 {len(all_hits)} 处 / 约 {total_px} px (辅助参考)")
        print(f"    波浪线: 预测路径 {len(wave)} 点, 命中 {cov_wave:.0%}")
        print(f"    圆:     预测路径 {len(circle)} 点, 命中 {cov_circle:.0%}")

        # 各自 70% 以上的预测点 ±3px 内有红像素, 就算"真的画上去了"
        ok = cov_wave >= 0.7 and cov_circle >= 0.7
        step("⑤ 网格证据图 + 报告")
        marks = [
            {"x": x, "y": y, "text": "", "rgb": (255, 200, 0)}
            for x, y in wave[::3]
        ] + [
            {"x": x, "y": y, "text": "", "rgb": (255, 200, 0)}
            for x, y in circle[::4]
        ]
        for h in all_hits[:10]:
            marks.append({"x": h.center[0], "y": h.center[1],
                          "text": f"red {h.w}x{h.h}", "rgb": (0, 255, 255)})
        grid = os.path.join(HERE, "grid-4-after-draw.png")
        vision.grid_overlay(vision.load(payload3), cols=10, rows=6,
                            marks=marks, path=grid)
        print(f"    {os.path.basename(grid)}")

        report = {
            "wave_points_frame": wave,
            "circle_points_frame": circle,
            "red_blocks": len(all_hits),
            "red_px_approx": total_px,
            "wave_coverage": round(cov_wave, 3),
            "circle_coverage": round(cov_circle, 3),
            "ok": bool(ok),
        }
        with open(os.path.join(HERE, "draw-report.json"), "w", encoding="utf-8") as h:
            json.dump(report, h, ensure_ascii=False, indent=2)
            h.write("\n")

        print(f"\n结论: {'通过 —— 红色笔迹真的画上去了' if ok else '没画上 —— 看证据图排查'}")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
