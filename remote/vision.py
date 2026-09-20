"""vision —— 让"看"这件事也归属 repo。

为什么要有这个模块
------------------
控制端拿到 ``screen.screenshot`` 的只是一帧 PNG —— **图片本身不是答案**。
要让调用方能自己闭环, 必须有人把"图上哪个坐标是输入框 / 发送按钮"算出来。

以前这一步是靠肉眼看图硬编码坐标 (``mouse.click {x:959, y:546}``), 有三个毛病:

1. 换了分辨率 / 缩放比例就点偏 (见 ``docs/knowledge/gui-screenshot-scale.md``);
2. 换台机器、换个主题, 坐标全废;
3. **调用方必须自己能读图** —— 一旦它读不了图, 整条链路就断在这里。

这里把它做成一组纯函数, 把它插进链路中间::

    screenshot(op)  ->  vision.locate(...)  ->  {"x":..,"y":..}  ->  mouse.click(op)

三条定位路线, **都不需要 numpy / OpenCV, 也不需要任何预训练模型**:

- :func:`find_color`     按颜色找色块 —— 蓝底按钮、高亮边框、品牌色图标
- :func:`match_template` 按形状找图标 —— 给一张裁好的小图, 在整屏里做模板匹配
- :func:`diff`           比较前后两帧 —— 找出"哪里变了", 判断输出有没有出来
- :func:`describe_grid`  把图拆成网格输出**文本版概览** (主色 + 内容量),
                         给读不了图的调用方当"眼睛"

为什么坚持只用 Pillow
---------------------
受控端只支持 Windows, 依赖表里目前只有 pynput / Pillow / websockets。numpy 和
opencv 都不是小包, 为一个"附带工具"把 everybody 安装成本抬上去不划算。代价是
匹配比 cv2 慢, 于是做了两级加速: 先用稀疏采样把候选从整屏筛到个位数量级, 再
在候选附近做精匹配。实测 1000x625 的帧 + 60x60 的模板约 2-5 秒, 够用。

坐标是两回事
------------
本模块返回的坐标默认**是展示坐标** —— 截图被 ``max_width`` 缩放过。要喂给
``mouse.*`` / ``keyboard.*`` 必须乘 :func:`scale_of` 得到的系数还原成**真实屏幕
坐标**。忘了这一步就会点偏 1.5 倍, 这是反复踩过的坑。所有 CLI 输出里, 两个坐标
都给: ``x/y`` 是展示坐标, ``screen.x/screen.y`` 是可以直接拿去点的。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageChops, ImageStat

try:  # Pillow >= 9.1
    _RESAMPLE = Image.Resampling.LANCZOS
    _BOX = Image.Resampling.BOX
except AttributeError:  # pragma: no cover - 老 Pillow
    _RESAMPLE = Image.LANCZOS
    _BOX = Image.BOX


@dataclass(frozen=True)
class Rect:
    """图上一块矩形。坐标是**展示坐标** (截图自身的像素坐标系)。"""

    x: int
    y: int
    w: int
    h: int
    score: float = 1.0      # 置信度: 颜色纯度 / 匹配得分 / 差异强度
    pixels: int = 0         # 命中的像素数 (估算值)
    rgb: Optional[Tuple[int, int, int]] = None   # 该块的主色 (find_saturated_blocks 才有)

    @property
    def center(self) -> Tuple[int, int]:
        return (self.x + self.w // 2, self.y + self.h // 2)

    @property
    def area(self) -> int:
        return self.w * self.h

    def to_dict(self, scale: float = 1.0) -> Dict[str, Any]:
        cx, cy = self.center
        return {
            "x": self.x,
            "y": self.y,
            "w": self.w,
            "h": self.h,
            "score": round(float(self.score), 4),
            "pixels": int(self.pixels),
            "rgb": list(self.rgb) if self.rgb else None,
            "center": {"x": cx, "y": cy},
            # 真实屏幕坐标 —— 直接喂给 mouse.click 的那个
            "screen": {"x": _to_screen(cx, scale), "y": _to_screen(cy, scale)},
        }


# ---------------------------------------------------------------- 载入与坐标换算


def load(source: Any) -> Image.Image:
    """从 bytes / 文件路径 / 已有 Image 载入一张图。"""
    if isinstance(source, Image.Image):
        return source
    if isinstance(source, (bytes, bytearray)):
        import io

        return Image.open(io.BytesIO(bytes(source)))
    return Image.open(source)


def scale_of(header: Optional[Dict[str, Any]]) -> float:
    """由截屏帧头算出"展示坐标 -> 真实屏幕坐标"的放大系数。

    真实屏幕 1680 宽、截图按 ``max_width=1000`` 回来时, 截图宽确实是 1000, 但
    ``source_width`` 才是 1680 —— 系数 1.68。**只看 ``width`` 会算成 1.0**, 然后
    鼠标就点在偏差半个屏幕的地方了。

    没有 source_width 的时候宁可返回 1.0 也不猜 —— 但调用方应当把它当成
    "这一帧不能用来换算", CLI 里是直接拒绝的 (见 ``_run_locate``)。
    """
    if not header:
        return 1.0
    src = header.get("source_width") or 0
    out = header.get("width") or 0
    if not src or not out or src == out:
        return 1.0
    return float(src) / float(out)


def _to_screen(value: float, scale: float) -> int:
    return int(round(value * scale))


def to_screen(rect: Rect, scale: float) -> Tuple[int, int]:
    """把一块矩形的中心换算成真实屏幕坐标。"""
    cx, cy = rect.center
    return (_to_screen(cx, scale), _to_screen(cy, scale))


# ------------------------------------------------------------------ 网格聚簇


def _clusters(
    hits: Sequence[Tuple[int, int]],
    cell: int,
    step: int,
    limit_w: int,
    limit_h: int,
) -> List[Tuple[int, int, int, int, int]]:
    """把稀疏采样出来的命中点聚成矩形簇。

    ``hits`` 是网格坐标集合。用 4-邻域 BFS 合并相邻格 —— 对角相接不算, 否则一条
    45 度的细线会把整张图连成一坨。返回 ``(x0, y0, x1, y1, count)`` 的真实像素
    包围盒 (uncrop 之前的布尔掩码坐标)。
    """
    remaining = set(hits)
    out: List[Tuple[int, int, int, int, int]] = []
    while remaining:
        seed = remaining.pop()
        stack = [seed]
        comp = [seed]
        while stack:
            gx, gy = stack.pop()
            for nb in ((gx + 1, gy), (gx - 1, gy), (gx, gy + 1), (gx, gy - 1)):
                if nb in remaining:
                    remaining.discard(nb)
                    stack.append(nb)
                    comp.append(nb)
        xs = [c[0] for c in comp]
        ys = [c[1] for c in comp]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        px0 = max(0, x0 * cell)
        py0 = max(0, y0 * cell)
        px1 = min(limit_w, (x1 + 1) * cell)
        py1 = min(limit_h, (y1 + 1) * cell)
        out.append((px0, py0, px1, py1, len(comp)))
    return out


def find_color(
    image: Any,
    rgb: Tuple[int, int, int],
    tol: int = 24,
    region: Optional[Tuple[int, int, int, int]] = None,
    min_pixels: int = 60,
    step: int = 2,
    cell: int = 6,
    limit: int = 20,
) -> List[Rect]:
    """找出图里所有接近指定颜色的色块。

    典型用途: 找蓝底发送按钮、找聚焦态输入框的彩色描边、找某个品牌色图标。

    ``tol`` 是**每个通道**的容差, 判定用切比雪夫距离 (max |Δ|), 不用欧氏 ——
    屏幕上的抗锯齿边缘会产生一串过渡色, 欧氏距离会把它们全算进来, 色块边缘
    糊成一圈毛边。

    ``step`` 稀疏采样步长: 隔 ``step`` 个像素检查一次, 速度差 ``step**2`` 倍。
    代价是极小 features 会被跳过去, 默认 2 对 >=12px 的目标足够。

    ``cell`` 聚簇网格边长: 命中点先落到 ``cell`` 大小的格子里再合并连通域,
    所以返回结果的最小外接可能比真实目标大 ``cell`` 像素左右 —— 取
    ``center`` 点按钮没问题, 拿来算精确面积不合适。
    """
    img = load(image).convert("RGB")
    ox, oy = 0, 0
    if region:
        ox, oy = region[0], region[1]
        img = img.crop((ox, oy, ox + region[2], oy + region[3]))
    width, height = img.size
    if width == 0 or height == 0:
        return []

    tr, tg, tb = rgb
    # tobytes() 比 getdata() 快一个数量级: 它是连续内存, 不做 tuple 装箱
    raw = img.tobytes()
    stride = width * 3
    hits: List[Tuple[int, int]] = []
    seen: set = set()
    for y in range(0, height, step):
        row = y * stride
        gy = y // cell
        for x in range(0, width, step):
            i = row + x * 3
            dr = raw[i] - tr
            dg = raw[i + 1] - tg
            db = raw[i + 2] - tb
            # 先用一个廉价的下界排掉绝大多数点, 再做完整比较
            if abs(dr) <= tol and abs(dg) <= tol and abs(db) <= tol:
                key = (x // cell, gy)
                if key not in seen:
                    seen.add(key)
                    hits.append(key)

    if not hits:
        return []

    # 命中点数是"格数", 换算成真实像素要乘 **采样步长** 而不是格子边长:
    # 一次采样代表 step² 个像素。乘 cell² 会把 12x12 的小方块也算成 144 像素,
    # 于是 min_pixels 形同虚设。
    unit = step * step
    rects: List[Rect] = []
    for x0, y0, x1, y1, count in _clusters(hits, cell, step, width, height):
        approx = count * unit
        if approx < min_pixels:
            continue
        rects.append(
            Rect(
                x=x0 + ox,
                y=y0 + oy,
                w=x1 - x0,
                h=y1 - y0,
                score=min(1.0, approx / max(1, (x1 - x0) * (y1 - y0))),
                pixels=approx,
            )
        )
    rects.sort(key=lambda r: r.area, reverse=True)
    return rects[:limit]


# ------------------------------------------------------------------ 模板匹配


def match_template(
    image: Any,
    template: Any,
    threshold: float = 0.85,
    coarse_divisor: int = 10,
    candidates: int = 8,
    refine_radius: int = 3,
    limit: int = 5,
) -> List[Rect]:
    """在大图里找小图出现的位置。

    两级: 先在大图与小图都降采样到模板短边约 ``coarse_divisor`` 像素的尺度上做
    **全屏粗筛** (这是 O(W*H*tw*th) 的部分, 降采样把它压成 1/k⁴), 拿到若干个候选
    后回到原分辨率, 在候选附近 ±k 像素内做**精匹配**。

    ``score`` 是 ``1 - 平均绝对差 / 255``, 1.0 表示逐像素完全相同。真机截图有
    JPEG-ish 的亚像素重采样差异, 同一台机器上同一个图标通常能到 0.9+; 低于
    0.85 基本就是"自认为匹配上了", 别信。

    **模板必须与目标同分辨率**: 从 1000 宽的帧上裁的模板, 拿去比 1400 宽的帧
    会全部 miss。要么两边都用同一个 ``max_width``, 要么先把模板缩放到目标图的
    同一比例。
    """
    img = load(image).convert("L")
    tpl = load(template).convert("L")
    iw, ih = img.size
    tw, th = tpl.size
    if tw == 0 or th == 0 or tw > iw or th > ih:
        return []

    k = max(1, min(tw, th) // max(2, coarse_divisor))
    cw, ch = max(1, tw // k), max(1, th // k)
    small_i = img.resize((max(1, iw // k), max(1, ih // k)), _BOX)
    small_t = tpl.resize((cw, ch), _BOX)
    sw, sh = small_i.size
    if cw > sw or ch > sh:
        return []

    s_bytes = small_i.tobytes()
    t_bytes = small_t.tobytes()
    # 精匹配要用**原尺寸**模板。这里只能拿降采样那份的字节去比, 越界是必然的 ——
    # 一级一组字节, 别混着用。
    f_bytes = tpl.tobytes()
    s_stride = sw
    t_pixels = cw * ch

    # ---- 第一级: 降采样图上的 SAD 滑窗 ----
    scored: List[Tuple[int, int, int]] = []  # (sad, gx, gy)
    for gy in range(0, sh - ch + 1):
        for gx in range(0, sw - cw + 1):
            sad = 0
            for ty in range(ch):
                s_off = (gy + ty) * s_stride + gx
                t_off = ty * cw
                for tx in range(cw):
                    sad += abs(s_bytes[s_off + tx] - t_bytes[t_off + tx])
            scored.append((sad, gx, gy))
    if not scored:
        return []
    scored.sort(key=lambda t: t[0])

    # ---- 第二级: 候选附近精匹配 ----
    i_bytes = img.tobytes()
    i_stride = iw
    full_pixels = tw * th
    results: List[Tuple[int, int, int, int, float]] = []
    best_plain: Optional[float] = None
    for sad, gx, gy in scored[:candidates]:
        if best_plain is not None and sad > best_plain * 3 + 1000:
            break  # 粗筛已经差很远了, 不用浪费时间精算
        base_x, base_y = gx * k, gy * k
        for dy in range(-refine_radius * k, refine_radius * k + 1, max(1, k // 2 or 1)):
            y = base_y + dy
            if y < 0 or y + th > ih:
                continue
            for dx in range(-refine_radius * k, refine_radius * k + 1, max(1, k // 2 or 1)):
                x = base_x + dx
                if x < 0 or x + tw > iw:
                    continue
                total = 0
                for ty in range(0, th, 2):  # 隔行采样: 视觉冗余足够, 省一半时间
                    i_off = (y + ty) * i_stride + x
                    t_off = ty * tw
                    for tx in range(0, tw, 2):
                        total += abs(i_bytes[i_off + tx] - f_bytes[t_off + tx])
                # 只采了 1/4 的像素, 乘回来
                approx_sad = total * 4
                score = 1.0 - approx_sad / float(255 * full_pixels)
                if best_plain is None or score > best_plain:
                    best_plain = score
                if score >= threshold:
                    results.append((x, y, tw, th, score))

    # 同一目标会被多个候选重复命中, 按位置去重 (中心距离小于模板尺寸算同一个)
    unique: List[Rect] = []
    for x, y, w, h, score in sorted(results, key=lambda t: -t[4]):
        cx, cy = x + w // 2, y + h // 2
        if any(abs(cx - (u.x + u.w // 2)) < w // 2 and abs(cy - (u.y + u.h // 2)) < h // 2
               for u in unique):
            continue
        unique.append(Rect(x=x, y=y, w=w, h=h, score=score, pixels=w * h))
        if len(unique) >= limit:
            break
    return unique


# -------------------------------------------------------------------- 帧差


def find_saturated_blocks(
    image: Any,
    min_saturation: int = 45,
    min_pixels: int = 16,
    region: Optional[Tuple[int, int, int, int]] = None,
    step: int = 1,
    cell: int = 4,
    with_color: bool = True,
    limit: int = 40,
) -> List[Rect]:
    """找出图上**颜色鲜艳**的色块 —— 也就是"找图标"。

    :func:`find_color` 要预先知道目标颜色, 可现实里往往是反过来的: 知道"这里应该
    有个图标", 但不知道它是什么颜色。于是改成按饱和度筛: 灰白背景 sat 接近 0,
    而 favicon / 按钮 / logo 这种东西必然带颜色。

    典型用法是"在一条窄带里找图标": 先把浏览器的标签栏单独截出来, 这里一找就能
    拿到每个标签的 favicon 位置 —— 坐标可以直接去点。

    ``min_saturation`` 是 ``max(rgb) - min(rgb)`` 的下限。40 左右能滤掉文字噪点,
    太低会把灰底上的黑色文字也算进来 (深色文字的差值也可能不小), 太高又会漏掉
    低饱和的浅色图标。
    """
    img = load(image).convert("RGB")
    ox, oy = 0, 0
    if region:
        ox, oy = region[0], region[1]
        img = img.crop((ox, oy, ox + region[2], oy + region[3]))
    width, height = img.size
    if width == 0 or height == 0:
        return []

    raw = img.tobytes()
    stride = width * 3
    hits: List[Tuple[int, int]] = []
    seen: set = set()
    for y in range(0, height, step):
        row = y * stride
        gy = y // cell
        for x in range(0, width, step):
            i = row + x * 3
            r, g, b = raw[i], raw[i + 1], raw[i + 2]
            if max(r, g, b) - min(r, g, b) < min_saturation:
                continue
            key = (x // cell, gy)
            if key not in seen:
                seen.add(key)
                hits.append(key)

    rects: List[Rect] = []
    for x0, y0, x1, y1, count in _clusters(hits, cell, step, width, height):
        approx = count * step * step
        if approx < min_pixels:
            continue
        rgb = None
        if with_color:
            tile = img.crop((x0, y0, x1, y1))
            palette = dominant_colors(tile, top=1)
            rgb = tuple(palette[0]["rgb"]) if palette else None
        rects.append(
            Rect(x=x0 + ox, y=y0 + oy, w=x1 - x0, h=y1 - y0,
                 score=min(1.0, approx / max(1, (x1 - x0) * (y1 - y0))),
                 pixels=approx, rgb=rgb)
        )
    rects.sort(key=lambda r: r.area, reverse=True)
    return rects[:limit]


def diff(
    before: Any,
    after: Any,
    tol: int = 18,
    min_pixels: int = 40,
    cell: int = 8,
    limit: int = 20,
) -> List[Rect]:
    """比较两帧, 返回"发生了变化"的区域。

    这是**不需要看懂画面**就能确认"我刚才那一步生效了吗"的手段: 点了一下输入
    框 -> 光标闪烁 / 边框高亮 -> diff 里会出现一小块; 粘贴了一段文字 -> 文字区域
    整块变化; 模型开始吐字 -> 回复区每帧都在变。

    两帧必须同尺寸, 不同就用 0 填充到较大尺寸再比 (截图尺寸变化本身也是信息)。
    """
    a = load(before).convert("RGB")
    b = load(after).convert("RGB")
    if a.size != b.size:
        w = max(a.width, b.width)
        h = max(a.height, b.height)
        canvas = Image.new("RGB", (w, h), (0, 0, 0))
        canvas.paste(a, (0, 0))
        a = canvas
        canvas = Image.new("RGB", (w, h), (0, 0, 0))
        canvas.paste(b, (0, 0))
        b = canvas

    width, height = a.size
    mask = ImageChops.difference(a, b).convert("L").point(lambda p: 255 if p > tol else 0)
    raw = mask.tobytes()
    stride = width
    hits: List[Tuple[int, int]] = []
    for y in range(0, height, 2):
        row = y * stride
        gy = y // cell
        for x in range(0, width, 2):
            if raw[row + x]:
                key = (x // cell, gy)
                if key not in hits:
                    hits.append(key)
    # 去重 (上面用线性查找, 命中率低时比维护 set 更快)
    hits = list(dict.fromkeys(hits))

    rects: List[Rect] = []
    for x0, y0, x1, y1, count in _clusters(hits, cell, 2, width, height):
        approx = count * cell * cell
        if approx < min_pixels:
            continue
        area = max(1, (x1 - x0) * (y1 - y0))
        rects.append(
            Rect(x=x0, y=y0, w=x1 - x0, h=y1 - y0,
                 score=min(1.0, approx / area), pixels=approx)
        )
    rects.sort(key=lambda r: r.area, reverse=True)
    return rects[:limit]


# ------------------------------------------------------------ 文本版图面概览


def describe_grid(
    image: Any, cols: int = 8, rows: int = 5, scale: float = 1.0
) -> List[Dict[str, Any]]:
    """把图切成 ``cols x rows`` 块, 每块输出主色与"内容量"。

    存在的理由很直接: **调用方读不了图的时候也要能知道屏幕上大概有什么**。
    主色用 ``resize`` 拿到 (一次 downsample 就是每块的均值), 内容量用该块的像素
    标准差 —— 纯色留白接近 0, 有文字/图标的块会明显高。

    于是"哪块是浏览器内容区""哪块是没打开的桌面"这类判断, 读 JSON 就能做。

    ``scale`` 给了就一并输出 ``screen`` (真实屏幕坐标)。直接调本函数的人常常忘了
    这一步, 于是拿展示坐标去点鼠标 —— 所以宁可让函数多算一次。
    """
    img = load(image).convert("RGB")
    width, height = img.size
    cw, ch = max(1, width // cols), max(1, height // rows)
    out: List[Dict[str, Any]] = []
    for row in range(rows):
        for col in range(cols):
            box = (col * cw, row * ch, min(width, (col + 1) * cw), min(height, (row + 1) * ch))
            tile = img.crop(box)
            if tile.width == 0 or tile.height == 0:
                continue
            mean = ImageStat.Stat(tile).mean
            r, g, b = (int(v) for v in mean[:3])
            try:
                sd = ImageStat.Stat(tile.convert("L")).stddev[0]
            except Exception:  # pragma: no cover - 单像素块
                sd = 0.0
            cx = box[0] + (box[2] - box[0]) // 2
            cy = box[1] + (box[3] - box[1]) // 2
            out.append(
                {
                    "col": col,
                    "row": row,
                    "x": box[0],
                    "y": box[1],
                    "w": box[2] - box[0],
                    "h": box[3] - box[1],
                    "rgb": [r, g, b],
                    "hex": f"#{r:02x}{g:02x}{b:02x}",
                    "content": round(float(sd), 1),
                    "center": {"x": cx, "y": cy},
                    "screen": {"x": _to_screen(cx, scale), "y": _to_screen(cy, scale)},
                }
            )
    return out


def dominant_colors(image: Any, blocks: int = 1, top: int = 6) -> List[Dict[str, Any]]:
    """列出图里出现最多的几种颜色 (量化到 32 级)。

    用来回答"这屏的主色调是什么" —— 找到高饱和的那一个, 通常就是按钮或者品牌
    色图标, 再把它喂给 :func:`find_color` 精确定位。
    """
    img = load(image).convert("RGB")
    small = img.resize((max(1, img.width // blocks), max(1, img.height // blocks)), _RESAMPLE)
    quant = small.quantize(colors=32, method=Image.Quantize.FASTOCTREE)
    palette = quant.getpalette() or []
    counts = quant.getcolors() or []
    total = sum(c for c, _ in counts) or 1
    out: List[Dict[str, Any]] = []
    for count, index in sorted(counts, reverse=True)[:top]:
        base = index * 3
        r, g, b = palette[base : base + 3] if base + 2 < len(palette) else (0, 0, 0)
        out.append(
            {
                "rgb": [int(r), int(g), int(b)],
                "hex": f"#{int(r):02x}{int(g):02x}{int(b):02x}",
                "share": round(count / total, 4),
                "saturation": int(max(r, g, b) - min(r, g, b)),
            }
        )
    return out


# -------------------------------------------------------------------- 模板存档


def save_template(image: Any, box: Tuple[int, int, int, int], path: str) -> str:
    """从一帧里裁一块存成模板文件, 供以后 :func:`match_template` 使用。

    这是"自举": 先用别的手段 (颜色 / 帧差) 定位一次, 把结果裁下来存盘, 之后就
    能用模板匹配快速复现同一个目标。
    """
    img = load(image)
    x, y, w, h = box
    crop = img.crop((x, y, x + w, y + h))
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    crop.save(path)
    return path
