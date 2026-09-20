"""在**画图 (mspaint)** 上验收"读坐标"这条链路。

全程只用 repo 自己的 op / vision, 不用本机任何图形能力。做的是三件事:

1. **训练**: ``screen.calibrate`` 在这台机器上实测图坐标 <-> 鼠标坐标的标定
2. **使用**: 在画图外的截图上**读**出一个彩色目标的图坐标, 用标定换算成鼠标坐标
3. **验证**: 真点过去, 用帧差确认"点击发生在预测的那个位置"

产物 (都在这个目录下):
- ``1-before.png`` / ``2-after-click.png``  原始帧
- ``grid-*.png``                            加了网格与目标标注的证据图
- ``report.json``                           校准报告 + 回测数据

用法::

    python -u evidence/calibration/verify_mspaint.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Tuple
import time

from remote import vision
from remote.calibrate import Calibration
from remote.client import WsClient

URL = "ws://127.0.0.1:8765/"
HERE = os.path.dirname(os.path.abspath(__file__))
MAX_WIDTH = 1280


def step(text: str) -> None:
    print(f"\n== {text}")


async def shot(client: WsClient, name: str, draw_cursor: bool = False):
    """抓一帧, 存文件, 返回 (header, bytes)。"""
    args = {"max_width": MAX_WIDTH}
    if draw_cursor:
        args["draw_cursor"] = True
    header, payload = await client.screenshot(args)
    path = os.path.join(HERE, f"{name}.png")
    with open(path, "wb") as handle:
        handle.write(payload)
    print(f"    帧 {header['width']}x{header['height']} "
          f"(source {header['source_width']}x{header['source_height']}, "
          f"scale {header['scale']}) -> {os.path.basename(path)}")
    return header, payload, path


async def ensure_paint(client: WsClient, screen_width: int) -> dict:
    """打开画图并把它弄到前台最大化。返回窗口信息。

    打开用的是 **Win+R + 粘贴** 而不是"开始菜单 + 打字": 这台 Win10 会把逐字
    输入吞掉 (前台不是对话框时字符就丢了), 而剪贴板 + Ctrl+V 一定能进输入框。
    这条经验也适用于往别的输入框里填内容。
    """
    step("① 打开画图 (Win+R -> 粘贴路径 -> 回车) 并确认它真的在前台")
    found = await client.call("window.list", {"process": "mspaint"})

    if not found["count"]:
        for attempt in range(3):
            await client.call("keyboard.hotkey", {"keys": "win+r"})
            await asyncio.sleep(1.2)
            await client.call(
                "keyboard.paste", {"text": r"C:\Windows\System32\mspaint.exe"}
            )
            await asyncio.sleep(0.6)
            await client.call("keyboard.key", {"key": "enter"})
            await asyncio.sleep(3.0)
            found = await client.call("window.list", {"process": "mspaint"})
            if found["count"]:
                break
            print(f"    第 {attempt + 1} 次没起来, 再试")
    if not found["count"]:
        raise RuntimeError("打不开画图 —— 后面的验收没法做")
    window = found["windows"][0]
    print(f"    窗口 {window['title']!r} rect={window['rect']}")

    # 切前台可能要多试一次: Win+R 那条路上常常有余党 (PowerToys 的 PowerLauncher)
    # 压在上面。它是 Alt+Space 抢出来的, 所以**不要用 Alt+Space 最大化** ——
    # 那正好是它的快捷键。
    foreground = await client.call("window.foreground", {})
    for attempt in range(4):
        if foreground["process"] == "mspaint":
            break
        await client.call("keyboard.key", {"key": "esc"})
        await asyncio.sleep(0.4)
        focused = await client.call("window.focus", {"process": "mspaint"})
        print(f"    第 {attempt + 1} 次 focus: focused={focused.get('focused')} "
              f"method={focused.get('method')}")
        await asyncio.sleep(0.8)
        foreground = await client.call("window.foreground", {})

    if window["rect"]["width"] < 0.9 * screen_width:
        await client.call("keyboard.hotkey", {"keys": "win+up"})
        await asyncio.sleep(0.8)

    found = await client.call("window.list", {"process": "mspaint"})
    window = found["windows"][0]
    print(f"    前台现在是 {foreground['process']} | {foreground['title'][:50]!r}")
    print(f"    画图窗口 hwnd={window['hwnd']} rect={window['rect']}")
    if foreground["process"] != "mspaint":
        raise RuntimeError("画图没到前台 —— 后面的点击会落到别人身上")
    return window


def frame_box_of_window(calib: Calibration, rect: dict, image) -> Tuple[int, int, int, int]:
    """窗口矩形 (鼠标坐标) -> 图坐标矩形, 并夹到图的范围内。

    最大化窗口的 left/top 常常是**负数** (Win10 把边框画在可视区外), 直接交给
    vision 会让 crop 出界, 所以这里必须夹一下。
    """
    x0, y0 = calib.to_frame(rect["left"], rect["top"])
    x1, y1 = calib.to_frame(rect["left"] + rect["width"], rect["top"] + rect["height"])
    x0, x1 = max(0, min(x0, image.width)), max(0, min(x1, image.width))
    y0, y1 = max(0, min(y0, image.height)), max(0, min(y1, image.height))
    return (x0, y0, max(1, x1 - x0), max(1, y1 - y0))


#: 画图 (MS Paint) 色板上的标准色。用**具体颜色**找目标比"找鲜艳的块"可靠:
#: find_saturated_blocks 筛的是"这块里有饱和像素", 聚成簇之后主色仍然是背景色,
#: 于是可能返回一个 rgb=(253,253,254) 的白块 —— 看着莫名其妙。
PALETTE = (
    (237, 28, 36), (136, 0, 21), (255, 127, 39), (255, 201, 14),
    (34, 177, 76), (0, 162, 232), (63, 72, 204), (163, 73, 164),
    (0, 0, 0), (127, 127, 127),
)


def pick_target(image, box: Tuple[int, int, int, int]):
    """在画图窗口里找一个"点下去一定看得见反应"的目标: 色板上的彩色方块。

    用**具体颜色**找目标比"找鲜艳的块"可靠: find_saturated_blocks 筛的是"这块里
    有饱和像素", 聚成簇之后主色仍然是背景色, 于是可能返回一个 rgb=(253,253,254)
    的白块 —— 看着莫名其妙。
    """
    for color in PALETTE:
        hits = vision.find_color(
            image, color, tol=36, region=box, min_pixels=60, step=1, cell=3, limit=8
        )
        if not hits:
            continue
        target = max(hits, key=lambda rect: rect.area)
        print(f"    命中颜色 {color}: {len(hits)} 块, 取最大的一块 "
              f"{target.w}x{target.h} @ ({target.x},{target.y})")
        return target, hits
    return None, []


def hit_blocks(changed, point: Tuple[int, int], pad: int = 10):
    """点是不是落在某个"变化了的地方"里 —— 按包含关系判, 不比中心距离。

    比中心距离会误判: 点左上角一个按钮弹出一大片菜单时, 变化区域可能有大半个
    屏幕宽, 它的中心离按钮很远, 但点确实生效了。
    """
    return [
        rect for rect in changed
        if rect.x - pad <= point[0] <= rect.x + rect.w + pad
        and rect.y - pad <= point[1] <= rect.y + rect.h + pad
    ]


async def main() -> int:
    async with WsClient(URL, timeout=60) as client:
        info = await client.call("info", {})
        print(f"受控端 {info['hostname']} / {info['platform']}")
        size = await client.call("screen.size", {})
        print(f"屏幕 {size['width']}x{size['height']}")

        window = await ensure_paint(client, size["width"])

        step("② calibrate: 实测这台机器的图坐标 <-> 鼠标坐标")
        report = await client.call(
            "screen.calibrate",
            {"cols": 3, "rows": 3, "max_width": MAX_WIDTH, "settle": 0.2},
        )
        print(f"    verdict={report['verdict']} "
              f"sampled={report['sampled']}/{report['requested']}")
        print(f"    declared_scale={report.get('declared_scale')} "
              f"fit_scale={report.get('fit_scale')} "
              f"residual={report.get('residual')}")
        print(f"    origin(图左上角对应的鼠标坐标)={report.get('origin')}")
        if report.get("outliers"):
            print(f"    剔除离群点 {len(report['outliers'])} 个")
        print(f"    {report.get('advice')}")
        if report["verdict"] != "aligned" or not report.get("calibration"):
            raise RuntimeError(f"标定没通过: {report.get('advice')}")
        calib = Calibration.from_dict(report["calibration"])
        # declared_scale 是服务端按帧头算的缩放, 直接拿来和标定结果比 —— 这正是
        # 校准要回答的问题: "帧头说的"和"实测的"差多少
        declared = float(report["declared_scale"])
        print(f"    老办法只用帧头缩放: {declared:.4f}  "
              f"标定的缩放: {calib.ax:.4f} / {calib.ay:.4f}  "
              f"平移: ({calib.bx:.2f}, {calib.by:.2f})")

        step("③ 截图, 在画图窗口里用 vision 读出目标位置")
        header, payload, before_path = await shot(client, "1-before")
        image = vision.load(payload)
        box = frame_box_of_window(calib, window["rect"], image)
        print(f"    画图窗口在图上的范围 {box}")
        target, candidates = pick_target(image, box)
        if target is None:
            raise RuntimeError("画图窗口里没找到色板上的彩色方块")
        cx, cy = target.center
        print(f"    目标: 图坐标 ({cx},{cy}) size={target.w}x{target.h}")

        computed = calib.to_screen(cx, cy)
        old_way = (int(round(cx * declared)), int(round(cy * declared)))
        print(f"    标定换算的鼠标坐标 ({computed[0]},{computed[1]}), "
              f"只用帧头缩放算的 ({old_way[0]},{old_way[1]})")

        step("④ 把鼠标挪过去, 看画在截图上的光标是不是真的落在目标上")

        # 这一步是**最硬的证据**: 不涉及"点击有没有生效"这类间接判断 ——
        # 我们把预测坐标交给 mouse.move, 受控端在下一帧把自己的光标画上去,
        # 于是"算出来的位置"与"实际落点"可以直接在同一张图上比较。
        # 认光标用的是 calibrate.find_marker —— 与校准时同一套逻辑, 不自创一套。
        from remote import calibrate

        verification: dict = {}
        await client.call("mouse.move", {"x": computed[0], "y": computed[1]})
        await asyncio.sleep(0.5)
        _, payload_moved, moved_path = await shot(
            client, "1b-cursor-moved", draw_cursor=True
        )
        marker, detail = calibrate.find_marker(payload, payload_moved)
        print(f"    帧差找到变化 {len(detail['changed'])} 处, "
              f"候选标记 {len(detail['candidates'])} 个")
        if marker is None:
            raise RuntimeError("移过去之后没认出光标标记 —— 没法证明落在哪")
        offset = ((marker[0] - cx) ** 2 + (marker[1] - cy) ** 2) ** 0.5
        print(f"    光标落在图坐标 ({marker[0]:.1f},{marker[1]:.1f}), "
              f"目标是 ({cx},{cy}) —— 差 {offset:.2f}px")
        verification.update({
            "cursor_seen_at": {"x": round(marker[0], 2), "y": round(marker[1], 2)},
            "cursor_offset_px": round(offset, 2),
        })
        # 标记本身有 12px 的手臂, 认出来的中心有几像素量化误差; 6px 已经很严了
        landed_cursor = offset <= 6.0

        step("⑤ 点下去, 看目标附近有没有变化")
        await client.call("mouse.click", {})
        await asyncio.sleep(1.0)

        step("⑥ 再抓一帧, 用帧差确认点击落在哪")
        header2, payload2, after_path = await shot(client, "2-after-click", draw_cursor=True)
        image2 = vision.load(payload2)
        changed = vision.diff(payload, payload2, tol=24, min_pixels=30)
        print(f"    变化区域 {len(changed)} 处: "
              f"{[(c.x, c.y, c.w, c.h) for c in changed][:8]}")

        landed = hit_blocks(changed, (cx, cy))
        verification.update({
            "target_frame": {"x": cx, "y": cy},
            "computed_screen": {"x": computed[0], "y": computed[1]},
            "old_way_screen": {"x": old_way[0], "y": old_way[1]},
            "changed_blocks": [{"x": c.x, "y": c.y, "w": c.w, "h": c.h} for c in changed],
            "changed_at_target": bool(landed),
        })
        if landed:
            nearest = min(
                landed,
                key=lambda r: ((r.center[0] - cx) ** 2 + (r.center[1] - cy) ** 2) ** 0.5,
            )
            distance = ((nearest.center[0] - cx) ** 2 + (nearest.center[1] - cy) ** 2) ** 0.5
            print(f"    点的位置落在一块变化区域内 —— 那块 {nearest.w}x{nearest.h} "
                  f"@({nearest.x},{nearest.y}), 中心距目标 {distance:.1f}px")
            verification["hit_block"] = {"x": nearest.x, "y": nearest.y,
                                        "w": nearest.w, "h": nearest.h}
            verification["hit_center_distance_px"] = round(distance, 2)
        else:
            print("    !! 目标附近没有变化 —— 点可能没落在它身上")
        step("⑥ 存带网格的证据图")
        marks = [
            {"x": cx, "y": cy, "text": f"target -> screen({computed[0]},{computed[1]})",
             "rgb": (255, 220, 0)},
            {"x": marker[0], "y": marker[1],
             "text": f"cursor landed, off {offset:.1f}px", "rgb": (255, 0, 255)},
        ]
        for index, block in enumerate(changed[:6]):
            centre = block.center
            marks.append({"x": centre[0], "y": centre[1],
                          "text": f"changed{index}", "rgb": (0, 255, 255)})
        grid_before = os.path.join(HERE, "grid-1-before.png")
        grid_after = os.path.join(HERE, "grid-2-after-click.png")
        vision.grid_overlay(image, cols=10, rows=6, marks=marks, path=grid_before)
        vision.grid_overlay(image2, cols=10, rows=6, marks=marks, path=grid_after)
        # 光标那帧单独出一张: 它才是"换算准不准"的直接证据
        grid_moved = os.path.join(HERE, "grid-1b-cursor-moved.png")
        vision.grid_overlay(
            vision.load(payload_moved), cols=10, rows=6, marks=marks, path=grid_moved
        )
        print(f"    {os.path.basename(grid_before)}  "
              f"{os.path.basename(grid_moved)}  {os.path.basename(grid_after)}")

        report_path = os.path.join(HERE, "report.json")
        with open(report_path, "w", encoding="utf-8") as handle:
            json.dump({"calibration": report, "verification": verification},
                      handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        print(f"    {os.path.basename(report_path)}")

        # 两条都要成立: 光标真的落在目标上 (证明换算准), 且点击后目标附近变了
        # (证明点生效)。前者是校准的验收, 后者是"读出来的坐标真的能用"。
        ok = report["verdict"] == "aligned" and landed_cursor and bool(landed)
        print(f"    光标落点 {'✓' if landed_cursor else '✗'} / "
              f"点击生效 {'✓' if landed else '✗'}")
        print(f"\n结论: {'通过 —— 标定读出来的坐标真能点到目标' if ok else '没通过'}")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
