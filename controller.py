import time
from pynput.mouse import Button, Controller as MouseController
from pynput.keyboard import Controller as KeyboardController, Key


class PynputMouseController:
    """鼠标/键盘控制器封装。

    内部保持 mouse / keyboard 两个独立 pynput 控制器实例:
    - pynput 的 MouseController 和 KeyboardController 都有 press/release/tap,
      如果双继承 (MouseController, KeyboardController), MRO 里 MouseController 在前,
      KeyboardController.tap() 内部调用的 self.press() 会解析到鼠标按键版,
      把键盘 Key 传进去会报 "required argument is not an integer"。
    - 所以这里用组合而不是继承, 各自方法走各自的实例。
    """

    special_keys = {
        "backspace": Key.backspace,
        "enter": Key.enter,
        "tab": Key.tab,
        "space": Key.space,
        "shift": Key.shift,
        "ctrl": Key.ctrl,
        "alt": Key.alt,
        "esc": Key.esc,
        "delete": Key.delete,
        # --- 标准键盘要有的一整排: 手机端"标准键盘"布局按下去得真有用 ---
        # 少了这些, 键盘上摆着方向键 / F1~F12 却按不动, 比不摆还糟。
        "up": Key.up,
        "down": Key.down,
        "left": Key.left,
        "right": Key.right,
        "home": Key.home,
        "end": Key.end,
        "page_up": Key.page_up,
        "page_down": Key.page_down,
        "insert": Key.insert,
        "cmd": Key.cmd,          # Windows 键
        **{f"f{i}": getattr(Key, f"f{i}") for i in range(1, 13)},
    }
    mouse_buttons = {
        "left": Button.left,
        "right": Button.right,
        "middle": Button.middle,
        # 兼容性
        "left click": Button.left,
        "right click": Button.right,
        "middle click": Button.middle,
    }

    def __init__(self):
        self.mouse = MouseController()
        self.keyboard = KeyboardController()

    def get_mouse_position(self):
        """获取当前鼠标位置。"""
        x, y = self.mouse.position
        return {"x": int(x), "y": int(y)}

    def move_mouse(self, x: int, y: int):
        """将鼠标移动到指定位置。"""
        self.mouse.move(x, y)

    def click_mouse(self, button: str):
        """点击鼠标按钮。"""
        button = PynputMouseController.mouse_buttons.get(button)
        if button:
            self.mouse.click(button)
        else:
            raise ValueError(
                "Unsupported button type. Use 'left', 'right', or 'middle'."
            )

    def type_text(self, text: str, interval: float = 0.05):
        """
        模拟键盘输入文本。

        :param text: 要输入的文本
        :param interval: 字符之间的间隔时间（秒），默认0.05秒
        """
        for char in text:
            self.keyboard.type(char)
            time.sleep(interval)

    def tap_key(self, key: str):
        """按一下键盘上的键 (特殊键或普通字符)。"""
        key = PynputMouseController.special_keys.get(key.lower(), key.lower())
        self.keyboard.tap(key)

    def hotkey(self, combo: str):
        """按一次组合键: ``hotkey("ctrl+c")`` / ``hotkey("ctrl+shift+s")``。

        按下按书写顺序, 松开逆序 —— 和真人按键盘一样, 也和
        ``remote/input.py`` 的 ``hotkey()`` 保持一致。
        手机端没有"按住"这个动作 (触屏没有 hover), 所以组合键只能靠这种
        "一次性Combo" 表达: 点一下 Ctrl, 再点 C, 就等于 Ctrl+C。
        """
        parts = [p.strip().lower() for p in str(combo).split("+") if p.strip()]
        if not parts:
            raise ValueError("组合键不能为空")
        keys = [PynputMouseController.special_keys.get(p, p) for p in parts]
        for key in keys:
            self.keyboard.press(key)
        for key in reversed(keys):
            self.keyboard.release(key)

    def scroll_mouse(self, delta: int):
        """滚动鼠标滚轮。delta>0 向上滚, delta<0 向下滚。"""
        self.mouse.scroll(0, int(delta))
