"""``python -m remote`` —— 默认同时启动 WebSocket、gRPC 与 PeerJS 三个传输。

默认**只绑 127.0.0.1**; 要给别的机器用必须显式 ``--allow-remote`` 且设置 token。
PeerJS 不需要本地端口 —— 它注册到公开 broker, 靠房间码配对。

    python -m remote                          # WS 8765 + gRPC 50051 + PeerJS (默认全开)
    python -m remote --no-peerjs              # 只开 WS + gRPC 两个本地端口
    python -m remote --no-grpc --no-peerjs    # 只开 WebSocket
    python -m remote --no-ws --no-grpc        # 只开 PeerJS (靠房间码配对, 不需要端口)
    python -m remote --ws-port 9000 --grpc-port 9001
    python -m remote --token secret           # 三个传输都要求 token
    python -m remote --allow-remote --token secret   # 绑 0.0.0.0 (危险, 必须带 token)
    python -m remote --selftest               # 只做自检: 报告后端 + 抓一帧, 不动鼠标
    python -m remote --selftest --selftest-input     # 额外测一次鼠标移动(会动光标)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

# 允许 `python remote/__main__.py` 直接跑 (此时包上下文为空, 需要把仓库根加进 sys.path)
if __package__ in (None, ""):  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from remote.grpc_server import GrpcServer  # noqa: E402
from remote.peerjs_server import PeerJsServer  # noqa: E402
from remote.service import RemoteError, RemoteService, VERSION  # noqa: E402
from remote.ws_server import WsServer  # noqa: E402

log = logging.getLogger("kuuki.remote")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m remote",
        description="kuuki-mouse 远程控制扩展: 鼠标/键盘控制 + 截屏, 暴露 WebSocket / gRPC / PeerJS 三种传输",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--ws-port", type=int, default=8765, help="WebSocket 端口")
    parser.add_argument("--grpc-port", type=int, default=50051, help="gRPC 端口")
    parser.add_argument("--no-ws", action="store_true", help="不开 WebSocket")
    parser.add_argument("--no-grpc", action="store_true", help="不开 gRPC")
    parser.add_argument(
        "--no-peerjs", action="store_true",
        help="不开 PeerJS (默认开; 走 0.peerjs.com 公开 broker, 无需端口/域名)",
    )
    parser.add_argument("--room", default=None, help="PeerJS 房间码 (默认随机生成)")
    parser.add_argument(
        "--token",
        default=os.environ.get("KUUKI_REMOTE_TOKEN"),
        help="访问令牌 (也可用环境变量 KUUKI_REMOTE_TOKEN)",
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="允许绑非回环地址 (必须同时设置 --token)",
    )
    parser.add_argument("--max-fps", type=float, default=30.0, help="screen.watch 的帧率上限")
    parser.add_argument("--log-level", default="INFO", help="日志级别")
    parser.add_argument("--json", action="store_true", help="启动时以 JSON 打印端点信息")
    parser.add_argument("--selftest", action="store_true", help="自检后退出 (不启动服务)")
    parser.add_argument(
        "--selftest-input",
        action="store_true",
        help="自检时额外移动一次鼠标 (会动到光标, 默认不动)",
    )
    parser.add_argument("--version", action="version", version=f"kuuki remote {VERSION}")
    return parser


def selftest(service: RemoteService, with_input: bool = False) -> int:
    """自检: 报告后端与屏幕、抓一帧, 可选测一次鼠标移动。返回退出码。"""
    ok = True
    print(f"kuuki remote {VERSION} 自检")
    try:
        info = service.handle("info", {})
        print(json.dumps(info, ensure_ascii=False, indent=2))
    except RemoteError as exc:
        print(f"  info 失败: {exc.to_dict()}")
        return 1

    print("\n-- 截屏 --")
    try:
        capture, data = service.capture({"format": "png"})
        print(
            f"  尺寸={capture.width}x{capture.height} "
            f"源尺寸={capture.source_width}x{capture.source_height} "
            f"字节={len(data)} 耗时={capture.duration_ms:.0f}ms"
        )
        png_magic = data[:8] == b"\x89PNG\r\n\x1a\n"
        print(f"  PNG 魔数正确: {png_magic}")
        ok = ok and png_magic and len(data) > 100
    except Exception as exc:  # noqa: BLE001
        print(f"  截屏失败: {exc.__class__.__name__}: {exc}")
        ok = False

    if with_input:
        print("\n-- 输入 (会动光标) --")
        try:
            width, height = service.screen.screen_size()
            before = service.controller.position()
            service.handle("mouse.move", {"x": width // 2, "y": height // 2, "duration": 0.2})
            middle = service.controller.position()
            service.handle("mouse.move", {"x": before[0], "y": before[1], "duration": 0.2})
            after = service.controller.position()
            print(f"  光标 {before} -> 屏幕中心 {middle} -> {after}")
            moved = middle != before
            print(f"  移动生效: {moved}")
            ok = ok and moved
        except Exception as exc:  # noqa: BLE001
            print(f"  输入测试失败: {exc.__class__.__name__}: {exc}")
            ok = False

    print(f"\n自检结果: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


async def run_servers(args: argparse.Namespace) -> int:
    token = args.token
    if args.allow_remote and not token:
        print("拒绝启动: --allow-remote 必须同时提供 --token", file=sys.stderr)
        return 2
    host = args.host
    if args.allow_remote and host == "127.0.0.1":
        host = "0.0.0.0"

    service = RemoteService(token=token)
    ws_server = None
    grpc_server = None
    peerjs_server = None

    if not args.no_ws:
        ws_server = WsServer(service, host=host, port=args.ws_port, token=token, max_fps=args.max_fps)
        await ws_server.start()
    if not args.no_grpc:
        grpc_server = GrpcServer(service, host=host, port=args.grpc_port, token=token)
        grpc_server.start()
    if not args.no_peerjs:
        # PeerJS 不需要本地端口 —— 它注册到公开 broker, 靠房间码配对
        peerjs_server = PeerJsServer(service, room=args.room, token=token)
        await peerjs_server.start()
        await peerjs_server.wait_ready(timeout=20.0)

    endpoints = []
    if ws_server is not None:
        endpoints.append(ws_server.describe())
    if grpc_server is not None:
        endpoints.append(grpc_server.describe())
    if peerjs_server is not None:
        endpoints.append(peerjs_server.describe())

    if args.json:
        print(json.dumps({"version": VERSION, "endpoints": endpoints}, ensure_ascii=False, indent=2))
    else:
        print(f"kuuki remote {VERSION} 已启动")
        for endpoint in endpoints:
            if endpoint["transport"] == "websocket":
                print(f"  WebSocket : {endpoint['url']}")
            elif endpoint["transport"] == "grpc":
                print(f"  gRPC      : {endpoint['address']}  ({endpoint['service']})")
            else:
                state = "已注册" if endpoint["ready"] else "未注册"
                print(f"  PeerJS    : {endpoint['peer_id']}  ({state}, {endpoint['broker']})")
        print(f"  token     : {'已设置' if token else '未设置 (仅回环安全)'}")
        print("  客户端示例: python -m remote.client ws ping")
        sys.stdout.flush()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in ("SIGINT", "SIGTERM"):
        try:
            import signal as _signal

            loop.add_signal_handler(getattr(_signal, sig), stop.set)
        except (NotImplementedError, AttributeError, ValueError):  # pragma: no cover - Windows
            pass

    try:
        await stop.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):  # pragma: no cover
        pass
    finally:
        print("\n正在关闭...")
        if ws_server is not None:
            await ws_server.close()
        if grpc_server is not None:
            grpc_server.stop(1.0)
        if peerjs_server is not None:
            await peerjs_server.close()
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.selftest:
        return selftest(RemoteService(token=args.token), args.selftest_input)
    try:
        return asyncio.run(run_servers(args))
    except KeyboardInterrupt:  # pragma: no cover
        print("\n已退出。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
