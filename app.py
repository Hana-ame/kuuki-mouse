import asyncio
import time
import math
from controller import PynputMouseKeyboardController

# --- 2. App 类 (还原为线性算法) ---
class App(PynputMouseKeyboardController):
    def __init__(
        self,
        sensitivity: float = 300.0,
        scale: float = 0.1,
        dead_zone: float = 0.05
    ):
        super().__init__()
        self.sensitivity = sensitivity
        self.scale = scale
        self.dead_zone = dead_zone
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

        # 线性位移计算
        self._pending_x += -d_alpha * self.sensitivity
        self._pending_y += -d_beta * self.sensitivity

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


app = App()


def get_data(x: float, y: float, z: float, alpha: float, beta: float, gamma: float):
    app.update_data(x, y, z, alpha, beta, gamma)


def mouse_event(message: str):
    app.click_mouse(message)


def text_event(message: str):
    app.paste_text(message)


def key_event(message: str):
    app.tap_key(message)


def key_down(key: str):
    app.key_down(key)


def key_up(key: str):
    app.key_up(key)
