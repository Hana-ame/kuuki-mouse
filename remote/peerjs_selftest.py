"""PeerJS 真机自检: 连**公开 broker** 跑一遍完整链路。

为什么单独一个入口
------------------
``test_remote.py`` 里的 PeerJS 用例用的是内存回环, 不联网 —— 那能证明"我们的代码"
是对的, 但证明不了网络那一层 (broker 注册 / ICE 打洞 / DataChannel 真实吞吐)。
这个脚本就是补那一层的: 两端都真的去连 ``0.peerjs.com``, 在同一个进程里互连。

安全
----
默认用**假屏幕 + 假输入**: 截图是内存里画的噪点图, 鼠标键盘是记账的假对象,
不会动你正在用的光标。所以可以放心在干活的那台机器上跑。

用法::

    python -m remote.peerjs_selftest                 # 随机房间码
    python -m remote.peerjs_selftest --room ABCDE    # 指定房间码
    python -m remote.peerjs_selftest --timeout 60    # 慢网络放宽握手预算

退出码: 0 全通 / 3 broker 注册失败 / 4 连接失败 / 5 未预期异常 / 6 大图字节不一致。

实测参考 (2026-09-20, 本机同机两端, 家庭宽带): 注册约 2s, **握手约 13s**, 之后每个
op 几十毫秒。握手这一步就是控制端给 peerjs 默认 30s 超时的原因 (ws 那套 10s 不够)。
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time
from typing import Optional

from .dummy import fake_controller
from .peerjs_client import PeerJsClient
from .peerjs_server import PeerJsServer, gen_room_code
from .screen import ScreenCapture
from .service import RemoteService

__all__ = ["main"]


def _noisy_screen(width: int = 320, height: int = 200) -> ScreenCapture:
    """噪点假屏幕: PNG 压不动, 一帧上百 KB, 用来逼出应用层分块。"""
    from PIL import Image

    rng = random.Random(11)
    raw = bytes(rng.randrange(256) for _ in range(width * height * 3))
    screen = ScreenCapture()
    screen.grab_image = lambda *a, **k: Image.frombytes(  # type: ignore[method-assign]
        "RGB", (width, height), raw
    )
    screen.screen_size = lambda *a, **k: (width, height)  # type: ignore[method-assign]
    return screen


class _Steps:
    def __init__(self) -> None:
        self.t0 = time.time()

    def __call__(self, text: str) -> None:
        print(f"[{time.time() - self.t0:6.1f}s] {text}", flush=True)


async def run(
    room: Optional[str] = None,
    token: Optional[str] = None,
    timeout: float = 40.0,
    big: bool = True,
) -> int:
    step = _Steps()
    service = RemoteService(screen=_noisy_screen(), controller=fake_controller()[0])
    server = PeerJsServer(service, room=room, token=token)
    step(f"服务端 peer_id={server.peer_id}")

    await server.start()
    step("start() 返回, 等 broker 确认注册 …")
    if not await server.wait_ready(timeout=min(timeout, 25.0)):
        step("注册失败: 0.peerjs.com:443 不可达, 或房间码已被占用 (换 --room 试试)")
        return 3

    client = PeerJsClient(server.peer_id, token=token, timeout=timeout)
    step("客户端连接中 (broker 注册 + ICE 打洞) …")
    try:
        await client.connect()
    except Exception as exc:  # noqa: BLE001
        step(f"连接失败: {exc.__class__.__name__}: {exc}")
        return 4
    step("连接建立")

    try:
        ping = await client.call("ping")
        step(f"ping   -> pong={ping.get('pong')}")
        info = await client.call("info")
        step(f"info   -> hostname={info.get('hostname')} python={info.get('python')}")
        step(f"pos    -> {await client.call('mouse.position')}")

        small = await client.screenshot({"format": "png"})
        step(f"shot   -> {len(small)}B 是PNG={small[:8] == bytes([137, 80, 78, 71, 13, 10, 26, 10])}")

        if big:
            expected, _ = service.capture({"format": "png"})
            step(f"大图   -> 服务端 {len(expected.data)}B (超过阈值, 会走分块)")
            payload = await client.screenshot({"format": "png"})
            same = payload == expected.data
            step(f"大图   -> 客户端 {len(payload)}B 逐字节相同={same}")
            if not same:
                return 6

        batch = await client.batch([{"op": "ping"}, {"op": "screen.size"}])
        step(f"batch  -> {[item['ok'] for item in batch['results']]}")
    finally:
        await client.close()
        await server.close()
    step("关闭完成")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m remote.peerjs_selftest",
        description="PeerJS 真机自检 (连公开 broker; 用假屏幕假输入, 不动真实光标)",
    )
    parser.add_argument("--room", default=None, help=f"房间码 (默认随机, 形如 {gen_room_code()})")
    parser.add_argument("--token", default=None, help="两端都用这个 token")
    parser.add_argument("--timeout", type=float, default=40.0, help="握手与单 op 的超时秒数")
    parser.add_argument("--no-big", action="store_true", help="跳过大截图 (不测分块)")
    args = parser.parse_args(argv)

    try:
        code = asyncio.run(run(args.room, args.token, args.timeout, not args.no_big))
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"失败: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 5
    print(f"EXIT={code}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
