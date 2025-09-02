from pynput.mouse import Button, Controller as MouseController

class PynputMouseController:
    def __init__(self):
        self.mouse = MouseController()

    def get_mouse_position(self):
        """获取当前鼠标位置。"""
        x, y = self.mouse.position
        return {"x": int(x), "y": int(y)}

    def move_mouse(self, x:int, y:int):
        """将鼠标移动到指定位置。"""
        self.mouse.move(x, y)

    def click_mouse(self, button: str):
        """点击鼠标按钮。"""
        if button == "left":
            self.mouse.click(Button.left)
        elif button == "right":
            self.mouse.click(Button.right)
        elif button == "middle":
            self.mouse.click(Button.middle)
        else:
            raise ValueError("Unsupported button type. Use 'left', 'right', or 'middle'.")