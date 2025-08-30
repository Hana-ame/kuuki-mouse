import torch
import torch.nn as nn

class BatchedClosestValue(nn.Module):
    def __init__(self, num_features: int, batch_size: int, initial_value: float = 0.0):
        super(BatchedClosestValue, self).__init__()
        # 假设输入形状为 (batch_size, num_features)
        # prev_output 的形状为 (batch_size, 1)，为每个样本保存一个状态
        initial_tensor = torch.full((batch_size, 1), initial_value)
        self.register_buffer('prev_output', initial_tensor)

    def forward(self, input: torch.Tensor):
        # input 形状: (batch_size, num_features)
        # self.prev_output 形状: (batch_size, 1)
        # 广播机制会自动处理减法
        diff = torch.abs(input - self.prev_output)

        # 沿特征维度(dim=1)寻找最小值的索引
        # min_vals, min_indices = torch.min(diff, dim=1)
        min_indices = torch.argmin(diff, dim=1, keepdim=True) # shape: (batch_size, 1)

        # 使用 gather 从输入中高效地选取每个样本的最接近值
        # gather需要索引和输入有相同的维度数
        closest_vals = torch.gather(input, dim=1, index=min_indices) # shape: (batch_size, 1)

        # 更新状态
        self.prev_output = closest_vals.detach()

        # 返回一个列向量，每个元素是对应输入样本的最接近值
        return closest_vals

class SimpleClosestValue(nn.Module):
    def __init__(self, initial_value: float = 0.0):
        super(SimpleClosestValue, self).__init__()
        # 初始状态是一个标量
        self.register_buffer('prev_output', torch.tensor([initial_value]))

    def forward(self, input: torch.Tensor):
        # 确保输入是1D
        if input.dim() != 1:
            raise ValueError(f"Expected 1D tensor, but got {input.dim()}D tensor.")
            
        diff = torch.abs(input - self.prev_output)
        min_idx = torch.argmin(diff) # 在1D上是安全的
        
        # .unsqueeze(0) 确保输出和prev_output形状一致
        closest_val = input[min_idx].unsqueeze(0) 
        
        # 更新状态
        self.prev_output = closest_val.detach()
        
        return closest_val
    
if __name__ == '__main__':
    # 使用示例
    model = BatchedClosestValue(num_features=5, batch_size=2, initial_value=10)
    input_data = torch.tensor([
        [1., 12., 20., 8., 30.],
        [5., 15., 9., 25., 35.]
    ])
    output = model(input_data)
    # 第一次：
    # 样本1: 12最接近10, prev_output[0]变为12
    # 样本2: 9最接近10, prev_output[1]变为9
    print("第一次输出:\n", output)
    print("更新后的状态:\n", model.prev_output)

    input_data2 = torch.tensor([
        [13., 2., 18., 7., 32.],
        [4., 16., 8., 22., 31.]
    ])
    output2 = model(input_data2)
    # 第二次：
    # 样本1: 13最接近12, prev_output[0]变为13
    # 样本2: 8最接近9, prev_output[1]变为8
    print("\n第二次输出:\n", output2)
    print("更新后的状态:\n", model.prev_output)