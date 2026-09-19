"""kuuki-mouse 的远程控制扩展 (鼠标/键盘控制 + 截屏), 通过 WebSocket / gRPC 暴露。

模块
----
``screen``    截屏后端 (mss / Pillow / ffmpeg x11grab) 与裁剪、缩放、编码
``input``     鼠标键盘控制 (扩展项目根的 ``controller.PynputMouseController``)
``service``   操作注册表 (``mouse.move`` / ``keyboard.type`` / ``screen.screenshot`` …)
``ws_server`` WebSocket 服务端 (JSON 请求 + 二进制截屏帧 + 流式推帧)
``grpc_server`` gRPC 服务端 (protobuf 强类型 + 服务端流式截屏)
``client``    命令行客户端 (调试/冒烟用)

快速开始::

    python -m remote                 # 同时起 WS(8765) 与 gRPC(50051), 只绑 127.0.0.1
    python -m remote --no-grpc       # 只要 WebSocket
    python -m remote.client ws --op ping
    python -m remote.client ws --screenshot out.png
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
