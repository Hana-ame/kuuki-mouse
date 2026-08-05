# 空气鼠标主逻辑
#
# 设计 (按你的坐标系定义 + 可用的物理信号):
#   Y (上下) = 手机长轴指向方向 与 地面 的夹角          (俯仰)
#   X (左右) = 指向方向 相对 校准基准铅垂面 的左右偏角 (偏航)
#
# 信号源 (关键, 别再踩之前的坑):
#   - 两个角都从 Mahony 陀螺积分四元数解出 (姿态解算保留, attitude.py)。
#     陀螺短时间积分平滑、无磁罗盘跳变。
#   - **不用** deviceorientation 的绝对 alpha (磁力计罗盘) 直接驱动:
#     罗盘在室内受磁场干扰会自己漂移/跳变, 手机静止 alpha 也在变,
#     之前"静止还在飘"就是它造成的。
#
# 抗漂移核心 — 静止冻结 (stillness freeze):
#   - 用陀螺转速 |gx,gy,gz| (静止时≈0, 不受磁场影响) 判断手机是否真在动。
#   - 持续静止 120ms -> 光标冻结在原地, 并把角度基准重锚定到当前值。
#     => 手不动: 光标绝对不动 (不管罗盘/噪声怎么变);
#        手再动: 从冻结点开始按角度差移动, 漂移永远无法累积。
#
# 驱动方式: 相对角度变化 (v = 角度差 * 增益), 无惯性, 静止即停。

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

JUMP_GUARD_DEG = 90.0   # 单帧角度跳变超过此值: 视为毛刺, 重锚定基准 (光标不跳)
DEADZONE_DEG = 0.15     # 角度差死区(°): 小于此的微小变化忽略
SMOOTHING = 0.15        # 光标低通系数 (0~1): 越小越跟手, 越大越稳
FULL_RANGE_X_DEG = 90.0  # 左右 ±45° 覆盖整个屏宽 (增益 = 屏宽/此值)
FULL_RANGE_Y_DEG = 60.0  # 上下 ±30° 覆盖整个屏高

STILL_GYRO_RAD_S = 0.06  # 静止判定: 陀螺转速低于此 (rad/s, ≈3.4°/s)
STILL_TIME_S = 0.12      # 持续静止多久才冻结 (秒)


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

        # 角度基准 (相对模式, 静止时重锚定)
        self._ref_az = None
        self._ref_el = None
        self._last_az = None
        self._last_el = None

        # 光标位置 (低通平滑)
        self._sx, self._sy = self._cx, self._cy

        # 静止检测
        self._still_since = None
        self._frozen = False

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
        """校准: 当前指向方向设为基准, 光标回到屏幕中心。"""
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

        # ---- 静止冻结: 手不动 -> 光标绝对不动 + 重锚定基准 (消除一切漂移) ----
        if gyro_mag < STILL_GYRO_RAD_S:
            if self._still_since is None:
                self._still_since = now
            if now - self._still_since >= STILL_TIME_S:
                self._frozen = True
                self._ref_az, self._ref_el = az, el   # 重锚定 -> 漂移无法累积
                return                                # 光标不动
        else:
            self._still_since = None
            self._frozen = False

        if self._ref_az is None:
            self._ref_az, self._ref_el = az, el
            return

        d_az = wrap_deg(az - self._ref_az)
        d_el = el - self._ref_el

        # 跳变保护: 毛刺 -> 重锚定, 不跳光标
        if abs(d_az) > JUMP_GUARD_DEG or abs(d_el) > JUMP_GUARD_DEG:
            self._ref_az, self._ref_el = az, el
            return

        # 死区: 微小角度差忽略
        if abs(d_az) < DEADZONE_DEG:
            d_az = 0.0
        if abs(d_el) < DEADZONE_DEG:
            d_el = 0.0

        # 相对角度变化驱动 (无惯性)
        tx = self._sx + d_az * self._gain_x
        ty = self._sy - d_el * self._gain_y
        tx = max(0, min(self._w - 1, tx))
        ty = max(0, min(self._h - 1, ty))

        # 轻微低通 (Mahony 已平滑, 这里只是去残余抖动)
        self._sx = self._sx * SMOOTHING + tx * (1 - SMOOTHING)
        self._sy = self._sy * SMOOTHING + ty * (1 - SMOOTHING)

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
