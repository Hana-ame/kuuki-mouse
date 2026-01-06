from pynput.mouse import Controller, Button
import threading
import time

def click_after_delay():
    # 创建鼠标控制器
    mouse = Controller()
    # 按下并释放左键
    mouse.click(Button.left)

# 启动定时任务
thread = threading.Timer(3600*4, click_after_delay)
thread.start()

print("4小时后将执行鼠标左键点击")