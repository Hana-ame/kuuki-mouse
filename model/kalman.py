import torch
import matplotlib.pyplot as plt

class VelocityKalmanFilterTorch:
    """
    A Kalman filter implemented in PyTorch to estimate 1D velocity from acceleration.

    This model treats acceleration as a control input and estimates the state of
    position and velocity. Since there is no independent measurement source (like GPS)
    to update the state, this acts primarily as a predictor/smoother.
    """
    def __init__(self, dt, process_noise_std=0.1, initial_uncertainty=1.0, device='cpu'):
        """
        Initializes the Kalman Filter.

        :param dt: float, time step in seconds
        :param process_noise_std: float, standard deviation of the process noise.
                                      This is a key tuning parameter representing model uncertainty.
        :param initial_uncertainty: float, a scalar for the initial state uncertainty.
        :param device: str, 'cpu' or 'cuda', to specify the computation device.
        """
        self.dt = dt
        self.device = device
        self.dtype = torch.float32

        # State vector [position, velocity]'
        self.x = torch.zeros((2, 1), dtype=self.dtype, device=self.device)

        # State transition matrix A
        self.A = torch.tensor([[1.0, self.dt],
                               [0.0, 1.0]], dtype=self.dtype, device=self.device)

        # Control input matrix B
        self.B = torch.tensor([[0.5 * self.dt**2],
                               [self.dt]], dtype=self.dtype, device=self.device)

        # Process noise covariance matrix Q
        q_val = process_noise_std**2
        self.Q = torch.tensor([[(self.dt**4)/4, (self.dt**3)/2],
                               [(self.dt**3)/2,  self.dt**2]], dtype=self.dtype, device=self.device) * q_val

        # State covariance matrix P (our uncertainty about the state estimate)
        self.P = torch.eye(2, dtype=self.dtype, device=self.device) * initial_uncertainty

    def update(self, acceleration):
        """
        Processes a new acceleration measurement and updates the state.

        :param acceleration: float or tensor, the acceleration measurement at the current time step.
        :return: tuple (tensor, tensor), returns the estimated (position, velocity).
        """
        u = torch.tensor([[acceleration]], dtype=self.dtype, device=self.device)

        # --- Prediction Step ---
        self.x = self.A @ self.x + self.B @ u
        self.P = self.A @ self.P @ self.A.T + self.Q

        position = self.x[0, 0]
        velocity = self.x[1, 0]
        return position, velocity

# --- Main execution script ---
if __name__ == "__main__":
    # --- 1. Simulation Setup ---
    dt = 0.1
    total_time = 20.0
    time_steps = int(total_time / dt)
    t = torch.linspace(0, total_time, time_steps)

    # --- 2. Generate Ideal Motion Data (Ground Truth) ---
    accel_true = torch.zeros(time_steps, dtype=torch.float32)
    accel_true[t < 5] = 0.5
    accel_true[t > 15] = -0.5
    velo_true = torch.cumsum(accel_true * dt, dim=0)

    # --- 3. Simulate Noisy Sensor Measurements ---
    noise_std_dev = 0.15
    accel_measured = accel_true + torch.randn(time_steps) * noise_std_dev

    # --- 4. Initialize Filter and Storage Lists ---
    kf = VelocityKalmanFilterTorch(dt=dt, process_noise_std=0.5)
    pos_kalman_list = []
    velo_kalman_list = []
    velo_simple_integration = torch.cumsum(accel_measured * dt, dim=0)

    # --- 5. Loop and Process Data ---
    for i in range(time_steps):
        pos, vel = kf.update(accel_measured[i].item())
        pos_kalman_list.append(pos)
        velo_kalman_list.append(vel)
    
    pos_kalman = torch.tensor(pos_kalman_list)
    velo_kalman = torch.tensor(velo_kalman_list)

    # --- 6. Visualize Results ---
    # Convert tensors to NumPy arrays for plotting
    t_np = t.cpu().numpy()
    accel_measured_np = accel_measured.cpu().numpy()
    accel_true_np = accel_true.cpu().numpy()
    velo_true_np = velo_true.cpu().numpy()
    velo_simple_integration_np = velo_simple_integration.cpu().numpy()
    velo_kalman_np = velo_kalman.cpu().numpy()
    
    plt.style.use('seaborn-v0_8-whitegrid')
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 10), sharex=True)

    # Plot Acceleration (with English labels)
    ax1.plot(t_np, accel_measured_np, label='Measured Acceleration (with Noise)', color='orange', alpha=0.6)
    ax1.plot(t_np, accel_true_np, label='True Acceleration', color='red', linewidth=2)
    ax1.set_ylabel('Acceleration (m/s²)')
    ax1.set_title('Acceleration vs. Time (PyTorch Implementation)')
    ax1.legend()
    ax1.grid(True)

    # Plot Velocity (with English labels)
    ax2.plot(t_np, velo_true_np, label='True Velocity', color='red', linewidth=3)
    ax2.plot(t_np, velo_simple_integration_np, label='Velocity from Simple Integration', linestyle='--', color='purple', linewidth=5)
    ax2.plot(t_np, velo_kalman_np, label='Velocity from Kalman Filter', color='green', linewidth=2)
    ax2.set_ylabel('Velocity (m/s)')
    ax2.set_xlabel('Time (s)')
    ax2.set_title('Comparison of Velocity Estimation from Acceleration')
    ax2.legend()
    ax2.grid(True)

    plt.tight_layout()
    plt.show()
