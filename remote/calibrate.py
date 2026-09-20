"""calibrate —— 把"图上坐标"和"鼠标坐标"对起来, 并用数据确认真的对上了。

为什么需要
----------
``vision`` 返回的坐标是**图坐标** —— 截图被 ``max_width`` 缩过, 所以返回值的
``screen`` 那一项要从 :func:`vision.scale_of` 拿系数还原。这条换算藏着两个假设:

1. 图是屏幕**均匀缩放**来的, 一个 ``source_width / width`` 就能还原;
2. 图的左上角就是鼠标坐标系的原点 (0, 0)。

假设 1 通常是真的。假设 2 **在多显示器上经常是假的**: Windows 虚拟桌面允许负
坐标 —— 副屏摆在主屏左边时它的 x 从负数开始, 而 ``ImageGrab.grab()`` 抓的就是
整个虚拟桌面。这时图坐标与鼠标坐标之间差一个**固定平移**, 于是每个点都偏同样
的量, 而且看不出来: 乘出来的数字看着很合理, 本地省事 —— 这类"看着对"的错误最难查。

与其翻文档相信这条换算, 不如把它测出来。闭环是::

    鼠标移到已知位置 -> 截图时把光标画上 -> 在图上认出那个标记
    -> 攒一组 (图坐标, 鼠标坐标) 样本 -> 最小二乘拟合 -> 报告残差

需要的零件仓库里都有 (``mouse.move`` / ``draw_cursor`` / ``vision`` 的帧差与找色),
不额外装东西, 也不需要人在旁边看屏幕。做完一次, 这台机器这条链路准不准就有数了。

fit 出来的东西怎么用
--------------------
:meth:`Calibration.to_screen` 可以替代 ``vision.to_screen`` —— 后者只做缩放,
前者**缩放和平移一起做**。多余的一步只是把 :func:`run` 的结果存下来喂给它。
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageChops

from . import vision
from .screen import CURSOR_ARM, CURSOR_COLOR

__all__ = [
    "Calibration",
    "CalibrationError",
    "Sample",
    "fit",
    "fit_robust",
    "marker_center",
    "find_marker",
    "plan_points",
    "run",
]

#: 截图里画的光标标记就是这个红, 尺寸就是 :data:`_ARM`。两个值从 ``remote.screen``
#: **引过来**而不是重写一遍: 那边改了颜色, 这边会从"能用"变成"忽然找不到标记",
#: 而且根因在另一个文件里, 极难往这儿想。
RED: Tuple[int, int, int] = CURSOR_COLOR

#: 拟合最少要几个点。两点只能连成一条直线, 没有"多余观测"就发现不了拟合错。
_MIN_SAMPLES = 3

#: 光标标记的手臂长度 (像素, **图坐标系**): 用来估认出来的色块够不够大, 也用来
#: 排点时给边缘留够位置 —— 标记画在帧的边上会被裁掉一半, 重心就往里偏了。
_ARM: int = CURSOR_ARM

_MARKER_MIN_EDGE = 8
_MARKER_MAX_EDGE = 72


class CalibrationError(ValueError):
    """样本不足以拟合, 或者拟合结果不能信。"""


# ------------------------------------------------------------------ 数据结构


@dataclass(frozen=True)
class Sample:
    """一个校准点: 鼠标在 ``screen_*``, 对应的标记落在图上 ``frame_*``。"""

    screen_x: float
    screen_y: float
    frame_x: float
    frame_y: float

    def to_dict(self) -> Dict[str, float]:
        return {
            "screen": {"x": self.screen_x, "y": self.screen_y},
            "frame": {"x": round(self.frame_x, 3), "y": round(self.frame_y, 3)},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Sample":
        screen = data.get("screen") or {}
        frame = data.get("frame") or {}
        return cls(
            screen_x=float(screen.get("x", 0)),
            screen_y=float(screen.get("y", 0)),
            frame_x=float(frame.get("x", 0)),
            frame_y=float(frame.get("y", 0)),
        )


@dataclass(frozen=True)
class Calibration:
    """图坐标 <-> 鼠标坐标的换算: ``screen = a * frame + b`` (逐轴)。

    ``b`` 就是**平移项**: 单显示器上是 0, 虚拟桌面左上角有负坐标时它是那个负
    数。只有缩放、没有平移的场合 ``b`` 拟合出来就是 0, 于是退化成
    ``vision.to_screen`` 那条老路 —— 一份代码两种机器都对。
    """

    ax: float
    bx: float
    ay: float
    by: float
    #: 拟合残差 (像素, 屏幕坐标系)。**这是可信度本身**: 拟合总能算出来,
    #: 残差才说明样本认得对不对。
    rmse: float = 0.0
    max_abs: float = 0.0
    count: int = 0

    def to_screen(self, frame_x: float, frame_y: float) -> Tuple[int, int]:
        return (
            int(round(self.ax * float(frame_x) + self.bx)),
            int(round(self.ay * float(frame_y) + self.by)),
        )

    def to_frame(self, screen_x: float, screen_y: float) -> Tuple[int, int]:
        """反过来: 鼠标坐标 -> 图坐标 (用来在图上圈出某个屏幕位置)。"""
        return (
            int(round((float(screen_x) - self.bx) / self.ax)),
            int(round((float(screen_y) - self.by) / self.ay)),
        )

    @property
    def offset(self) -> Tuple[float, float]:
        """图左上角 (0,0) 对应的鼠标坐标 —— 多显示器上常常不是 (0,0)。"""
        return (self.bx, self.by)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ax": round(self.ax, 6),
            "bx": round(self.bx, 4),
            "ay": round(self.ay, 6),
            "by": round(self.by, 4),
            "rmse": round(self.rmse, 4),
            "max_abs": round(self.max_abs, 4),
            "count": self.count,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Calibration":
        return cls(
            ax=float(data.get("ax", 1.0)),
            bx=float(data.get("bx", 0.0)),
            ay=float(data.get("ay", 1.0)),
            by=float(data.get("by", 0.0)),
            rmse=float(data.get("rmse", 0.0)),
            max_abs=float(data.get("max_abs", 0.0)),
            count=int(data.get("count", 0)),
        )


# ------------------------------------------------------------------ 拟合


def _fit_axis(frame: Sequence[float], screen: Sequence[float], axis: str):
    """一元最小二乘。返回 ``(slope, intercept, 残差)``。"""
    n = len(frame)
    mean_f = sum(frame) / n
    mean_s = sum(screen) / n
    var = sum((f - mean_f) ** 2 for f in frame)
    # 所有点的 frame 坐标相同 -> 斜率有无穷多解。这不是"数据不好", 是"这个
    # 维度的标定没法做", 必须喊出来而不是给个看似合理的数。
    if var <= 1e-9:
        raise CalibrationError(
            f"{axis} 轴上 {n} 个样本的图坐标全都一样 ({frame[0]}), 拟合不出斜率; "
            f"cols/rows 至少要有一个 >1"
        )
    cov = sum((f - mean_f) * (s - mean_s) for f, s in zip(frame, screen))
    slope = cov / var
    intercept = mean_s - slope * mean_f
    residuals = [s - (slope * f + intercept) for f, s in zip(frame, screen)]
    return slope, intercept, residuals


def fit(samples: Sequence[Sample]) -> Calibration:
    """由样本拟合标定。

    :raises CalibrationError: 样本少于 :data:`_MIN_SAMPLES`, 或者某一轴上所有点
        的图坐标相同。
    """
    points = list(samples)
    if len(points) < _MIN_SAMPLES:
        raise CalibrationError(
            f"只有 {len(points)} 个样本, 拟合至少需要 {_MIN_SAMPLES} 个; "
            f"多了才知道有没有认错标记"
        )

    frame_x = [p.frame_x for p in points]
    frame_y = [p.frame_y for p in points]
    screen_x = [p.screen_x for p in points]
    screen_y = [p.screen_y for p in points]

    ax, bx, res_x = _fit_axis(frame_x, screen_x, "x")
    ay, by, res_y = _fit_axis(frame_y, screen_y, "y")
    if abs(ax) < 1e-6 or abs(ay) < 1e-6:
        raise CalibrationError(
            f"拟合出的缩放系数接近 0 (ax={ax:.6g}, ay={ay:.6g}) —— 图坐标和鼠标"
            f"坐标八成串了轴或者认错了标记, 不用这个结果"
        )

    errors = [(dx * dx + dy * dy) ** 0.5 for dx, dy in zip(res_x, res_y)]
    rmse = (sum(e * e for e in errors) / len(errors)) ** 0.5
    return Calibration(
        ax=ax, bx=bx, ay=ay, by=by,
        rmse=rmse, max_abs=max(errors), count=len(points),
    )


# ------------------------------------------------------------------ 采样规划


def _point_error(calib: "Calibration", point: Sample) -> float:
    pred_x = calib.ax * point.frame_x + calib.bx
    pred_y = calib.ay * point.frame_y + calib.by
    return ((pred_x - point.screen_x) ** 2 + (pred_y - point.screen_y) ** 2) ** 0.5


def fit_robust(
    samples: Sequence[Sample], threshold: float = 2.0
) -> Tuple[Calibration, List[Tuple[Sample, float]]]:
    """拟合, 但**先不假设每个点都认对了**。

    为什么需要这一层: 认标记靠的是"帧差区域里那块红的尺寸对得像十字", 而屏幕
    上动的东西不只有光标 —— 动画、加载指示器、视频里跳过来的红 logo, 尺寸凑巧
    一样就会混进来。实测本机 9 个点里有 4 个认错到屏幕别处, 直接拟合出来的系数
    差 19%, 而**报告上看不出来**: 残差很大, 但归因是"okin 错了", 说不清错在哪。

    做法是最朴素的那种一致性投票: 枚举三点组合拟一条线, 看有多少点同意它
    (残差 <= ``threshold``), 票数最多的那条胜出, 再用全部赞成者重拟合。
    三点组合在 9 点规模下只有 84 种, 不必用随机采样。

    :return: ``(标定, [(被剔除的样本, 它与模型的偏离), ...])``
    :raises CalibrationError: 连三点都达不成一致 —— 这时不要给任何结果
    """
    points = list(samples)
    if len(points) < _MIN_SAMPLES:
        raise CalibrationError(
            f"只有 {len(points)} 个样本, 拟合至少需要 {_MIN_SAMPLES} 个"
        )

    best_model: Optional[Calibration] = None
    best_inliers: List[Sample] = []
    best_penalty = float("inf")
    threshold = max(0.5, float(threshold))
    for combo in itertools.combinations(points, _MIN_SAMPLES):
        try:
            candidate = fit(list(combo))
        except CalibrationError:
            continue        # 这三点本身退化 (某轴同值), 构不成模型
        inliers = [p for p in points if _point_error(candidate, p) <= threshold]
        # 票数相同就比残差和 —— 避免"平票时随便挑一个"
        penalty = sum(_point_error(candidate, p) for p in inliers)
        if len(inliers) > len(best_inliers) or (
            len(inliers) == len(best_inliers) and penalty < best_penalty
        ):
            best_model, best_inliers, best_penalty = candidate, inliers, penalty

    if best_model is None or len(best_inliers) < _MIN_SAMPLES:
        raise CalibrationError(
            f"{len(points)} 个样本里找不出 {_MIN_SAMPLES} 个互相一致的点 "
            f"(容差 {threshold}px) —— 标记多半全认错了, 别用这次的标定"
        )

    # 用全部"赞成票"重拟合一遍: 三点确定的直线本身就有误差, 多点拟合更稳
    final = fit(best_inliers)
    # 还得看**票数够不够**: 任意三点都能拟合出一条过自己身的直线, 所以
    # RANSAC 永远找得出一个"最佳模型" —— 全是垃圾数据也能给出三个点的一致。
    # 多数点都互相不一致时不要给结果, 宁让调用方重跑 (通常是屏幕动态太多)。
    _MIN_INLIER_RATIO = 0.5
    if len(best_inliers) / len(points) < _MIN_INLIER_RATIO:
        raise CalibrationError(
            f"{len(points)} 个点里只有 {len(best_inliers)} 个互相一致 "
            f"(不到一半, 容差 {threshold}px) —— 认错标记太多, 换帧率更平稳的时候重跑"
        )

    outliers = [
        (point, _point_error(final, point))
        for point in points
        if _point_error(final, point) > threshold
    ]
    return final, outliers


def plan_points(
    width: int,
    height: int,
    cols: int = 3,
    rows: int = 3,
    margin: float = 0.12,
    min_margin: int = 0,
) -> List[Tuple[int, int]]:
    """在 ``width x height`` 的屏幕上按网格取 ``cols * rows`` 个靶点。

    ``margin`` 是留边**比例**, ``min_margin`` 是留边的**像素下限** —— 后者用来兜
    住"缩得很小的图": 标记尺寸在图坐标系里是固定的 :data:`_ARM`, 屏幕比例上的
    12% 缩到图上可能不足 12px, 于是标记被裁掉一半, 认出来的重心往里偏, 残差就
    下不来。调用方把 ``(_ARM + 2) / 缩放系数`` 传进来即可。

    ``cols`` / ``rows`` 各自为 1 也能跑 (那一轴拟合不出斜率, 会报
    :class:`CalibrationError`), 网格默认 3x3 = 9 点。
    """
    cols = max(1, int(cols))
    rows = max(1, int(rows))
    margin = min(0.45, max(0.0, float(margin)))
    left_pad = max(width * margin, float(min_margin))
    top_pad = max(height * margin, float(min_margin))
    # 留边不能大到把网格挤没了: 各留至少 1px, 极端窄屏也不至于算出负宽度
    left_pad = min(left_pad, max(0.0, width / 2 - 1))
    top_pad = min(top_pad, max(0.0, height / 2 - 1))
    span_x = width - 2 * left_pad
    span_y = height - 2 * top_pad
    out: List[Tuple[int, int]] = []
    for row in range(rows):
        for col in range(cols):
            fx = col / (cols - 1) if cols > 1 else 0.5
            fy = row / (rows - 1) if rows > 1 else 0.5
            x = int(round(left_pad + span_x * fx))
            y = int(round(top_pad + span_y * fy))
            out.append((x, y))
    return out


# ------------------------------------------------------------------ 认标记


def marker_center(
    before: Any,
    after: Any,
    diff_tol: int = 18,
    red_tol: int = 32,
    min_pixels: int = 16,
) -> Optional[Tuple[float, float]]:
    """只要位置。排障要看"为什么选错了"用 :func:`find_marker`。"""
    return find_marker(
        before, after, diff_tol=diff_tol, red_tol=red_tol, min_pixels=min_pixels
    )[0]


def find_marker(
    before: Any,
    after: Any,
    diff_tol: int = 18,
    red_tol: int = 32,
    min_pixels: int = 16,
) -> Tuple[Optional[Tuple[float, float]], Dict[str, Any]]:
    """在移动前后的两帧里认出光标标记, 返回 ``(中心图坐标, 认的过程)``。

    ``info`` 里有帧差了几块、每块里有哪些红色候选、最后选了哪一个 —— 残差大的
    时候报告只能说"认错了", 认成了**什么**得看这个。

    两步而不是一步: 先 :func:`vision.diff` 找出"这一屏哪儿变了", 再只在变化
    区域里找红 (:func:`vision.find_color`)。直接整屏找红会把屏幕本身的红色 UI
    也算进来 —— 桌面主题、图标、错误提示都可能是红的。帧差把范围压到"刚才动过
    的地方"之后, 干扰基本就没了。

    认不出来返回 ``(None, info)`` (比如这一帧被别的窗口动画盖过、受控端改了标记
    颜色)。
    """
    info: Dict[str, Any] = {"changed": [], "candidates": [], "selected": None}
    changed = vision.diff(before, after, tol=diff_tol, min_pixels=min_pixels)
    info["changed"] = [
        {"x": r.x, "y": r.y, "w": r.w, "h": r.h, "pixels": r.pixels} for r in changed
    ]
    if not changed:
        return (None, info)

    image = vision.load(after)
    width, height = image.size
    best: Optional[Tuple[float, float, float]] = None   # (面积, x, y)
    for rect in changed:
        # 帧差给出的包围盒往往比真实变化紧, 往外放宽几个像素再找红
        pad = _ARM
        x = max(0, rect.x - pad)
        y = max(0, rect.y - pad)
        w = min(width - x, rect.w + pad * 2)
        h = min(height - y, rect.h + pad * 2)
        if w <= 0 or h <= 0:
            continue
        hits = vision.find_color(
            image, RED, tol=red_tol, region=(x, y, w, h),
            min_pixels=min_pixels, step=1, cell=3,
        )
        for hit in hits:
            cx, cy = hit.center
            edge_ok = (
                _MARKER_MIN_EDGE <= hit.w <= _MARKER_MAX_EDGE
                and _MARKER_MIN_EDGE <= hit.h <= _MARKER_MAX_EDGE
            )
            info["candidates"].append({
                "x": hit.x, "y": hit.y, "w": hit.w, "h": hit.h,
                "pixels": hit.pixels, "accepted": bool(edge_ok),
            })
            if not edge_ok:
                continue
            area = float(hit.area)
            if best is None or area > best[0]:
                best = (area, cx, cy)
    if best is None:
        return (None, info)
    # 最后一步在像素级收紧: find_color 的中心是按 cell 网格算的, 有 ±cell/2 的
    # 量化误差 —— 校准是在量亚像素级的东西, 这一步不能省。
    box = (
        max(0, int(best[1]) - _MARKER_MAX_EDGE // 2),
        max(0, int(best[2]) - _MARKER_MAX_EDGE // 2),
        min(width, int(best[1]) + _MARKER_MAX_EDGE // 2),
        min(height, int(best[2]) + _MARKER_MAX_EDGE // 2),
    )
    center = _refine(image, box, red_tol) or (best[1], best[2])
    info["selected"] = {"x": round(center[0], 2), "y": round(center[1], 2)}
    return (center, info)


def _refine(image, box: Tuple[int, int, int, int], tol: int):
    """在候选框里取纯红像素的**像素级**包围盒中心。

    ``find_color`` 的 ``Rect`` 是按 cell 网格聚合出来的, 用它直接做拟合会引入
    ``cell/2`` 的系统偏差。这里回到原始像素算 getbbox, 没有就用 None。
    """
    crop = image.crop(box)
    gap = ImageChops.difference(crop, Image.new("RGB", crop.size, RED))
    mask = gap.convert("L").point(lambda p: 255 if p <= tol else 0)
    bbox = mask.getbbox()
    if not bbox:
        return None
    return (
        box[0] + (bbox[0] + bbox[2]) / 2.0,
        box[1] + (bbox[1] + bbox[3]) / 2.0,
    )


# ------------------------------------------------------------------ 闭环


def _position_of(controller: Any) -> Optional[Tuple[int, int]]:
    try:
        x, y = controller.position()
        return (int(x), int(y))
    except Exception:
        return None


def run(
    screen: Any,
    controller: Any,
    cols: int = 3,
    rows: int = 3,
    margin: float = 0.12,
    settle: float = 0.1,
    tolerance: float = 2.0,
    max_width: Optional[int] = None,
    restore: bool = True,
    sleep: Any = None,
    reporter: Any = None,
) -> Dict[str, Any]:
    """跑一次完整校准: 移鼠标 -> 画光标抓帧 -> 认标记 -> 拟合 -> 报告。

    :param screen: ``remote.screen.ScreenCapture`` (要有 ``screen_size`` /
        ``capture``; 测试里换成假的同样能跑)
    :param controller: ``remote.input.InputController``
    :param settle: 移过去之后等多久再抓帧 (移完了立刻抓, 有时会抓到上一帧)
    :param tolerance: 判定"这条换算没偏"的残差上限, 单位像素
    :param max_width: 顺带用这个宽度做一次缩放校准 (不填就是不缩放, 只测平移)
    :param restore: 结束后把鼠标挪回原位。默认 True —— 校准会动鼠标, 这是副作用,
        悄悄把人家光标留在角落不礼貌
    :param reporter: ``(名字, dict) -> None`` 回调, 每个采样点调一次。残差大的
        时候报告只能说"某个标记认错了", 认成了什么得靠它把过程捞出来。
    :return: 可直接 JSON 化的报告 dict
    """
    pause = sleep if sleep is not None else time.sleep
    # 键名跟着 screen.ScreenCapture.capture 的参数走 (是 ``fmt`` 不是 ``format``)
    opts: Dict[str, Any] = {"fmt": "png", "quality": 90}
    # max_width 为 0 / None 就是不缩放。不用 ``or``: 0 是"不缩放"的合法写法,
    # 但 negative 值也没意义, 一律当没给。
    if max_width and max_width > 0:
        opts["max_width"] = int(max_width)

    width, height = screen.screen_size()
    # 排点前先算缩放系数: 要给"图上的标记"留出不被裁的空间, 而标记尺寸是恒定的
    # :data:`_ARM` 像素, 所以所需的屏幕留边 = (arm + 余量) / 系数。
    factor = 1.0
    if max_width and max_width > 0 and max_width < width:
        factor = float(max_width) / float(width)
    min_margin = int((_ARM + 2) / max(1e-6, factor)) + 1
    points = plan_points(
        width, height, cols=cols, rows=rows, margin=margin, min_margin=min_margin
    )
    started_from = _position_of(controller)

    def shoot(cursor_position: Optional[Tuple[int, int]] = None, draw_cursor: bool = True):
        return screen.capture(
            cursor_position=cursor_position, draw_cursor=draw_cursor, **opts
        )

    samples: List[Sample] = []
    misses: List[Dict[str, Any]] = []
    for target_x, target_y in points:
        # 前帧**不画**光标: 两帧都画的话, 帧差里会出现旧位置和新的位置两个同样
        # 有效的红色标记, 认哪个全靠猜。只画后一帧, 变化区域就只剩新光标一处
        # (背景里其它动的东西没有纯红, 会在下一步被排掉)。
        before = shoot(draw_cursor=False)
        controller.move_absolute(int(target_x), int(target_y))
        if settle and settle > 0:
            pause(float(settle))
        capture = shoot(_position_of(controller), draw_cursor=True)
        center, detail = find_marker(before.data, capture.data)
        if reporter is not None:
            reporter("sample", {
                "screen": {"x": target_x, "y": target_y},
                "frame": {"x": center[0], "y": center[1]} if center else None,
                "detail": detail,
            })
        if center is None:
            # 把"这一帧里到底有什么"一起带上: 光说"没找到"没法排障
            summary = {
                "changed": len(detail["changed"]),
                "candidates": len(detail["candidates"]),
                "accepted": sum(1 for c in detail["candidates"] if c["accepted"]),
            }
            misses.append({
                "screen": {"x": target_x, "y": target_y},
                "reason": "no_marker",
                **summary,
            })
            continue
        samples.append(
            Sample(
                screen_x=float(target_x),
                screen_y=float(target_y),
                frame_x=float(center[0]),
                frame_y=float(center[1]),
            )
        )

    if restore and started_from is not None:
        controller.move_absolute(int(started_from[0]), int(started_from[1]))

    report: Dict[str, Any] = {
        "ok": False,
        "screen": {"width": int(width), "height": int(height)},
        "requested": len(points),
        "sampled": len(samples),
        "missed": misses,
        "max_width": int(opts.get("max_width", 0) or 0),
        "tolerance": float(tolerance),
        "restored": bool(restore) and started_from is not None,
    }
    if len(samples) < _MIN_SAMPLES:
        report["verdict"] = "too_few_samples"
        report["advice"] = (
            f"{len(points)} 个靶点只认出 {len(samples)} 个标记, 拟合不出来。"
            f"常见原因: 这一帧里没有 FPS 变化之外的东西可参考 (纯色桌面会让帧差失效), "
            f"或者受控端当前没有桌面会话 (锁屏 / 断开远程桌面)。"
        )
        return report

    declared = vision.scale_of(
        {"width": _frame_width(capture), "source_width": capture.source_width}
    )
    # 认标记必然会错认: 屏幕上动的东西不止光标。所以拟合走 fit_robust —— 拿多数
    # 一致的点说话, 少数离群点单独列出来而不是让它们把所有参数拉跑偏。
    try:
        calib, outliers = fit_robust(samples, threshold=max(1.0, float(tolerance)))
    except CalibrationError as exc:
        report["verdict"] = "inconsistent"
        report["advice"] = str(exc)
        return report

    drift = abs(calib.ax - declared) / declared if declared else 0.0
    aligned = calib.max_abs <= float(tolerance)
    report.update({
        "ok": True,
        "calibration": calib.to_dict(),
        "samples": [s.to_dict() for s in samples],
        "declared_scale": round(declared, 4),
        "fit_scale": round(calib.ax, 4),
        "scale_drift": round(drift, 6),
        "origin": {"x": round(calib.bx, 3), "y": round(calib.by, 3)},
        "residual": {"rmse": round(calib.rmse, 4), "max": round(calib.max_abs, 4)},
        "verdict": "aligned" if aligned else "drifted",
        "outliers": [
            {"screen": {"x": point.screen_x, "y": point.screen_y},
             "frame": {"x": round(point.frame_x, 3), "y": round(point.frame_y, 3)},
             "error": round(error, 3)}
            for point, error in outliers
        ],
        "advice": _advice(calib, declared, drift, aligned, len(misses), tolerance,
                          len(outliers)),
    })
    return report


def _frame_width(capture: Any) -> int:
    return int(getattr(capture, "width", 0) or 0)


def _advice(
    calib: Calibration,
    declared: float,
    drift: float,
    aligned: bool,
    misses: int,
    tolerance: float,
    outliers: int = 0,
) -> str:
    """把结论说成人话。机器能算出来不代表调用方看得懂, 报告要带判断。"""
    bits: List[str] = []
    if abs(calib.bx) < 0.5 and abs(calib.by) < 0.5:
        bits.append("图左上角对齐鼠标原点 (单显示器常见), 平移项为 0")
    else:
        bits.append(
            f"图左上角对应鼠标坐标 ({calib.bx:.1f}, {calib.by:.1f}) —— 虚拟桌面有偏移, "
            f"只用 vision.scale_of 换算会整体偏这么多"
        )
    if declared and drift > 0.005:
        bits.append(
            f"缩放系数与帧头声明的 {declared:.4f} 差 {drift * 100:.2f}%"
            f" (拟合值 {calib.ax:.4f})"
        )
    if aligned:
        bits.append(f"残差 {calib.max_abs:.2f}px <= 容差 {tolerance}px, 这条换算可用")
    else:
        bits.append(
            f"残差 {calib.max_abs:.2f}px 超过容差 {tolerance}px, 别用这次的结果 —— "
            f"多半是某个标记认错了: 减少屏幕上的动态内容 (关掉视频/时钟动画) 再跑一次"
        )
    if misses:
        bits.append(f"有 {misses} 个靶点没认出标记, 已跳过")
    if outliers:
        bits.append(
            f"{outliers} 个被认为是离群点没参与拟合 (屏幕上动的东西不止光标, "
            f"认错标记是常态, 靠多数投票排除); 剔掉的点在 outliers 里"
        )
    return "; ".join(bits) + "。"
