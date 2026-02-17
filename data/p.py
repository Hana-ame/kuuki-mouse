from pynput.mouse import Controller as MouseController, Button
from pynput.keyboard import Controller as KeyboardController, Key
import time


def immediate_action():

    time.sleep(10)

    # 初始化控制器
    mouse = MouseController()
    keyboard = KeyboardController()

    print("F")
    keyboard.press("f")
    keyboard.release("f")

    # 2. 停留5秒
    time.sleep(5)

    print("W")
    keyboard.press("w")
    keyboard.release("w")

    time.sleep(10)

    # 4. 按下 Ctrl + W
    print("执行 Ctrl + W")
    with keyboard.pressed(Key.ctrl):
        keyboard.press("w")
        keyboard.release("w")

    time.sleep(10)


if __name__ == "__main__":
    # 建议在正式执行前留出10秒准备时间，以便你将鼠标移动到目标位置
    print("程序将在10秒后开始执行，请准备...")
    time.sleep(60 * 15)
    for _ in range(20):
        time.sleep(60 * 10)
        immediate_action()
    print("任务完成")
