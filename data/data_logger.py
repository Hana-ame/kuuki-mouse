# by gemini
# 得到
import csv
import os
from datetime import datetime

# 定义CSV文件的名称和表头
CSV_FILENAME = "data_log.csv"
CSV_HEADERS = ['timestamp', 'x', 'y', 'z', 'alpha', 'beta', 'gamma']

def get_data(x: float, y: float, z: float, alpha: float, beta: float, gamma: float):
    """
    接收6个浮点数，将它们连同当前时间戳一起记录到CSV文件中。
    """
    # 1. 打印接收到的数据 (按原样保留)
    # print(f"Received data: x={x}, y={y}, z={z}, alpha={alpha}, beta={beta}, gamma={gamma}")

    # 2. 获取当前时间戳，并格式化为字符串
    # 格式 "年-月-日 时:分:秒" (例如: "2023-10-27 10:30:55")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 3. 准备要写入的一行数据，并在此时进行四舍五入处理
    data_row = {
        'timestamp': timestamp,
        'x': round(x, 1),       # <-- 主要改动在这里
        'y': round(y, 1),       # <-- 主要改动在这里
        'z': round(z, 1),       # <-- 主要改动在这里
        'alpha': round(alpha, 1), # <-- 主要改动在这里
        'beta': round(beta, 1),  # <-- 主要改动在这里
        'gamma': round(gamma, 1)  # <-- 主要改动在这里
    }
    # 4. 检查文件是否存在，以确定是否需要写入表头
    file_exists = os.path.exists(CSV_FILENAME)

    # 5. 使用 'a' (append) 模式打开文件，如果文件不存在则会创建
    # newline='' 是为了防止在Windows下写入时出现多余的空行
    try:
        with open(CSV_FILENAME, mode='a', newline='', encoding='utf-8') as csvfile:
            # 使用 DictWriter 可以方便地通过字典写入数据
            writer = csv.DictWriter(csvfile, fieldnames=CSV_HEADERS)

            # 如果文件是新创建的，就写入表头
            if not file_exists:
                writer.writeheader()

            # 写入数据行
            writer.writerow(data_row)
        
        # print(f"Data successfully saved to {CSV_FILENAME}")

    except IOError as e:
        print(f"Error writing to file {CSV_FILENAME}: {e}")


# --- 主程序：如何使用这个函数 ---
if __name__ == "__main__":
    # 第一次调用
    print("--- 记录第一组数据 ---")
    get_data(1.1, 2.2, 3.3, 10.0, 20.0, 30.0)

    print("\n" + "="*30 + "\n")

    # 模拟过了一段时间后，记录另一组数据
    import time
    time.sleep(2) # 暂停2秒，让时间戳有明显变化

    # 第二次调用
    print("--- 记录第二组数据 ---")
    get_data(4.4, 5.5, 6.6, 45.5, 55.5, 65.5)