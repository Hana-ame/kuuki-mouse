# 控制端不挑平台

只有**受控端**限定 Windows。控制端（发起动作的一侧）可以在任何有 Python 的机器上：

- `remote/client.py` —— 单机调试级 CLI
- `remote/ctl.py` —— 多机编排级 CLI
- `WsClient` / `GrpcClient` / `PeerJsClient` —— 可编程类

所以看到「多同治阘/手机/WSL 能跑吗」这类问题时，答案是「能，只要它是控制端」。

一个常见误解：WSL 里能跑 `python -m remote` 吗？——不能，且它会主动拒绝退出。
想从 WSL 控制 Windows 桌面，正确做法是让 Windows 起受控端、WSL 起客户端去连。
