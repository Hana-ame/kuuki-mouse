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
        # 可以添加更多特殊键映射...
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

    def scroll_mouse(self, delta: int):
        """滚动鼠标滚轮。delta>0 向上滚, delta<0 向下滚。"""
        self.mouse.scroll(0, int(delta))
