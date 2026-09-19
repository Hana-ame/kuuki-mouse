"""kuuki-mouse 的远程控制扩展 (鼠标/键盘控制 + 截屏), 通过 WebSocket / gRPC / PeerJS 暴露。

**受控端 (服务端) 只支持 Windows** —— 它指"被操作的那台机器", 非 Windows 会拒绝启动
(见 ``remote/__main__.py`` 的 ``platform_refusal()``)。命令行客户端不挑平台。

模块
----
``screen``    截屏 (只有一个后端: ``PIL.ImageGrab``) 与裁剪、缩放、编码
``input``     鼠标键盘控制 (扩展项目根的 ``controller.PynputMouseController``)
``service``   操作注册表 (``mouse.move`` / ``keyboard.type`` / ``screen.screenshot`` …)
``ws_server`` WebSocket 服务端 (JSON 请求 + 二进制截屏帧 + 流式推帧)
``grpc_server`` gRPC 服务端 (protobuf 强类型 + 服务端流式截屏)
``peerjs_server`` PeerJS 服务端 (公开 broker + 房间码, 自带应用层分块)
``peerjs_client`` / ``client`` PeerJS 客户端与命令行客户端 (调试/冒烟用)
``win``       WSL → Windows 的实验性 PowerShell 桥 (未接入服务端, 见 ``win/host.py``)

快速开始::

    python -m remote                  # 三个传输全开: WS 8765 + gRPC 50051 + PeerJS
    python -m remote --no-peerjs      # 只要本机两个端口
    python -m remote.client ws ping
    python -m remote.client ws screenshot shot.png
"""

from .service import RemoteError, RemoteService, VERSION
from .screen import Capture, Region, ScreenCapture

__all__ = [
    "RemoteError",
    "RemoteService",
    "VERSION",
    "Capture",
    "Region",
    "ScreenCapture",
]
