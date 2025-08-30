import asyncio
import time
# from grpc_remote_control.grpc_controller import get_mouse_position, move_mouse, click_mouse

from model import get_gravity_float, to_tensor

# from server import main

class App:
    def __init__(self):
        self._last_data_timestamp = 0
        self._x = 0 # previous status
        self._y = 0 # previous status
        self._z = 0 # previous status
        self._alpha = 0 # previous status
        self._beta = 0 # previous status
        self._gamma = 0 # previous status
        self._last_acc_timestamp = 0
        
        self.v_x = 0 # 横轴上的速度
        self.v_y = 0 # 纵轴上的速度
        
    def update_data(self, x:float,y:float,z:float,alpha:float,beta:float,gamma:float):
        data_timestamp = time.time()
        a = to_tensor(x,y,z)
        g = get_gravity_float(beta, gamma)
        a_dash = a-g
        print(a_dash, data_timestamp - self._last_data_timestamp)
        # 需要：
        # 1.忽略<0.15的部分，这个是精度误差。
        # 2.测得的是这段时间内的平均加速度，因此需要乘以时间间隔得到速度。
        # 3.速度应该有衰减系数。
        # 善后处理
        self._x = x
        self._y = y
        self._z = z
        self._alpha = alpha
        self._beta = beta
        self._gamma = gamma
        self._last_data_timestamp = data_timestamp
    def update_mouse(self):
        pass

app = App()

def data_loop(x:float,y:float,z:float,alpha:float,beta:float,gamma:float):
    print(f"Received data: x={x:02f}, y={y:02f}, z={z:02f}, alpha={alpha:02f}, beta={beta:02f}, gamma={gamma:02f}")

def get_data(x:float,y:float,z:float,alpha:float,beta:float,gamma:float):
    # data_loop(x,y,z,alpha,beta,gamma)
    app.update_data(x,y,z,alpha,beta,gamma)
    



# if __name__ == "__main__":
#     try:
#         asyncio.run(main())
#     except KeyboardInterrupt:
#         print("\n服务器正在关闭。")