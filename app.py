# 空气鼠标主逻辑 (激光笔式绝对定位)
#
# 定位方式 (用户指定):
#   Y (屏幕高度) = 手机长轴指向方向 与 地面 的夹角 (俯仰)
#   X (屏幕左右) = 指向方向 相对 校准基准铅垂面(指向屏幕方向) 的左右偏角 (偏航)
#
#   校准(calibrate): 把当前指向方向定为屏幕中心 -> 之后"指向哪, 光标到哪"。
#
# 姿态来源:
#   - 保留 Mahony 陀螺积分 (姿态解算, attitude.py), 用加速度计+陀螺融合,
#     长轴的俯仰(与地面夹角)从这里算, 重力融合 -> 稳定不漂移。
#   - 偏航(左右)用 deviceorientation 的绝对 alpha (磁力计定航向, 绝对不漂移),
#     避免陀螺积分偏航漂移。
#
# 抗抖动: 死区(中心附近小角度不动) + 目标位置低通平滑 + 跳变保护。
# 无惯性累积: 手停下 -> 指向不变 -> 光标停在原地。

import math
import sys
import time

from controller import PynputMouseController
from attitude import (
    wrap_deg,
    quat_from_accel,
    Mahony,
    q_rotate,
    normalize3,
)

JUMP_GUARD_DEG = 90.0   # 单帧角度跳变超过此值视为传感器毛刺, 丢弃该帧
DEADZONE_DEG = 0.5      # 中心附近死区(°): 指向偏差小于此 -> 光标锁定中心
SMOOTHING = 0.55        # 目标位置低通系数 (0~1): 越大越平滑(略滞后), 越小越跟手
FULL_RANGE_X_DEG = 90.0  # 左右 ±45° 覆盖整个屏宽
FULL_RANGE_Y_DEG = 60.0  # 上下 ±30° 覆盖整个屏高


