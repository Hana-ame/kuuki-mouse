# 空气鼠标主逻辑 (使用 ~/my-node-app 同款姿态解算)
#
# 管线 (与 my-node-app 的 lib/attitude.js + motion/forward.js 同款):
#   传感器(手机浏览器) --WSS--> 本模块
#     设备系加速度{x,y,z} m/s² (含重力) + 设备系陀螺{gx,gy,gz} rad/s
#     + W3C 设备朝向{alpha,beta,gamma}
#     -> Mahony 互补滤波融合 -> 连续四元数 q (设备系->地球系)
#     -> 屏幕法线(正前朝向) in 世界系 = q⊗(0,0,1)
#     -> yaw(绕世界Z)/pitch(俯仰) 分解 (azimuth/elevation)
#     -> 相对校准基准的方位差 -> 死区/平滑(可选) -> 鼠标移动
#
# 鼠标驱动方式 (对齐重构前原版):
#   v = 绝对角度的变化 * sensitivity, 每帧直接算, 默认不做速度惯性累积/低通飞轮,
#   所以停住即停、不滑; 需要防抖时可开少量 smoothing (attenuation_coefficient>0)。
#
# 与旧实现的区别:
#   旧 app.py 直接用原始欧拉角差分 (alpha - prev_alpha), 有万向锁、跳变、无融合。
#   现在用四元数 + Mahony, 姿态连续平滑、抖动小、静止不漂移, 且任何朝向都稳定。

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

JUMP_GUARD_DEG = 90.0  # 单帧方位角变化超过此角度视为传感器毛刺, 丢弃


class App(PynputMouseController):
    def __init__(
        self,
        sensitivity: float = 1.0,        # 角度(°)->速度 增益 (对齐 may-node 原版感受)
        attenuation_coefficient: float = 0.0,  # 平滑系数: 0 = 停用, 按绝对角度变化直接驱动 (原版做法, 不滑)
                                              # >0 = 少量低通防抖, 保留上一帧比例
        deadzone: float = 0.15,          # 死区(°): 小于此的方位角变化忽略 (抑制静止抖动)
        scale: float = 8.0,              # 最终像素缩放
        kp: float = 0.5,                 # Mahony 比例增益 (跟随加速度计重力)
        ki: float = 0.1,                 # Mahony 积分增益 (消除陀螺零偏)
    ):
        super().__init__()
        self.sensitivity = sensitivity
        self.attenuation_coefficient = attenuation_coefficient
        self.deadzone = deadzone
        self.scale = scale

        # 姿态解算核心: Mahony 融合 (陀螺积分 + 重力修正), 四元数表示 设备->地球
        self.mah = Mahony(kp=kp, ki=ki)
        self._inited = False
        self._last_ts = 0

        # 校准基准: 启动/按需重置时的 yaw/pitch, 之后所有移动都是相对它
        self._ref_az = 0.0   # 参考方位角 yaw (deg)
        self._ref_el = 0.0   # 参考俯仰角 pitch (deg)

        self.v_x = 0.0  # 横轴速度 (deg)
        self.v_y = 0.0  # 纵轴速度 (deg)

    # ---- 姿态派生 ----
    def _forward(self):
        """屏幕法线(正前朝向)在世界系的方向 = q ⊗ (0,0,1)。"""
        return normalize3(q_rotate(self.mah.q, {'x': 0, 'y': 0, 'z': 1}))

    def _pointing(self):
        """由朝向四元数分解出 yaw(azimuth)/pitch(elevation), 度。"""
        f = self._forward()
        az = math.degrees(math.atan2(f['x'], f['y']))   # 绕世界 Z 的方位角
        el = math.degrees(math.asin(max(-1, min(1, f['z']))))  # 俯仰角
        return az, el

    def calibrate(self):
        """重置姿态基准: 当前朝向 = 鼠标不动。用于上电或放置/换手后。"""
        az, el = self._pointing()
        self._ref_az = az
        self._ref_el = el
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

        if not self._inited:
            # 首帧: 用手持姿态初始化 Mahony 基准 (仅重力静态解算, 与 my-node-app 同款)
            self.mah.set_orientation(quat_from_accel(accel, 0))
            self._inited = True
            self.calibrate()
            return

        # 陀螺仪 rad/s + 加速度计(含重力) -> Mahony 融合出连续姿态
        gyro = {'x': float(gx or 0), 'y': float(gy or 0), 'z': float(gz or 0)}
        self.mah.update(dt, gyro, accel)

        az, el = self._pointing()
        d_az = wrap_deg(az - self._ref_az)
        d_el = wrap_deg(el - self._ref_el)

        # 跳变保护: 单帧差超过阈值视为毛刺
        if abs(d_az) > JUMP_GUARD_DEG:
            d_az = 0.0
        if abs(d_el) > JUMP_GUARD_DEG:
            d_el = 0.0

        # 死区: 抑制静止抖动
        if abs(d_az) < self.deadzone:
            d_az = 0.0
        if abs(d_el) < self.deadzone:
            d_el = 0.0

        # 按绝对角度的变化直接驱动 (原版做法): 每帧 = 角度差 * 增益
        # 不累积速度惯性, 停住即停, 不"滑"; attenuation<=0 时完全停用平滑。
        att = self.attenuation_coefficient
        if att <= 0:
            self.v_x = d_az * self.sensitivity
            self.v_y = d_el * self.sensitivity
        else:
            # 少量低通防抖: 保留上一帧 att 比例 + 新帧 (1-att) 比例
            self.v_x = self.v_x * att + d_az * self.sensitivity * (1 - att)
            self.v_y = self.v_y * att + d_el * self.sensitivity * (1 - att)

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
        print("已重新校准姿态基准 (当前朝向 = 鼠标不动)")
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