import time
import pyperclip  # 需要安装: pip install pyperclip
from pynput.mouse import Button, Controller as MouseController
from pynput.keyboard import Controller as KeyboardController, Key


# --- 1. 控制器基类 (新增了按下、释放、滚轮方法) ---
# --- 1. 控制器基类 (修复了粘贴延迟问题) ---
class PynputMouseKeyboardController:
    special_keys = {
        "shift": Key.shift,
        "control": Key.ctrl,
        "alt": Key.alt,
        "meta": Key.cmd,
        "backspace": Key.backspace,
        "enter": Key.enter,
        "tab": Key.tab,
        "space": Key.space,
        "esc": Key.esc,
        "escape": Key.esc,
        "delete": Key.delete,
        "insert": Key.insert,
        "home": Key.home,
        "end": Key.end,
        "pageup": Key.page_up,
        "pagedown": Key.page_down,
        "capslock": Key.caps_lock,
        "arrowup": Key.up,
        "arrowdown": Key.down,
        "arrowleft": Key.left,
        "arrowright": Key.right,
        "f1": Key.f1,
        "f2": Key.f2,
        "f3": Key.f3,
        "f4": Key.f4,
        "f5": Key.f5,
        "f6": Key.f6,
        "f7": Key.f7,
        "f8": Key.f8,
        "f9": Key.f9,
        "f10": Key.f10,
        "f11": Key.f11,
        "f12": Key.f12,
        "numlock": Key.num_lock,
        "scrolllock": Key.scroll_lock,
        "pause": Key.pause,
        "printscreen": Key.print_screen,
        "divide": "/",
        "multiply": "*",
        "subtract": "-",
        "add": "+",
        "decimal": ".",
    }

    mouse_buttons = {
        "left": Button.left,
        "right": Button.right,
        "middle": Button.middle,
    }

    def __init__(self):
        self.mouse = MouseController()
        self.keyboard = KeyboardController()

    def move_mouse(self, dx: int, dy: int):
        self.mouse.move(dx, dy)

    def click_mouse(self, button: str):
        btn = self.mouse_buttons.get(button.lower())
        if btn:
            self.mouse.click(btn)

    def press_mouse(self, button: str):
        btn = self.mouse_buttons.get(button.lower())
        if btn:
            self.mouse.press(btn)

    def release_mouse(self, button: str):
        btn = self.mouse_buttons.get(button.lower())
        if btn:
            self.mouse.release(btn)

    def scroll_mouse(self, dy: int):
        self.mouse.scroll(0, dy)

    def tap_key(self, key: str):
        key_lower = str(key).lower()
        target_key = self.special_keys.get(key_lower, key_lower)
        self.keyboard.tap(target_key)

    def key_down(self, key: str):
        key_lower = str(key).lower()
        target_key = self.special_keys.get(key_lower, key_lower)
        self.keyboard.press(target_key)

    def key_up(self, key: str):
        key_lower = str(key).lower()
        target_key = self.special_keys.get(key_lower, key_lower)
        self.keyboard.release(target_key)

    def paste_text(self, text: str):
        """
        修复后的粘贴逻辑：增加延时并确保剪贴板更新
        """
        try:
            # 1. 保存当前剪贴板内容 (可选，用于恢复，但在高频操作中可能会引起问题，此处省略)

            # 2. 复制内容到剪贴板
            pyperclip.copy(text)

            # 3. 增加等待时间，这是解决"先粘贴后录入"的关键
            # 0.05s 对 Windows 环境来说经常不够，建议 0.2s
            time.sleep(0.2)

            # 额外检查：确保剪贴板确实更新了 (简单验证)
            # 注意：某些环境下 pyperclip.paste() 可能会改变剪贴板状态，需谨慎
            # 这里采用最稳妥的纯延时策略

            # 4. 模拟 Ctrl+V
            self.key_down("control")
            time.sleep(0.05)  # 按键之间加微小延时
            self.tap_key("v")
            time.sleep(0.05)
            self.key_up("control")

        except Exception as e:
            print(f"剪贴板粘贴失败: {e}")
            self.type_text(text)

    def type_text(self, text: str):
        for char in text:
            self.keyboard.type(char)
            time.sleep(0.01)
