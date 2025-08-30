# 1. Import the necessary libraries
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import torch
from model import SimpleClosestValue, get_gravity, deg_to_rad

# 2. Read the CSV file into a Pandas DataFrame
# A DataFrame is like a table for your data.
try:
    df = pd.read_csv('data_log.csv')
except FileNotFoundError:
    print("Error: 'data_log.csv' not found. Make sure the file is in the same directory as your script.")
    exit()


# 3. Display the first few rows of the data to check it
print("--- Data from CSV file ---")
print(df)
print("\n")

condition = df['alpha'] > 180
df.loc[condition, 'alpha'] = df.loc[condition, 'alpha'] - 360

# 4. Get the specific column you want to plot
# You can access a column by its name, like a dictionary key.
x_column = df['x']
y_column = df['y']
z_column = df['z']
a_column = df['alpha']
b_column = df['beta']
c_column = df['gamma']

# 5. Plot the column
print("--- Plotting the 'x' column ---")

# Use the .plot() method directly on the column data
# x_column.plot(kind='line', marker='o', linestyle='-')
# y_column.plot(kind='line', marker='x', linestyle='-')
# z_column.plot(kind='line', marker='.', linestyle='-')


# 计算滑动平均并添加到DataFrame
magnitude = np.sqrt(x_column**2 + y_column**2 + z_column**2)
window = 500  # 窗口大小
magnitude_smooth = magnitude.rolling(window=window).mean()
# 绘制原始数据和平滑线
# magnitude.plot(kind='line', marker='*', linestyle='--', color='red', label='Original')
magnitude_smooth.plot(color='blue', label=f'MA({window})')

g_vector = [torch.tensor(g) for g in zip(x_column, y_column, z_column)]
print(g_vector)
g_vector_from_gyro = [get_gravity(deg_to_rad(a),deg_to_rad(b),deg_to_rad(c))  for a,b,c in zip(a_column, b_column, c_column)]
print(g_vector_from_gyro)


x_a = []
x_g = []
x_d = []
y_a = []
y_g = []
y_d = []
z_a = []
z_g = []
z_d = []
for a, g in zip(g_vector, g_vector_from_gyro):
    d = a - g
    x_a.append(a[0])
    x_g.append(g[0])
    x_d.append(d[0])
    y_a.append(a[1])
    y_g.append(g[1])
    y_d.append(d[1])
    z_a.append(a[2])
    z_g.append(g[2])
    z_d.append(d[2])

# 假设x_d, y_d, z_d是你的数据序列
# 将它们转换为Pandas Series对象
x_series = pd.Series(x_d)
y_series = pd.Series(y_d)
z_series = pd.Series(z_d)

# 设置窗口大小
window_size = 500  # 可根据需要调整

# 计算滑动窗口平均值
x_smooth = x_series.rolling(window=window_size).mean()
y_smooth = y_series.rolling(window=window_size).mean()
z_smooth = z_series.rolling(window=window_size).mean()

plt.plot(x_smooth, 'r--', label=f'x_d (滑动平均, window={window_size})', linewidth=2)
plt.plot(y_smooth, 'g--', label=f'y_d (滑动平均, window={window_size})', linewidth=2)
plt.plot(z_smooth, 'b--', label=f'z_d (滑动平均, window={window_size})', linewidth=2)
# # 创建图形
# plt.plot(x_d)
# plt.plot(y_d)
# plt.plot(z_d)

# 6. Add titles and labels to make the plot clear
plt.ylabel('ms-2')         # Label for the y-axis
plt.xlabel('time')              # Label for the x-axis
plt.grid(True)                         # Add a grid for better readability


# 7. Show the plot
plt.show()