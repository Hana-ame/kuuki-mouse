"""``python -m remote`` —— WebSocket / gRPC / PeerJS 三种传输按需开启, **默认只开 PeerJS**。

**受控端只支持 Windows** —— 这是产品定位, 不是临时限制。非 Windows 上会在启动前直接
拒绝 (退出码 2), 见 :func:`platform_refusal`。WSL / Linux / 手机侧只跑**客户端**, 连到
Windows 上的服务端; 想从 WSL 里控制 Windows 桌面见 ``remote/win/``。

**为什么默认只开 PeerJS**: 它连公开 broker、靠房间码配对, 不需要本地端口、不需要
防火墙放行, 拿起来就能用; WS / gRPC 要占端口并把端口暴露出去, 属于"要了才给"的东西,
不该在用户没开口时就默认占上。

开关语义 (见 :func:`resolve_transports`): **给了任何一个开启开关就以给的那几个为准**
—— ``--ws`` 就是"只要 WebSocket", 不会顺带把 PeerJS 也注册到公开 broker 上; 一个都
没给才用默认。``--no-xxx`` 是在这个结果上再减。

默认**只绑 127.0.0.1**; 要给别的机器用必须显式 ``--allow-remote`` 且设置 token。

    python -m remote                       # 只开 PeerJS (默认, 房间码配对, 不需要端口)
    python -m remote --ws                  # 只开 WebSocket 8765
    python -m remote --grpc                # 只开 gRPC 50051
    python -m remote --ws --grpc           # 本机两个端口, 不连公开 broker
    python -m remote --ws --peerjs         # WebSocket + PeerJS
    python -m remote --ws --grpc --peerjs  # 三个全开
    python -m remote --ws --ws-port 9000   # 换端口 (端口参数不会替你把传输打开)
    python -m remote --token secret        # 所有已开的传输都要 token
    python -m remote --allow-remote --token secret   # 绑 0.0.0.0 (危险, 必须带 token)
    python -m remote --selftest            # 只做自检: 报告后端 + 抓一帧, 不动鼠标
    python -m remote --selftest --selftest-input     # 额外测一次鼠标移动(会动光标)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys

# 允许 `python remote/__main__.py` 直接跑 (此时包上下文为空, 需要把仓库根加进 sys.path)
if __package__ in (None, ""):  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# GrpcServer **故意不在这里 import**: remote/grpc_server.py 顶层就要 grpc, 而 grpcio
# 连 requirements.txt 里都没写 (只有真要用 gRPC 才需要装)。顶层 import 会让"只开
# PeerJS 的默认用法"因为没装 grpcio 直接 ImportError —— 所以挪到真要开 gRPC 时再导。
from remote.peerjs_server import PeerJsServer  # noqa: E402
from remote.service import RemoteError, RemoteService, VERSION  # noqa: E402
from remote.ws_server import WsServer  # noqa: E402
# 仓库根的顶层模块 (不是 remote 的子模块): 打印中文前先把流切到 UTF-8,
# 否则英文 Windows (cp1252) 上 --help 直接 UnicodeEncodeError。见 utf8_stdio.py
from utf8_stdio import force_utf8_stdio  # noqa: E402

log = logging.getLogger("kuuki.remote")

#: 受控端 (被操作的那台机器) 的平台白名单。产品定位就是 Windows: 屏幕采集走
#: ``PIL.ImageGrab``、输入注入走 pynput 的 Win32 后端, 目标桌面只有 Windows 一种。
#: 非 Windows 的 X11 / WSLg 分支**保留作参考但不再维护** —— 见 ``remote/screen.py``
#: 与 ``remote/input.py`` 里的 ``_LINUX`` 相关代码, 服务端不会再走到。
SUPPORTED_PLATFORMS = ("win32",)


def platform_refusal(platform: str | None = None) -> str | None:
    """平台不受支持时返回给用户看的拒绝说明, 受支持时返回 ``None``。

    ``platform`` 只用于测试注入, 默认取 ``sys.platform``。
    """
    current = sys.platform if platform is None else platform
    if any(current.startswith(supported) for supported in SUPPORTED_PLATFORMS):
        return None
    return (
        f"拒绝启动: 受控端只支持 Windows, 当前平台是 {current!r}。\n"
        "\n"
        "  受控端 = 被操作的那台机器, 它必须原生跑在 Windows 上:\n"
        "      Windows 侧:  python -m remote        (或 start-win.bat)\n"
        "\n"
        "  要从 WSL / Linux / 手机侧操作它, 在那边只跑客户端, 连到 Windows 上的服务端:\n"
        "      python -m remote.client peerjs --peer kuuki-mouse-<房间码> info\n"
        "      python -m remote.client ws ping      (服务端在本机时)\n"
        "\n"
        "  想在 WSL 里控制 Windows 桌面: 见 remote/win/ (实验性, 未接入服务端)。\n"
        "  详见 remote/README.md 第 5 节 (两端点的部署位置) 与第 8 节 (已知限制)。"
    )


#: 默认开启的传输。只开 PeerJS 的理由见模块 docstring: 它不需要本地端口, 而 WS/gRPC
#: 一旦开了就占端口、要防火墙放行, 属于"要了才给"。
DEFAULT_TRANSPORTS = ("peerjs",)

#: 展示/启动顺序。WebSocket 与 gRPC 是本地端口 (放前面), PeerJS 是出站的 (放后面)。
TRANSPORT_ORDER = ("ws", "grpc", "peerjs")


class _RememberPort(argparse.Action):
    """记下"这个端口参数用户真的手打过"。

    光看值分不出 ``--ws-port 8765`` 里的 8765 是默认值还是用户给的 —— 但"给了端口
    却没开对应传输"一定是手误 (换了端口结果服务没起来), 要点出来而不是静默忽略。
    """

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        setattr(namespace, f"{self.dest}_explicit", True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m remote",
        description=(
            "kuuki-mouse 远程控制扩展: 鼠标/键盘控制 + 截屏, "
            "WebSocket / gRPC / PeerJS 三种传输按需开启, 默认只开 PeerJS "
            "(受控端只支持 Windows)"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.set_defaults(ws_port_explicit=False, grpc_port_explicit=False)
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument(
        "--ws", action="store_true",
        help="开启 WebSocket (默认不开; 给了任何传输开关就以给的为准)",
    )
    parser.add_argument(
        "--grpc", action="store_true",
        help="开启 gRPC (默认不开; 需要装 grpcio)",
    )
    parser.add_argument(
        "--peerjs", action="store_true",
        help="开启 PeerJS (默认就开着; 走 0.peerjs.com 公开 broker, 无需端口/域名)",
    )
    parser.add_argument("--ws-port", action=_RememberPort, type=int, default=8765, help="WebSocket 端口")
    parser.add_argument("--grpc-port", action=_RememberPort, type=int, default=50051, help="gRPC 端口")
    parser.add_argument("--no-ws", action="store_true", help="不开 WebSocket")
    parser.add_argument("--no-grpc", action="store_true", help="不开 gRPC")
    parser.add_argument(
        "--no-peerjs", action="store_true",
        help="不开 PeerJS (默认开; 关了就只剩本地端口这几种连法)",
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
    parser.add_argument(
        "--page-url",
        default=None,
        help="手机端页面地址 (默认从 git remote 推断 GitHub Pages 地址)",
    )
    parser.add_argument(
        "--qr",
        action="store_true",
        help="额外打印配对二维码 (终端 ASCII + 当前目录 pair_<房间码>.png)",
    )
    parser.add_argument("--selftest", action="store_true", help="自检后退出 (不启动服务)")
    parser.add_argument(
        "--selftest-input",
        action="store_true",
        help="自检时额外移动一次鼠标 (会动到光标, 默认不动)",
    )
    parser.add_argument("--version", action="version", version=f"kuuki remote {VERSION}")
    return parser


def resolve_transports(args: argparse.Namespace) -> list:
    """把命令行开关解析成"这次要开哪些传输", 按 :data:`TRANSPORT_ORDER` 排序返回。

    语义是**枚举即选择**: 给了任何一个 ``--ws/--grpc/--peerjs`` 就以给的那几个为准,
    一个都没给才退回 :data:`DEFAULT_TRANSPORTS`。这样 ``--ws`` 就是"只要 WebSocket",
    不会顺带把 PeerJS 也注册到公开 broker 上 —— 多开一条出站的通道不该是默认行为,
    而 additive 语义 ("--ws = PeerJS + WS") 会让想只要本地端口的人白白暴露出去。

    ``--no-xxx`` 是在这个结果上再减, 所以 ``--ws --no-peerjs`` 依旧是"只要 WebSocket",
    ``--no-peerjs`` 单独用则什么都不剩 (由 :func:`main` 拦下来)。
    """
    enabled = {name for name in TRANSPORT_ORDER if getattr(args, name, False)}
    if not enabled:
        enabled = set(DEFAULT_TRANSPORTS)
    for name in TRANSPORT_ORDER:
        if getattr(args, f"no_{name}", False):
            enabled.discard(name)
    return [name for name in TRANSPORT_ORDER if name in enabled]


def transport_conflicts(args: argparse.Namespace, transports: list) -> list:
    """找出"给了某传输的专属参数, 但那个传输没开"的组合。

    这类组合在开关语义下会**静默什么都不做** —— 用户换了端口、指定了房间码, 服务却
    没起来, 而且没有任何提示 (改默认之前不存在这个问题, 因为三个都是开的)。宁可启动时
    报错, 也别让人对着一个不存在的端口排查半天。
    """
    problems = []
    if args.ws_port_explicit and "ws" not in transports:
        problems.append("--ws-port 只在 WebSocket 开启时有效: 要再加 --ws")
    if args.grpc_port_explicit and "grpc" not in transports:
        problems.append("--grpc-port 只在 gRPC 开启时有效: 要再加 --grpc")
    if args.room and "peerjs" not in transports:
        problems.append("--room 只在 PeerJS 开启时有效: 要再加 --peerjs")
    return problems


def empty_transport_refusal() -> str:
    """一个传输都没开时的拒绝说明 (退出码 2)。"""
    return (
        "拒绝启动: 一个传输都没开 (默认只开 PeerJS, 被 --no-peerjs 关掉了)。\n"
        "\n"
        "  挑一个开:\n"
        "      python -m remote              # 只开 PeerJS (默认, 房间码配对)\n"
        "      python -m remote --ws         # 只开 WebSocket 8765\n"
        "      python -m remote --grpc       # 只开 gRPC 50051\n"
        "      python -m remote --ws --grpc  # 本机两个端口\n"
        "\n"
        "  开关语义: 给了任何 --ws/--grpc/--peerjs 就以给的那几个为准, --no-xxx 再减。"
    )


#: git remote 也认不出来时的兜底 (本仓库 fork 后地址会变, 所以能推断就推断)
FALLBACK_PAGE_URL = "https://hana-ame.github.io/kuuki-mouse/"


def guess_page_url() -> str:
    """从 ``git remote`` 推断 GitHub Pages 地址。

    页面托管在哪取决于仓库是谁 fork 的, 硬编码会指到别人家去 —— 所以用 remote 推。
    认不出来 (没装 git / 不是 GitHub / 不在仓库里) 就退回 ``FALLBACK_PAGE_URL``,
    宁可给个能改的错地址, 也别什么都不说。
    """
    try:
        import subprocess

        done = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        match = re.match(
            r"(?:git@|https://)github\.com[:/]([^/]+)/(.+?)(?:\.git)?/?$",
            done.stdout.strip(),
        )
        if match:
            return f"https://{match.group(1)}.github.io/{match.group(2)}/"
    except Exception:  # noqa: BLE001 - 认不出就用兜底, 不值得为此失败
        pass
    return FALLBACK_PAGE_URL


def pair_page(args: argparse.Namespace) -> str:
    """手机端页面地址: 命令行给了就用给的, 否则从 git remote 推断。"""
    return (args.page_url or guess_page_url()).rstrip("/")


def show_qr(url: str, room: str) -> None:
    """打印配对二维码 (终端 ASCII + PNG)。

    ``qrcode`` 是惰性 import: 只有给了 ``--qr`` 才需要它, 平时跑服务不该被这个
    可选依赖绊住 —— 而且它不在打进 exe 的必需路径上。

    二维码里**只放房间码, 不放 token**: 二维码会被截图/转发, token 该手填
    (页面上有输入框, 或把 ``#/房间码?token=xxx`` 自己做成码)。
    """
    try:
        import qrcode
    except ImportError:
        print("  (没装 qrcode, 跳过二维码: pip install qrcode)")
        return

    code = qrcode.QRCode(border=2, box_size=8)
    code.add_data(url)
    code.make(fit=True)
    try:
        import pathlib

        png = f"pair_{room}.png"
        code.make_image().save(png)
        print(f"  二维码图片: {pathlib.Path(png).resolve()}")
    except Exception as exc:  # noqa: BLE001 - 缺 Pillow 等, 终端码还能用
        print(f"  (二维码 PNG 保存失败: {exc} — 不影响配对)")
    try:
        # tty=True 会输出 ANSI 颜色码, Windows 终端显示为乱码, 所以用 tty=False
        code.print_ascii(tty=False)
    except Exception:  # noqa: BLE001
        pass


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


async def run_servers(args: argparse.Namespace, transports: list) -> int:
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

    if "ws" in transports:
        ws_server = WsServer(service, host=host, port=args.ws_port, token=token, max_fps=args.max_fps)
        await ws_server.start()
    if "grpc" in transports:
        # 惰性导入: 见文件头 —— 没开 gRPC 就不该要求装 grpcio
        from remote.grpc_server import GrpcServer

        grpc_server = GrpcServer(service, host=host, port=args.grpc_port, token=token)
        grpc_server.start()
    if "peerjs" in transports:
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
        payload = {"version": VERSION, "endpoints": endpoints}
        # 配对链接也给一份: --json 是给程序读的 (自己做 UI / 自己出码),
        # 让人去解析 endpoints 拼 URL 没道理
        if peerjs_server is not None:
            payload["pair_url"] = f"{pair_page(args).rstrip('/')}/#/{peerjs_server.room}"
        print(json.dumps(payload, ensure_ascii=False, indent=2))
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
        # 没设 token 时的提示要分情况: "仅回环安全" 只在**没有 PeerJS** 时才成立。
        # PeerJS 是这台机器主动连出去注册到公开 broker 的 —— 别人知道房间码就能连进来,
        # 跟本地绑的是 127.0.0.1 还是 0.0.0.0 完全无关。房间码 31^5 ≈ 2860 万种,
        # 挡不住有心的枚举, 它只是配对用的, 不是凭证。
        if token:
            print("  token     : 已设置")
        elif peerjs_server is not None:
            print("  token     : 未设置 —— PeerJS 经公开 broker 配对, 知道房间码的人都能控这台机器")
            print("              不可信网络下请先加 --token <口令> (手机端配对时要填同一个)")
        else:
            print("  token     : 未设置 (仅回环安全)")

        # --allow-remote 只管本地端口绑哪儿; 只开 PeerJS 时它没有任何作用。不点出来
        # 的话, 用户会以为自己已经"放行远程"了, 然后对着连不上的 ws:// 排查半天。
        if args.allow_remote and ws_server is None and grpc_server is None:
            print("  注意      : --allow-remote 只影响本地端口, 这次没开 WS/gRPC, 它不起作用")

        # 手机端是扫码/打开链接配对的主路径, 只给个裸房间码等于让人手打五位数。
        # 二维码里不放 token (码会被截图转发), 设了 token 就提示手填。
        if peerjs_server is not None:
            page = pair_page(args)
            room = next(
                (e["room"] for e in endpoints if e["transport"] == "peerjs"), ""
            )
            if room:
                print(f"  手机端    : {page}/#/{room}")
                if args.qr:
                    show_qr(f"{page}/#/{room}", room)
                if token:
                    print(f"              (设了 token: 页面上的 Token 框要填同一个)")

        # 示例要给一条**这次真能跑通**的命令 —— 默认只开 PeerJS 时还说 `ws ping`
        # 是拿一条连不上的命令当指引。
        if ws_server is not None:
            print("  客户端示例: python -m remote.client ws ping")
        elif grpc_server is not None:
            print("  客户端示例: python -m remote.client grpc ping")
        elif peerjs_server is not None:
            print(f"  客户端示例: python -m remote.client peerjs --peer {peerjs_server.peer_id} ping")
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
    # 必须在 parse_args 之前: --help / --version 就在 parse_args 里输出并退出
    force_utf8_stdio()
    args = build_parser().parse_args(argv)
    # 平台门禁: 放在 --help/--version 之后 (那两个在 parse_args 内就退出了, 不受影响),
    # 但排在 --selftest 与起服务之前 —— 自检同样会碰屏幕和输入, 非 Windows 无从谈起。
    refusal = platform_refusal()
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return 2
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.selftest:
        return selftest(RemoteService(token=args.token), args.selftest_input)

    # 传输选择排在 --selftest 之后: 自检不起服务, 开哪几个传输跟它无关, 不该因为
    # 带了 --no-peerjs 就让人连自检都做不了。
    transports = resolve_transports(args)
    if not transports:
        print(empty_transport_refusal(), file=sys.stderr)
        return 2
    conflicts = transport_conflicts(args, transports)
    if conflicts:
        print(f"拒绝启动: 参数对不上 (这次开的传输: {', '.join(transports)})", file=sys.stderr)
        for problem in conflicts:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    try:
        return asyncio.run(run_servers(args, transports))
    except KeyboardInterrupt:  # pragma: no cover
        print("\n已退出。")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
