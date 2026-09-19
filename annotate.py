"""在截图上叠加坐标网格并标记候选控件, 用于核对"我算的坐标到底偏没偏"。

输出一张带标注的图:
  * 每 100px 一条主网格线, 每 20px 一条细线; 主网格线标坐标数值
  * 边框带刻度尺
  * 用十字+圆圈标出给定的目标点, 旁边写坐标
  * 用矩形框标出检测到的色块包围盒

用法:
    python annotate.py <输入png> <输出png> [--mark x,y=标签 ...] [--color-mask]
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Tuple

from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    "/mnt/c/Windows/Fonts/msyh.ttc",
    "/mnt/c/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

GRID_MINOR = 20
GRID_MAJOR = 100


def load_font(size: int = 14):
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def draw_grid(draw: ImageDraw.ImageDraw, w: int, h: int, font) -> None:
    """画坐标网格 + 刻度数值。"""
    for x in range(0, w, GRID_MINOR):
        major = (x % GRID_MAJOR == 0)
        color = (90, 90, 100) if major else (48, 48, 54)
        draw.line([(x, 0), (x, h)], fill=color, width=1)
        if major and x:
            draw.text((x + 2, 2), str(x), fill=(150, 200, 255), font=font)
            draw.text((x + 2, h - 18), str(x), fill=(150, 200, 255), font=font)
    for y in range(0, h, GRID_MINOR):
        major = (y % GRID_MAJOR == 0)
        color = (90, 90, 100) if major else (48, 48, 54)
        draw.line([(0, y), (w, y)], fill=color, width=1)
        if major and y:
            draw.text((2, y + 2), str(y), fill=(150, 200, 255), font=font)
            draw.text((w - 46, y + 2), str(y), fill=(150, 200, 255), font=font)


def mark_point(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    label: str,
    color: Tuple[int, int, int],
    font,
    arm: int = 26,
    radius: int = 9,
) -> None:
    """十字 + 圆圈标一个点。"""
    draw.line([(x - arm, y), (x + arm, y)], fill=color, width=2)
    draw.line([(x, y - arm), (x, y + arm)], fill=color, width=2)
    draw.ellipse([x - radius, y - radius, x + radius, y + radius], outline=color, width=3)
    text = f"{label} ({x},{y})"
    tw = draw.textlength(text, font=font)
    # 标签放在点的右上方, 避免压住目标
    tx, ty = x + arm + 6, y - arm - 4
    if tx + tw > draw.im.size[0]:
        tx = x - arm - 6 - tw
    if ty < 0:
        ty = y + arm + 4
    # 半透明底衬, 保证可读
    draw.rectangle([tx - 3, ty - 2, tx + tw + 3, ty + 18], fill=(0, 0, 0))
    draw.text((tx, ty), text, fill=color, font=font)


def mark_box(draw: ImageDraw.ImageDraw, box, label: str, color, font) -> None:
    x0, x1, y0, y1 = box
    draw.rectangle([x0, y0, x1, y1], outline=color, width=3)
    text = f"{label} box ({x0},{y0})-({x1},{y1}) {x1 - x0}x{y1 - y0}"
    draw.rectangle([x0, max(0, y0 - 20), x0 + draw.textlength(text, font=font) + 6, y0],
                   fill=(0, 0, 0))
    draw.text((x0 + 3, max(0, y0 - 19)), text, fill=color, font=font)


def find_color_box(im: Image.Image, kind: str = "magenta"):
    """找色块包围盒。magenta = 品红按钮; blue = 蓝色按钮。"""
    w, h = im.size
    xs, ys = [], []
    step = 1
    for y in range(0, h, step):
        for x in range(0, w, step):
            r, g, b = im.getpixel((x, y))
            if kind == "magenta" and r > 200 and g < 90 and b > 200:
                xs.append(x); ys.append(y)
            elif kind == "blue" and b > 180 and 80 < r < 175 and 120 < g < 205:
                xs.append(x); ys.append(y)
    if not xs:
        return None
    return (min(xs), max(xs), min(ys), max(ys)), len(xs)


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--mark", action="append", default=[],
                    help="x,y=标签 (可多次)")
    ap.add_argument("--box", default=None,
                    help="x0,x1,y0,y1=标签 手动画框")
    ap.add_argument("--auto", choices=["magenta", "blue"], default=None,
                    help="自动检测该颜色色块并画框")
    ap.add_argument("--scale", type=float, default=1.0, help="输出缩放")
    args = ap.parse_args(argv)

    im = Image.open(args.src).convert("RGB")
    w, h = im.size
    print(f"图像尺寸: {w}x{h}")

    # 先在原图上检测色块 —— 必须在叠黑底之前做, 否则颜色被压暗、阈值判不中
    auto_found = find_color_box(im, args.auto) if args.auto else None

    # 再叠一层半透明黑, 让网格和标注更清楚
    overlay = Image.new("RGB", (w, h), (0, 0, 0))
    im = Image.blend(im, overlay, 0.25)
    draw = ImageDraw.Draw(im)
    font = load_font(15)
    font_small = load_font(13)

    draw_grid(draw, w, h, font_small)

    if args.auto:
        if auto_found:
            box, npx = auto_found
            cx, cy = (box[0] + box[1]) // 2, (box[2] + box[3]) // 2
            mark_box(draw, box, f"auto-{args.auto}", (0, 255, 120), font)
            mark_point(draw, cx, cy, "auto center", (0, 255, 120), font)
            print(f"自动检测 {args.auto}: box={box} 中心=({cx},{cy}) 像素={npx}")
        else:
            print(f"未检测到 {args.auto}")

    if args.box:
        spec, _, label = args.box.partition("=")
        x0, x1, y0, y1 = (int(v) for v in spec.split(","))
        mark_box(draw, (x0, x1, y0, y1), label or "box", (255, 200, 0), font)

    for item in args.mark:
        spec, _, label = item.partition("=")
        x, y = (int(v) for v in spec.split(","))
        mark_point(draw, x, y, label or "mark", (255, 80, 80), font)

    if args.scale and args.scale != 1.0:
        im = im.resize((int(w * args.scale), int(h * args.scale)), Image.LANCZOS)

    im.save(args.dst)
    print(f"已保存: {args.dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
