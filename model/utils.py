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

if __name__ == '__main__':
    print(get_gravity(torch.Tensor([1.3/180*torch.pi]),torch.Tensor([6.1/180*torch.pi])))