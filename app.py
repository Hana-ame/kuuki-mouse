# 空气鼠标主逻辑
#
# 设计 (按你的定义):
#   Y (绝对位置) = 手机长轴指向方向 与地面(水平面) 的夹角  的函数
#   X (绝对位置) = 指向方向 相对 校准基准铅垂面 的左右偏角   的函数
#   -> 每帧: 光标 = 屏幕中心 + (当前角度 − 校准基准角度) × px/度
#      δ(夹角) = δ(绝对位置), 一一对应, 角度变位置立刻变, 无速度/无惯性/无低通。
#
# 信号源:
#   - 两个角都从 Mahony 陀螺积分四元数解出 (姿态解算保留, attitude.py)。
#   - 不用 deviceorientation 的绝对 alpha (磁罗盘): 室内受磁场干扰会自漂,
#     "静止还在飘"就是它造成的。
#
# 抗漂移 — 静止冻结 (只在陀螺可用时):
#   - 陀螺转速 |gx,gy,gz| 持续静止 150ms -> 光标冻结 + 角度基准重锚定。
#   - 手不动: 光标绝对不动; 手再动: 从冻结点起 δ角度→δ位置, 不跳变。
#   - 设备没提供 rotationRate (gx,gy,gz 恒为 0) 时自动禁用冻结 —
#     Mahony 无陀螺输入也不会漂移 (偏航固定在初始值)。

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

JUMP_GUARD_DEG = 90.0    # 单帧角度跳变超过此值: 视为毛刺, 重锚定基准 (光标不跳)
DEADZONE_DEG = 0.05      # 角度差死区(°): 吸收传感器微噪 (≈1°/s 慢移仍可用)
FULL_RANGE_X_DEG = 90.0  # 左右 ±45° 对应整个屏宽 (增益 = 屏宽/此值)
FULL_RANGE_Y_DEG = 60.0  # 上下 ±30° 对应整个屏高

STILL_GYRO_RAD_S = 0.05  # 静止判定: 陀螺转速低于此 (rad/s, ≈2.9°/s)
STILL_TIME_S = 0.10      # 持续静止多久才冻结 (秒)
GYRO_LIVE_THRESH = 0.02  # 判定陀螺是否可用: 转速超过此值即认为有 rotationRate


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

        # 屏幕尺寸 / 增益
        self._w, self._h = self._screen_size()
        self._cx, self._cy = self._w / 2, self._h / 2
        self._gain_x = self._w / FULL_RANGE_X_DEG   # px/度
        self._gain_y = self._h / FULL_RANGE_Y_DEG

        # 角度基准 (校准时/静止时设定)
        self._ref_az = None
        self._ref_el = None
        self._last_az = None
        self._last_el = None

        # 光标绝对位置
        self._sx, self._sy = self._cx, self._cy

        # 静止检测
        self._still_since = None
        self._gyro_live = False   # rotationRate 是否可用

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

    def _angles(self):
        """长轴指向 -> (偏航 az, 俯仰 el) 度。az: 水平投影角(相对世界); el: 与地面夹角。"""
        v = self._long_axis_world()
        el = math.degrees(math.asin(max(-1, min(1, v['z']))))
        az = math.degrees(math.atan2(v['x'], v['y']))
        return az, el

    # ---- 校准 ----
    def calibrate(self):
        """校准: 当前指向方向设为基准, 光标回屏幕中心。"""
        if self._last_az is not None and self._last_el is not None:
            self._ref_az = self._last_az
            self._ref_el = self._last_el
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
        gyro_mag = math.hypot(gyro['x'], gyro['y'], gyro['z'])

        if gyro_mag > GYRO_LIVE_THRESH:
            self._gyro_live = True

        if not self._inited:
            self.mah.set_orientation(quat_from_accel(accel, 0))
            self._inited = True
            self._last_az, self._last_el = self._angles()
            self._ref_az, self._ref_el = self._last_az, self._last_el
            return

        # Mahony 融合 (保留陀螺积分)
        self.mah.update(dt, gyro, accel)

        az, el = self._angles()
        self._last_az, self._last_el = az, el

        # ---- 静止冻结 (仅陀螺可用时): 手不动 -> 光标绝对不动 + 重锚定 ----
        if self._gyro_live and gyro_mag < STILL_GYRO_RAD_S:
            if self._still_since is None:
                self._still_since = now
            if now - self._still_since >= STILL_TIME_S:
                self._ref_az, self._ref_el = az, el   # 重锚定 -> 恢复移动时不跳变
                return                                # 光标不动
        else:
            self._still_since = None

        if self._ref_az is None:
            self._ref_az, self._ref_el = az, el
            return

        d_az = wrap_deg(az - self._ref_az)   # 相对基准铅垂面的左右偏角
        d_el = el - self._ref_el             # 相对基准的地平面夹角差

        # 跳变保护: 毛刺 -> 重锚定, 光标不跳
        if abs(d_az) > JUMP_GUARD_DEG or abs(d_el) > JUMP_GUARD_DEG:
            self._ref_az, self._ref_el = az, el
            return

        # 死区: 微噪忽略
        if abs(d_az) < DEADZONE_DEG:
            d_az = 0.0
        if abs(d_el) < DEADZONE_DEG:
            d_el = 0.0

        # ---- 绝对位置 = 角度的直接函数: δ角度 = δ绝对位置, 无速度无低通 ----
        tx = self._cx + d_az * self._gain_x
        ty = self._cy - d_el * self._gain_y
        self._sx = max(0, min(self._w - 1, tx))
        self._sy = max(0, min(self._h - 1, ty))

        try:
            self.mouse.position = (int(self._sx), int(self._sy))
        except Exception:
            pass

    def update_mouse(self):
        pass   # 已在 update_data 里定位


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
        print("已校准: 当前指向设为基准, 光标回屏幕中心")
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
    elif t == "scroll":
        app.scroll_mouse(int(msg.get("delta", 0)))
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
