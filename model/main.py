import torch
from . import get_gravity

# 如果放在桌子上
# y+ ↑
# y- ↓
# x+ →
# x- ←
def get_acc_on_table(a:torch.Tensor, beta:torch.Tensor, gamma:torch.Tensor):
    g = get_gravity(beta=beta, gamma=gamma)
    a = a - g
    return a
    