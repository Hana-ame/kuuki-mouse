import numpy as np
import torch

# 理论上，应该有一个办法通过

def deg_to_rad(d: float)-> torch.Tensor:
    return torch.deg2rad(torch.Tensor([d]))

# 通过beta和gamma两个角度读数得到重力加速度在各个轴上的分量
# beta, gamma in RAD!!
def get_gravity(beta: torch.Tensor, gamma: torch.Tensor, g:float=9.85) -> torch.Tensor:
    a_z= g*torch.cos(beta)*torch.cos(gamma)
    if not beta <= torch.pi/2 and beta >= -torch.pi/2:
        a_z = -a_z
    a_y = g * torch.sin(beta)
    a_x = -g * torch.cos(beta)*torch.sin(gamma)
    return torch.Tensor([a_x, a_y, a_z])

# def get_gravity_float(beta: float, gamma: float):
#     return get_gravity(torch.Tensor(beta),torch.Tensor(gamma))

def get_gravity_float(beta: float, gamma: float):
    return get_gravity(deg_to_rad(beta),deg_to_rad(gamma))

def to_tensor(x:float,y:float,z:float):
    return torch.Tensor([x,y,z])


def process_tensor(input_tensor: torch.Tensor, deviation: float = .15) -> torch.Tensor:
    # 创建布尔掩码
    mask_gt = input_tensor > deviation    # 大于1.5的掩码
    mask_lt = input_tensor < -deviation   # 小于-1.5的掩码
    mask_mid = ~(mask_gt | mask_lt) # 中间区域的掩码（既不是大于1.5也不是小于-1.5）

    # 初始化一个结果张量，先复制原值
    result = input_tensor.clone()

    # 应用条件操作
    result[mask_gt] = input_tensor[mask_gt] - deviation  # 大于1.5的减去1.5
    result[mask_lt] = input_tensor[mask_lt] + deviation  # 小于-1.5的加上1.5
    result[mask_mid] = 0.0                         # 中间的设为0

    return result


if __name__ == '__main__':
    print(get_gravity(torch.Tensor([1.3/180*torch.pi]),torch.Tensor([6.1/180*torch.pi])))