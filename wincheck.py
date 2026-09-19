"""Windows 侧自检: 验证在 Windows 上跑 remote 服务的三种传输。

放在仓库里用 `.venv-win\\Scripts\\python.exe wincheck.py` 跑。
不移动鼠标、不按键, 只读状态 + 截屏。
"""

import asyncio
import sys

sys.path.insert(0, ".")

from remote.client import GrpcClient, WsClient  # noqa: E402
from remote.service import RemoteService  # noqa: E402


def check_service_direct():
    print("=== 直接调 RemoteService (不经传输) ===")
    service = RemoteService()
    print("  screen.size ->", service.handle("screen.size", {}))
    print("  mouse.pos   ->", service.handle("mouse.position", {}))
    keys = service.handle("keyboard.check", {"keys": ["a", "中", "f13"]})["keys"]
    print("  key.check   ->", [(k["key"], k["supported"]) for k in keys])
    capture, data = service.capture({"format": "png", "max_width": 800})
    print(f"  screenshot  -> {capture.width}x{capture.height} {len(data)}B backend={capture.backend}")
    print("  PNG magic   ->", data[:4] == b"\x89PNG")
    return data


async def check_ws():
    print("=== WebSocket (127.0.0.1:8766) ===")
    async with WsClient("ws://127.0.0.1:8766/", timeout=20) as client:
        print("  ping     ->", (await client.call("ping"))["pong"])
        print("  screen   ->", await client.call("screen.size"))
        header, png = await client.screenshot({"format": "jpeg", "quality": 60, "max_width": 800})
        print(f"  shot     -> {header['width']}x{header['height']} {len(png)}B backend={header['backend']}")
        print("  mouse    ->", await client.call("mouse.position"))


def check_grpc():
    print("=== gRPC (127.0.0.1:50052) ===")
    with GrpcClient("127.0.0.1:50052", timeout=20) as client:
        print("  ping     ->", client.call("ping")["ok"])
        info = client.call("info")
        print("  os       ->", info["os"], "| python", info["python"])
        data = client.screenshot({"format": "png", "max_width": 400})
        magic = data[:4] == b"\x89PNG"
        print(f"  shot     -> {len(data)}B magic={magic}")
        seen = []
        frames = client.stream({"fps": 4, "max_width": 300}, lambda img: seen.append(img), count=2)
        print(f"  stream   -> {frames} 帧")


if __name__ == "__main__":
    check_service_direct()
    asyncio.run(check_ws())
    check_grpc()
    print("\nALL OK")
