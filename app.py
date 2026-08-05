# 空气鼠标主逻辑 (对齐重构前原版, meromeromeiro/kuuki-mouse)
#
# 驱动方式 (原版):
#   手机浏览器 deviceorientation 给的是绝对角度 (alpha=绕z, beta=绕x, gamma=绕y),
#   浏览器内部已融合 (磁力计定航向+重力定俯仰), 不存在积分漂移。
#   -> v_x = (alpha - 上帧alpha) * sensitivity
#      v_y = (beta  - 上帧beta)  * sensitivity
#   每帧直接算, 无速度惯性累积, 无陀螺仪积分:
#   - 手机静止 -> 绝对角度不变 -> delta=0 -> 光标不动 (不滑)
#   - 手机转动 -> 光标跟随角度差, 停住即停
#
# 重要: 不要用陀螺仪(Mahony)积分出的方位角驱动鼠标!
#   陀螺零偏会让方位角在静止时缓慢漂移, 导致光标持续滑动 (前几版的问题)。

import math

from controller import PynputMouseController
from attitude import wrap_deg

JUMP_GUARD_DEG = 90.0  # 单帧角度跳变超过此值视为传感器毛刺, 丢弃


class App(PynputMouseController):
    def __init__(
        self,
        sensitivity: float = 300.0,   # 角度(°)->速度 增益 (对齐原版 300)
        deadzone: float = 0.15,      # 死区(°): 小于此的角度变化忽略 (吸收静止抖动)
        scale: float = 0.1,          # 速度->像素 缩放 (对齐原版 0.1, 净 30px/度)
    ):
        super().__init__()
        self.sensitivity = sensitivity
        self.deadzone = deadzone
        self.scale = scale

        self._prev_alpha = None  # 上帧绝对方位角 alpha (度)
        self._prev_beta = None   # 上帧绝对俯仰角 beta (度)
        self.v_x = 0.0
        self.v_y = 0.0

    def calibrate(self):
        """重置: 丢弃上一帧, 避免换手/放置后产生巨大跳变。"""
        self._prev_alpha = None
        self._prev_beta = None
        self.v_x = 0.0
        self.v_y = 0.0

    # ---- 主更新 ----
    def update_data(
        self, x: float, y: float, z: float,
        alpha: float, beta: float, gamma: float,
        gx: float = 0.0, gy: float = 0.0, gz: float = 0.0,
    ):
        # 首帧: 只记录基准
        if self._prev_alpha is None:
            self._prev_alpha = alpha
            self._prev_beta = beta
            self.v_x = 0.0
            self.v_y = 0.0
            return

        # 绝对角度的变化 (原版: delta = alpha - prev_alpha)
        d_az = wrap_deg(alpha - self._prev_alpha)
        d_el = wrap_deg(beta - self._prev_beta)

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

        # 直接按绝对角度变化驱动, 无惯性
        self.v_x = d_az * self.sensitivity
        self.v_y = d_el * self.sensitivity

        self._prev_alpha = alpha
        self._prev_beta = beta

    def update_mouse(self):
        # 与原版一致: 横轴 alpha -> X, 纵轴 beta -> Y (符号按手感)
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
        print("已重新校准姿态基准 (丢弃上一帧, 防跳变)")
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
