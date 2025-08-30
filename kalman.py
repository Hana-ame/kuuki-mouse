import numpy as np
import matplotlib.pyplot as plt

class KalmanFilter1DVelocity:
    """
    一个用于从加速度估计速度的一维卡尔曼滤波器。
    
    状态 x: [velocity]
    输入 u: [acceleration]
    测量 z: [noisy velocity]
    """
    def __init__(self, dt: float, q_noise: float, r_noise: float, initial_velocity: float = 0., initial_p: float = 1.0):
        """
        初始化滤波器。

        Args:
            dt (float): 时间步长 (Δt)。
            q_noise (float): 过程噪声的方差 Q。代表模型预测的不确定性。
            r_noise (float): 测量噪声的方差 R。代表速度传感器的不确定性。
            initial_velocity (float): 初始速度估计值。
            initial_p (float): 初始误差协方差 P。代表初始估计的不确定性。
        """
        # 时间步长
        self.dt = dt
        
        # 定义状态转移矩阵 F 和控制输入矩阵 B
        self.F = np.array([[1]])
        self.B = np.array([[dt]])
        
        # 定义测量矩阵 H
        self.H = np.array([[1]])
        
        # 定义过程噪声协方差 Q
        self.Q = np.array([[q_noise]])
        
        # 定义测量噪声协方差 R
        self.R = np.array([[r_noise]])
        
        # 初始化状态向量 x 和误差协方差矩阵 P
        self.x = np.array([[initial_velocity]]) # 状态 [velocity]
        self.P = np.array([[initial_p]])       # 协方差

    def predict(self, acceleration: float):
        """
        执行预测步骤。
        
        Args:
            acceleration (float): 当前时刻的加速度输入 u_k。
        """
        u = np.array([[acceleration]])
        
        # 预测状态
        self.x = self.F @ self.x + self.B @ u
        
        # 预测误差协方差
        self.P = self.F @ self.P @ self.F.T + self.Q
        
    def update(self, measured_velocity: float):
        """
        执行更新（校正）步骤。

        Args:
            measured_velocity (float): 当前时刻的速度测量值 z_k。
        
        Returns:
            float: 更新后的速度估计值。
        """
        z = np.array([[measured_velocity]])
        
        # 计算卡尔曼增益 K
        K_denominator = self.H @ self.P @ self.H.T + self.R
        K = self.P @ self.H.T @ np.linalg.inv(K_denominator)
        
        # 更新状态估计
        self.x = self.x + K @ (z - self.H @ self.x)
        
        # 更新误差协方差
        I = np.eye(self.P.shape[0])
        self.P = (I - K @ self.H) @ self.P
        
        return self.x[0, 0]

# --- 仿真与测试 ---

# 1. 仿真参数
dt = 0.1  # 时间步长为 0.1 秒
total_time = 20.0 # 总仿真时间
timesteps = np.arange(0, total_time, dt)
n_steps = len(timesteps)

# 2. 生成“真实”数据（我们假装不知道）
true_acceleration = np.zeros(n_steps)
# 在 5s 到 15s 之间施加一个恒定的加速度
true_acceleration[int(5/dt):int(15/dt)] = 2.0 

true_velocity = np.zeros(n_steps)
for k in range(1, n_steps):
    true_velocity[k] = true_velocity[k-1] + true_acceleration[k-1] * dt

# 3. 生成带噪声的“传感器”数据
accel_noise_std = 0.2  # 加速度计的噪声标准差
velocity_sensor_noise_std = 1.0 # 速度传感器的噪声标准差

noisy_acceleration = true_acceleration + np.random.normal(0, accel_noise_std, n_steps)
noisy_velocity_measurement = true_velocity + np.random.normal(0, velocity_sensor_noise_std, n_steps)


# 4. 初始化并运行卡尔曼滤波器
# Q: 过程噪声，我们相信物理模型，所以给一个较小的值
# R: 测量噪声，速度计噪声标准差的平方
q_val = 0.01
r_val = velocity_sensor_noise_std**2

kf = KalmanFilter1DVelocity(dt, q_noise=q_val, r_noise=r_val)
filtered_velocity = np.zeros(n_steps)

for k in range(n_steps):
    # 获取传感器读数
    current_accel = noisy_acceleration[k]
    current_measured_vel = noisy_velocity_measurement[k]
    
    # 卡尔曼滤波器步骤
    kf.predict(acceleration=current_accel)
    filtered_velocity[k] = kf.update(measured_velocity=current_measured_vel)
    
# 5. 结果可视化
plt.style.use('seaborn-v0_8-whitegrid')
plt.figure(figsize=(14, 8))
plt.plot(timesteps, true_velocity, 'g-', label='真实速度 (Ground Truth)', linewidth=2.5)
plt.plot(timesteps, noisy_velocity_measurement, 'r.', markersize=4, alpha=0.6, label='噪声测量速度 (Sensor)')
plt.plot(timesteps, filtered_velocity, 'b-', label='卡尔曼滤波后速度 (Filtered)', linewidth=2)
plt.title('卡尔曼滤波器: 从加速度估计速度', fontsize=16)
plt.xlabel('时间 (s)', fontsize=12)
plt.ylabel('速度 (m/s)', fontsize=12)
plt.legend(fontsize=12)
plt.grid(True)
plt.show()