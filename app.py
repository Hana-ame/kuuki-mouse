# 空气鼠标主逻辑
#
# 结构 (保留陀螺积分 + 差值驱动):
#   1. 姿态解算: 保留 Mahony 陀螺积分 (attitude.py), 用加速度计+陀螺仪
#      (含设备朝向 alpha/beta/gamma 做参考) 融合出连续四元数姿态。
#   2. 鼠标驱动: 用姿态的绝对角度差值直接驱动 (v = 角度差 * sensitivity),
#      不做速度惯性累积, 静止即停。
#
# 手机浏览器 deviceorientation 的 alpha/beta 本身就是绝对角度 (磁力计+重力融合),
# 但保留陀螺积分可以平滑姿态、抗瞬时噪声; 两者都可用, 这里默认用姿态解算出的
# 方位角差值来驱动, 需要时可在 App(use_raw_angles=True) 切回原始 alpha/beta 差值。

import math
import time

from controller import PynputMouseController
from attitude import (
    wrap_deg,
    quat_from_accel,
    Mahony,
    q_rotate,
    normalize3,
)

JUMP_GUARD_DEG = 90.0  # 单帧角度跳变超过此值视为传感器毛刺, 丢弃


class App(PynputMouseController):
    def __init__(
        self,
        sensitivity: float = 300.0,   # 角度(°)->速度 增益 (对齐原版 300)
        deadzone: float = 0.15,      # 死区(°): 小于此的角度变化忽略 (吸收静止抖动)
        scale: float = 0.1,          # 速度->像素 缩放 (对齐原版 0.1, 净 30px/度)
        use_raw_angles: bool = False,  # True: 直接用 deviceorientation 的 alpha/beta 差值; False: 用 Mahony 姿态解算出的角度差值
        kp: float = 0.5,             # Mahony 比例增益 (跟随加速度计重力)
        ki: float = 0.1,             # Mahony 积分增益 (消除陀螺零偏)
    ):
        super().__init__()
        self.sensitivity = sensitivity
        self.deadzone = deadzone
        self.scale = scale
        self.use_raw_angles = use_raw_angles

        # 姿态解算核心: Mahony 融合 (陀螺积分 + 重力修正), 四元数表示 设备->地球
        self.mah = Mahony(kp=kp, ki=ki)
        self._inited = False
        self._last_ts = 0.0

        # 校准基准 (use_raw_angles=False 时使用)
        self._ref_az = 0.0
        self._ref_el = 0.0

        # 差值驱动状态
        self._prev_alpha = None
        self._prev_beta = None
        self._prev_az = None
        self._prev_el = None

        self.v_x = 0.0
        self.v_y = 0.0

    # ---- 姿态派生 (Mahony) ----
    def _forward(self):
        """屏幕法线(正前朝向)在世界系的方向 = q ⊗ (0,0,1)。"""
        return normalize3(q_rotate(self.mah.q, {'x': 0, 'y': 0, 'z': 1}))

    def _pointing(self):
        """由朝向四元数分解出 yaw(azimuth)/pitch(elevation), 度。"""
        f = self._forward()
        az = math.degrees(math.atan2(f['x'], f['y']))
        el = math.degrees(math.asin(max(-1, min(1, f['z']))))
        return az, el

    def calibrate(self):
        """重置基准/上一帧, 避免换手/放置后产生巨大跳变。"""
        az, el = self._pointing()
        self._ref_az = az
        self._ref_el = el
        self._prev_alpha = None
        self._prev_beta = None
        self._prev_az = None
        self._prev_el = None
        self.v_x = 0.0
        self.v_y = 0.0

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
            # 首帧: 用手持姿态初始化 Mahony 基准 (仅重力静态解算)
            self.mah.set_orientation(quat_from_accel(accel, 0))
            self._inited = True
            self.calibrate()
            return

        # 陀螺仪积分 + 重力修正 -> 连续四元数姿态
        self.mah.update(dt, gyro, accel)

        # ---- 取"角度差值"来源 ----
        if self.use_raw_angles:
            d_az = wrap_deg(alpha - self._prev_alpha) if self._prev_alpha is not None else 0.0
            d_el = wrap_deg(beta - self._prev_beta) if self._prev_beta is not None else 0.0
            self._prev_alpha = alpha
            self._prev_beta = beta
        else:
            az, el = self._pointing()
            if self._prev_az is None:
                d_az = 0.0
                d_el = 0.0
            else:
                d_az = wrap_deg(az - self._prev_az)
                d_el = wrap_deg(el - self._prev_el)
            self._prev_az = az
            self._prev_el = el

        # 跳变保护
        if abs(d_az) > JUMP_GUARD_DEG:
            d_az = 0.0
        if abs(d_el) > JUMP_GUARD_DEG:
            d_el = 0.0

        # 死区: 静止抖动 -> 不动
        if abs(d_az) < self.deadzone:
            d_az = 0.0
        if abs(d_el) < self.deadzone:
            d_el = 0.0

        # 直接按角度差值驱动, 无惯性
        self.v_x = d_az * self.sensitivity
        self.v_y = d_el * self.sensitivity

    def update_mouse(self):
        self.move_mouse(
            -int(self.v_x * self.scale),
            -int(self.v_y * self.scale),
        )


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
        print("已重新校准姿态基准 (防止换手/放置跳变)")
    else:
        app.tap_key(message)


def handle_message(msg: dict) -> bool:
    """路由一条协议消息 (JSON dict)。

    新协议带类型字段 t: sensor / mouse / text / key / calibrate;
    兼容旧协议顶层字段 (mouse/text/key 直接作为键)。
    返回 True 表示已处理。
    """
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
        mouse_event(msg["mouse"])          # 旧协议兼容
    elif "text" in msg:
        text_event(msg["text"])
    elif "key" in msg:
        key_event(msg["key"])
    else:
        return False
    return True
