import time
import math
from controller import PynputMouseKeyboardController


# --- 2. App 类 (灵敏度调整为 2x) ---
# --- 2. App 类 (修改核心算法) ---
class App(PynputMouseKeyboardController):
    def __init__(
        self, sensitivity: float = 50.0, dead_zone: float = 0.05, scale: float = 1.0
    ):
        super().__init__()
        self.sensitivity = sensitivity  # 提高基础灵敏度
        self.scale = scale
        self.dead_zone = dead_zone  # 降低死区
        self._last_alpha = None
        self._last_beta = None
        self._pending_x = 0.0
        self._pending_y = 0.0

    def _normalize_angle_delta(self, delta: float) -> float:
        if delta > 180:
            delta -= 360
        elif delta < -180:
            delta += 360
        return delta

    def update_data(self, x, y, z, alpha, beta, gamma):
        if self._last_alpha is None:
            self._last_alpha = alpha
            self._last_beta = beta
            return

        d_alpha = self._normalize_angle_delta(alpha - self._last_alpha)
        d_beta = self._normalize_angle_delta(beta - self._last_beta)

        # 应用死区
        if abs(d_alpha) < self.dead_zone:
            d_alpha = 0
        if abs(d_beta) < self.dead_zone:
            d_beta = 0

        # --- 核心修改：平方项趋势 ---
        # 公式：output = sign(d) * (|d|)^2 * sensitivity
        # 效果：小移动更小(精准)，大移动更大(快速)

        # 计算 X 轴位移
        if d_alpha != 0:
            # d_alpha * abs(d_alpha) 相当于保留了符号的平方 (d^2)
            # 这样就不需要单独调用 sign() 函数了
            processed_alpha = d_alpha * abs(d_alpha)
            self._pending_x += -processed_alpha * self.sensitivity

        # 计算 Y 轴位移
        if d_beta != 0:
            processed_beta = d_beta * abs(d_beta)
            self._pending_y += -processed_beta * self.sensitivity

        self.update_mouse()

        self._last_alpha = alpha
        self._last_beta = beta

    def update_mouse(self):
        dx = int(self._pending_x * self.scale)
        dy = int(self._pending_y * self.scale)
        if dx != 0 or dy != 0:
            self.move_mouse(dx, dy)
            self._pending_x -= dx / self.scale
            self._pending_y -= dy / self.scale
