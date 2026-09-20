"""排查"认标记"为什么认错: 单个靶点, 把帧差区域与红色候选全打印出来。

校准那一步出错时, 报告只会说"残差 426px"。看数量级知道是认错了标记, 但认成了
**什么**得亲眼看 —— 这个脚本就是那双眼睛。用法::

    python -u evidence/calibration/diagnose_marker.py [x] [y] [max_width]
"""

import asyncio
import sys

from remote.client import WsClient
from remote import vision
from remote.calibrate import RED

URL = "ws://127.0.0.1:8765/"


async def main() -> int:
    x = int(sys.argv[1]) if len(sys.argv) > 1 else 840
    y = int(sys.argv[2]) if len(sys.argv) > 2 else 500
    max_width = int(sys.argv[3]) if len(sys.argv) > 3 else 1280

    async with WsClient(URL, timeout=30) as client:
        size = await client.call("screen.size")
        print(f"屏幕 {size['width']}x{size['height']}  目标 ({x},{y})  max_width={max_width}")

        await client.call("mouse.move", {"x": x, "y": y})
        await asyncio.sleep(0.4)

        header_before, before = await client.screenshot({"max_width": max_width})
        await asyncio.sleep(0.25)
        header_after, after = await client.screenshot(
            {"max_width": max_width, "draw_cursor": True}
        )
        print(f"帧 {header_after['width']}x{header_after['height']} "
              f"source={header_after['source_width']}x{header_after['source_height']} "
              f"scale={header_after['scale']}")
        print(f"帧头报的光标: {header_after.get('cursor')}")

        changed = vision.diff(before, after, tol=18, min_pixels=40)
        print(f"\n帧差区域 {len(changed)} 处:")
        for rect in changed:
            print(f"  ({rect.x:5d},{rect.y:5d}) {rect.w:4d}x{rect.h:4d}  pixels={rect.pixels}")

        image = vision.load(after)
        hits = vision.find_color(image, RED, tol=32, min_pixels=16, step=1, cell=3, limit=30)
        print(f"\n全屏红色候选 {len(hits)} 处:")
        for hit in hits:
            cx, cy = hit.center
            print(f"  ({hit.x:5d},{hit.y:5d}) {hit.w:4d}x{hit.h:4d}  "
                  f"center=({cx},{cy})  pixels={hit.pixels}  score={hit.score:.2f}")

        expected = vision.scale_of(header_after)
        want = (round(x / expected), round(y / expected))
        print(f"\n按帧头换算, 标记应当出现在图上 {want} (系数 {expected:.4f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