class App(PynputMouseController):
    def __init__(
        self,
        kp: float = 0.5,   # Mahony 比例增益
        ki: float = 0.1,   # Mahony 积分增益
    ):
        super().__init__()
        # 姿态解算: Mahony 陀螺积分 + 重力修正 (保留)
        self.mah = Mahony(kp=kp, ki=ki)
        self._inited = False
        self._last_ts = 0.0

        # 屏幕尺寸 / 映射增益
        self._w, self._h = self._screen_size()
        self._cx, self._cy = self._w / 2, self._h / 2
        self._gain_x = self._w / FULL_RANGE_X_DEG   # px/度
        self._gain_y = self._h / FULL_RANGE_Y_DEG

        # 校准基准 (指向屏幕中心的方向)
        self._ref_az = None   # 基准偏航 (绝对 alpha)
        self._ref_el = None   # 基准俯仰 (长轴与地面夹角)

        # 当前指向
        self._last_az = None
        self._last_el = None

        # 平滑后的光标位置 (绝对坐标)
        self._sx = self._cx
        self._sy = self._cy

    # ---- 屏幕 ----
    def _screen_size(self):
        try:
            if sys.platform.startswith("win"):
                import ctypes
                w = ctypes.windll.user32.GetSystemMetrics(0)
                h = ctypes.windll.user32.GetSystemMetrics(1)
                if w > 0 and h > 0:
                    return w, h
        except Exception:
            pass
        try:
            import tkinter
            root = tkinter.Tk()
            root.withdraw()
            w, h = root.winfo_screenwidth(), root.winfo_screenheight()
            root.destroy()
            if w > 0 and h > 0:
                return w, h
        except Exception:
            pass
        return 1920, 1080

    # ---- 姿态派生 ----
    def _long_axis_world(self):
        """手机长轴 (+y, 竖屏长边) 在世界系中的方向 = q ⊗ (0,1,0)。"""
        return normalize3(q_rotate(self.mah.q, {'x': 0, 'y': 1, 'z': 0}))

    def _pitch_deg(self):
        """长轴指向方向 与地面(水平面) 的夹角, 度 (上正下负)。"""
        v = self._long_axis_world()
        return math.degrees(math.asin(max(-1, min(1, v['z']))))

    def _yaw_deg(self, alpha: float) -> float:
        """左右偏航 = deviceorientation 的绝对 alpha (磁力计, 无陀螺漂移)。"""
        return float(alpha)

    # ---- 校准 ----
    def calibrate(self):
        """把当前指向方向设为屏幕中心。"""
        if self._last_az is not None and self._last_el is not None:
            self._ref_az = self._last_az
            self._ref_el = self._last_el
        # 光标回屏幕中心
        self._sx, self._sy = self._cx, self._cy
        try:
            self.mouse.position = (int(self._cx), int(self._cy))
        except Exception:
            pass

    # ---- 主更新 ----
    def update_data(
        self, x: float, y: float, z: float,
        alpha: float, beta: float, gamma: float,
        gx: float = 0.0, gy: float = 0.0, gz: float = 0.0,
    ):
        now = time.time()
        dt = (now - self._last_ts) if self._last_ts else 1 / 60
        dt = min(max(dt, 1e-4), 0.1)
        self._last_ts = now

        accel = {'x': x, 'y': y, 'z': z}
        gyro = {'x': float(gx or 0), 'y': float(gy or 0), 'z': float(gz or 0)}

        if not self._inited:
            self.mah.set_orientation(quat_from_accel(accel, 0))
            self._inited = True
            self._last_az = self._yaw_deg(alpha)
            self._last_el = self._pitch_deg()
            return

        # Mahony 融合 (保留陀螺积分)
        self.mah.update(dt, gyro, accel)

        az = self._yaw_deg(alpha)
        el = self._pitch_deg()
        self._last_az, self._last_el = az, el

        if self._ref_az is None:
            self.calibrate()   # 首帧定基准
            return

        d_az = wrap_deg(az - self._ref_az)   # 左右偏角
        d_el = el - self._ref_el             # 上下俯仰差 (无环绕)

        # 跳变保护: 毛刺丢弃
        if abs(d_az) > JUMP_GUARD_DEG or abs(d_el) > JUMP_GUARD_DEG:
            return

        # 死区: 中心附近小角度 -> 光标锁定中心 (抑制抖动)
        if abs(d_az) < DEADZONE_DEG and abs(d_el) < DEADZONE_DEG:
            self._sx, self._sy = self._cx, self._cy
            self.mouse.position = (int(self._cx), int(self._cy))
            return

        # 绝对映射: 指向哪, 光标到哪
        tx = self._cx + d_az * self._gain_x
        ty = self._cy - d_el * self._gain_y
        tx = max(0, min(self._w - 1, tx))
        ty = max(0, min(self._h - 1, ty))

        # 目标位置低通平滑 (停手后光标收敛到目标, 不滑)
        self._sx = self._sx * SMOOTHING + tx * (1 - SMOOTHING)
        self._sy = self._sy * SMOOTHING + ty * (1 - SMOOTHING)

        try:
            self.mouse.position = (int(self._sx), int(self._sy))
        except Exception:
            pass

    def update_mouse(self):
        # 绝对模式: 已在 update_data 里定位, 这里无需再动
        pass


app = App()


def get_data(x: float, y: float, z: float, alpha: float, beta: float,
             gamma: float, gx: float = 0.0, gy: float = 0.0, gz: float = 0.0):
    app.update_data(x, y, z, alpha, beta, gamma, gx, gy, gz)
    app.update_mouse()


def mouse_event(message: str):
    app.click_mouse(message)


def text_event(message: str):
    app.type_text(message)


def key_event(message: str):
    if message.lower() in ("calibrate", "c", "recenter"):
        app.calibrate()
        print("已校准: 当前指向 = 屏幕中心 (激光笔模式)")
    else:
        app.tap_key(message)


def handle_message(msg: dict) -> bool:
    """路由一条协议消息 (JSON dict)。"""
    if not isinstance(msg, dict):
        return False
    t = msg.get("t")
    if t == "sensor":
        get_data(
            float(msg.get("x", 0)), float(msg.get("y", 0)), float(msg.get("z", 0)),
            float(msg.get("alpha", 0)), float(msg.get("beta", 0)), float(msg.get("gamma", 0)),
            float(msg.get("gx", 0)), float(msg.get("gy", 0)), float(msg.get("gz", 0)),
        )
    elif t == "mouse":
        mouse_event(msg.get("button", "left"))
    elif t == "text":
        text_event(msg.get("text", ""))
    elif t == "key":
        key_event(msg.get("key", ""))
    elif t == "calibrate":
        key_event("calibrate")
    elif "mouse" in msg:
        mouse_event(msg["mouse"])
    elif "text" in msg:
        text_event(msg["text"])
    elif "key" in msg:
        key_event(msg["key"])
    else:
        return False
    return True
