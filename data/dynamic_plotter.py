import time
import random
import threading
from collections import deque
from datetime import datetime

import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation

# --- 1. 全局配置和数据缓存 ---

CACHE_SIZE = 1000  # 缓存的数据点数量

# 使用 collections.deque 并设置 maxlen，它是一个固定大小的队列
# 当新数据添加进来且队列已满时，最老的数据会自动被丢弃。
# 这是实现“缓存1000个点”最有效的方式。
timestamps = deque(maxlen=CACHE_SIZE)
data_x = deque(maxlen=CACHE_SIZE)
data_y = deque(maxlen=CACHE_SIZE)
data_z = deque(maxlen=CACHE_SIZE)
data_alpha = deque(maxlen=CACHE_SIZE)
data_beta = deque(maxlen=CACHE_SIZE)
data_gamma = deque(maxlen=CACHE_SIZE)

# 创建一个线程锁，以确保在多线程环境下数据写入和读取的安全性
# 当绘图函数正在读取数据时，数据源线程不应修改数据，反之亦然。
data_lock = threading.Lock()


# --- 2. 动态数据源 (核心函数和模拟器) ---

def get_data(x: float, y: float, z: float, alpha: float, beta: float, gamma: float):
    """
    这个函数是数据的入口点。
    它接收数据，并将其安全地添加到我们的缓存队列中。
    """
    with data_lock:  # 获取锁，保证数据同步
        timestamps.append(datetime.now())
        data_x.append(x)
        data_y.append(y)
        data_z.append(z)
        data_alpha.append(alpha)
        data_beta.append(beta)
        data_gamma.append(gamma)
    
    # 为了在终端看到反馈，我们打印一下接收到的数据
    # 在实际应用中，你可能会移除这行代码
    print(f"Received: x={x:.2f}, y={y:.2f}, z={z:.2f} | alpha={alpha:.2f}, beta={beta:.2f}, gamma={gamma:.2f}")


def data_source_simulator():
    """
    这是一个模拟器，它会持续生成随机数据并调用 get_data。
    它在一个单独的线程中运行，以不阻塞主程序的图形界面。
    """
    while True:
        # 生成一些随机波动的数据
        x = 10 + (random.random() - 0.5) * 2
        y = 12 + (random.random() - 0.5) * 3
        z = 15 + (random.random() - 0.5) * 1
        alpha = 90 + (random.random() - 0.5) * 10
        beta = 45 + (random.random() - 0.5) * 5
        gamma = 0 + (random.random() - 0.5) * 20
        
        get_data(x, y, z, alpha, beta, gamma)
        
        # 控制数据生成速率，例如每 100 毫秒生成一组新数据
        time.sleep(0.1)


# --- 3. 绘图部分 ---

# 创建图表和两个子图
fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True)

# 初始化第一个子图 (xyz) 的线条
line_x, = ax1.plot([], [], 'r-', label='x')
line_y, = ax1.plot([], [], 'g-', label='y')
line_z, = ax1.plot([], [], 'b-', label='z')
ax1.set_title('Positional Data (x, y, z)')
ax1.set_ylabel('Position Value')
ax1.legend()
ax1.grid(True)

# 初始化第二个子图 (abc) 的线条
line_alpha, = ax2.plot([], [], 'c-', label='alpha')
line_beta, = ax2.plot([], [], 'm-', label='beta')
line_gamma, = ax2.plot([], [], 'y-', label='gamma')
ax2.set_title('Rotational Data (alpha, beta, gamma)')
ax2.set_ylabel('Angle Value')
ax2.set_xlabel('Time')
ax2.legend()
ax2.grid(True)


def update_plot(frame):
    """
    这是 FuncAnimation 每次更新时会调用的函数。
    它负责从缓存中获取最新数据并更新图表上的线条。
    """
    with data_lock:  # 获取锁，安全地读取数据
        # 从 deque 复制数据，这样可以尽快释放锁
        current_times = list(timestamps)
        current_x = list(data_x)
        current_y = list(data_y)
        current_z = list(data_z)
        current_alpha = list(data_alpha)
        current_beta = list(data_beta)
        current_gamma = list(data_gamma)

    if not current_times:
        return [] # 如果没有数据，直接返回

    # 更新第一个子图的数据
    line_x.set_data(current_times, current_x)
    line_y.set_data(current_times, current_y)
    line_z.set_data(current_times, current_z)

    # 更新第二个子图的数据
    line_alpha.set_data(current_times, current_alpha)
    line_beta.set_data(current_times, current_beta)
    line_gamma.set_data(current_times, current_gamma)
    
    # 自动调整坐标轴范围
    ax1.relim()
    ax1.autoscale_view()
    ax2.relim()
    ax2.autoscale_view()
    
    # 自动格式化X轴的日期显示
    fig.autofmt_xdate()

    # blit=True 要求返回已更新的艺术家对象列表
    return [line_x, line_y, line_z, line_alpha, line_beta, line_gamma]

async def main():
    # 创建并启动数据模拟器线程
    # 设置为守护线程 (daemon=True)，这样当主程序退出时，该线程也会自动结束
    simulator_thread = threading.Thread(target=data_source_simulator, daemon=True)
    simulator_thread.start()

    # 创建动画
    # interval=200 表示每 200 毫秒更新一次图表
    # blit=True 是一个优化选项，它让动画只重绘发生变化的部分，可以提高刷新速度
    ani = FuncAnimation(fig, update_plot, interval=200, blit=True, cache_frame_data=False)

    # 调整布局
    plt.tight_layout()
    # 显示图表。这会启动 matplotlib 的事件循环，动画会一直运行，直到你关闭窗口。
    plt.show()

    print("Plotting window closed. Program finished.")


# --- 4. 主程序入口 ---
if __name__ == "__main__":
    main()