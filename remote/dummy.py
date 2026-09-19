"""仿真受控端 (dummy device): 没有真设备时的替身。

**为什么需要它**

`remote.service.RemoteService` 和三种传输的服务端代码本身是通用的, 真正"依赖本机"的只有
两个层: 抓屏 (`remote.screen`) 和输入 (`remote.input`)。把它们换成假的实现, 剩下的整个栈
——协议编解码、op 注册表、服务端并发模型、`remote.ctl` 的分发层 —— 就都能在**一台机器上**
当真的练。

真有多台机器之前, 这是唯一能验证「一主多从」行为的方式: 每台 dummy 有自己的主机名、
屏幕尺寸、响应延迟和故障注入规则, 所以**能分辨出命令到底去了哪台** —— 全都返回同一个答案的
话, 广播和单发就分不出来了。

用法::

    from remote.dummy import DummySwarm

    swarm = DummySwarm.build(count=3, latency=0.1)
    await swarm.start()                       # 起 3 台, 端口随机
    swarm.dump_registry("registry.json")      # 写一份 registry 给 remote.ctl 用
    ...                                       # 跑 ctl 命令
    await swarm.close()

命令行版 (起一批在后台等着被操作)::

    python -m remote.dummy --count 3 --both --bootstrap-registry .workbuddy/tmp/reg.json

注意事项:
    - 延迟用的是 ``time.sleep``, 不是 ``asyncio.sleep``: ``RemoteService.handle`` 是同步的,
      WS 服务端会把 ``handle_request`` 丢进线程池, gRPC 服务端自己也有一池线程, 所以在被测
      **服务端的线程里**睡是安全的, 不会卡住事件循环。
    - WS 服务端所需的 |port| 必须在**协程内**取 (``WsServer.start()`` 之后立刻), 事件循环一关
      socket 信息就没了 —— 这也是 :meth:`DummyDevice.start_ws` 写成 coroutine 的原因。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw

from remote.input import InputController
from remote.screen import ScreenCapture
from remote.service import RemoteError, RemoteService

VERSION = "0.1.0"

# 几组肉眼分辨得出的屏幕底色, 分给不同的 dummy: 截屏字节数因此不同, 便于核对
PALETTES: Sequence[Tuple[int, int, int]] = (
    (24, 32, 48),
    (48, 24, 32),
    (32, 48, 24),
    (40, 40, 20),
    (20, 40, 40),
    (40, 20, 40),
)

SIZES: Sequence[Tuple[int, int]] = ((320, 200), (400, 250), (256, 160), (480, 300), (360, 240))


# ================================================================ 假抓屏 / 假输入


def fake_screen(
    width: int = 200,
    height: int = 100,
    color: Tuple[int, int, int] = (10, 20, 30),
    bars: bool = False,
) -> ScreenCapture:
    """造一个 ScreenCapture, 但抓图改成在内存里画。

    ``bars=True`` 时画三条 RGB 竖条 —— 给 dummy 用的, 让每台设备的截图内容不同
    (尺寸/颜色/字节数都不一样, 广播时一眼能看出是不是各拿到各的)。

    默认 (``bars=False``) 保持与测试里一致的 200x100 深蓝 + 一个白点。
    """
    screen = ScreenCapture()

    def fake_grab():
        image = Image.new("RGB", (width, height), color)
        if bars:
            draw = ImageDraw.Draw(image)
            tints = ((255, 80, 80), (80, 255, 80), (80, 80, 255))
            step = max(1, width // 3)
            for index, tint in enumerate(tints):
                draw.rectangle([index * step, 0, min(width, (index + 1) * step) - 1, height - 1],
                               fill=tint)
        else:
            image.putpixel((50, 25), (255, 255, 255))
        return image

    screen.grab_image = fake_grab  # type: ignore[method-assign]
    # 屏幕尺寸也跟着假走, 否则 info 报的是真实显示器
    screen.screen_size = lambda *args, **kwargs: (width, height)  # type: ignore[method-assign]
    return screen


class FakeMouse:
    """假鼠标: 记录动过什么, 不碰真实光标。"""

    def __init__(self, start: Tuple[int, int] = (400, 300)):
        self.scrolls: List[Tuple[int, int]] = []
        self.positions: List[Tuple[int, int]] = []
        self.presses = 0
        self.releases = 0
        self.clicks: List[Tuple[str, int]] = []
        self._pos = start

    @property
    def position(self):
        return self._pos

    @position.setter
    def position(self, value):
        self._pos = (int(value[0]), int(value[1]))
        self.positions.append(self._pos)

    def scroll(self, dx, dy):
        self.scrolls.append((int(dx), int(dy)))

    def press(self, button):
        self.presses += 1

    def release(self, button):
        self.releases += 1


class FakeKeyboard:
    """假键盘: 记录按键事件, 不真的按。"""

    def __init__(self):
        self.events: List[Tuple[str, Any]] = []

    def press(self, key):
        self.events.append(("press", str(key)))

    def release(self, key):
        self.events.append(("release", str(key)))

    def tap(self, key):
        self.events.append(("tap", str(key)))

    def type(self, text):
        self.events.append(("type", text))


def fake_controller(start: Tuple[int, int] = (400, 300)):
    mouse, keyboard = FakeMouse(start), FakeKeyboard()
    return InputController(mouse=mouse, keyboard=keyboard), mouse, keyboard


# ================================================================ 单台 dummy


@dataclass
class DummyDevice:
    """一台仿真受控端: 真 RemoteService + 假屏幕 + 假输入。

    每台的 ``info`` 返回值里会多点一个 ``device`` 字段 (自己的名字), 这是**为了能验证命令到达了
    哪台** -- 全都返回同一个 hostname 的话, 广播和单发就分不出来。
    """

    name: str
    size: Tuple[int, int] = (320, 200)
    color: Tuple[int, int, int] = (24, 32, 48)
    latency: float = 0.0
    jitter: float = 0.0
    fail_ops: Tuple[str, ...] = ()
    note: str = ""

    service: RemoteService = field(init=False, repr=False)
    mouse: FakeMouse = field(init=False, repr=False)
    keyboard: FakeKeyboard = field(init=False, repr=False)
    received: List[dict] = field(default_factory=list)
    ws: Any = field(default=None, repr=False)
    grpc: Any = field(default=None, repr=False)
    ws_port: Optional[int] = field(default=None)
    grpc_port: Optional[int] = field(default=None)

    def __post_init__(self) -> None:
        screen = fake_screen(self.size[0], self.size[1], self.color, bars=True)
        self.mouse = FakeMouse()
        self.keyboard = FakeKeyboard()
        controller = InputController(mouse=self.mouse, keyboard=self.keyboard)
        self.service = RemoteService(screen=screen, controller=controller)
        _install_recorder(self)

    # ---------------- 生命周期 ----------------

    async def start_ws(self, host: str = "127.0.0.1", port: int = 0) -> int:
        from remote.ws_server import WsServer

        self.ws = WsServer(self.service, host=host, port=port)
        await self.ws.start()
        # 必须在这里取端口: asyncio.run 之后 socket 信息就没了 (WinError 10038)
        self.ws_port = self.ws._server.sockets[0].getsockname()[1]
        return self.ws_port

    async def start_grpc(self, host: str = "127.0.0.1", port: int = 0) -> int:
        from remote.grpc_server import GrpcServer

        self.grpc = GrpcServer(self.service, host=host, port=port)
        self.grpc_port = self.grpc.start()
        return self.grpc_port

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
            self.ws = None
        if self.grpc is not None:
            self.grpc.stop(grace=0)
            self.grpc = None

    # ---------------- 观测 ----------------

    @property
    def ws_endpoint(self) -> Optional[str]:
        return f"ws://127.0.0.1:{self.ws_port}" if self.ws_port else None

    @property
    def grpc_endpoint(self) -> Optional[str]:
        return f"127.0.0.1:{self.grpc_port}" if self.grpc_port else None

    def ops_seen(self) -> List[str]:
        return [item["op"] for item in self.received]

    def count_op(self, op: str) -> int:
        return sum(1 for item in self.received if item["op"] == op)


def _install_recorder(device: DummyDevice) -> None:
    """给 service.handle 挂一层记录 + 延迟 + 故障注入。

    只包装 ``handle``: WS 走 ``handle_request`` (内部会落到 handle), gRPC 直接调 handle,
    两路都能记录到, 不会漏。
    """
    original = device.service.handle
    lock = threading.Lock()

    def handle(op: str, args: Optional[dict] = None) -> dict:
        started = time.perf_counter()
        parsed = dict(args or {})
        delay = device.latency + (random.uniform(0, device.jitter) if device.jitter else 0.0)
        if delay > 0:
            time.sleep(delay)  # 在服务端线程里睡, 不阻塞别的连接

        entry = {"op": op, "args": parsed, "at": round(started, 6), "ok": True, "error": None}
        try:
            if op in device.fail_ops:
                raise RemoteError("dummy_fault", f"{device.name} 被注入了 {op} 故障")
            result = original(op, args)
        except Exception as exc:  # noqa: BLE001 - 失败也要留痕
            entry["ok"] = False
            entry["error"] = f"{exc.__class__.__name__}: {exc}"
            with lock:
                device.received.append(entry)
            raise
        if isinstance(result, dict) and op in ("info", "ping"):
            result = {**result, "device": device.name}
        entry["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        with lock:
            device.received.append(entry)
        return result

    device.service.handle = handle  # type: ignore[method-assign]


# ================================================================ 一批 dummy


@dataclass
class DummySwarm:
    """一批同时跑着的仿真受控端。端口默认随机, 互不冲突。"""

    devices: List[DummyDevice]

    # ---------------- 构造 ----------------

    @classmethod
    def build(
        cls,
        count: int = 3,
        names: Optional[Sequence[str]] = None,
        latency: float = 0.0,
        jitter: float = 0.0,
        fail_ops: Sequence[str] = (),
        seed: Optional[int] = None,
    ) -> "DummySwarm":
        rng = random.Random(seed)
        devices: List[DummyDevice] = []
        for index in range(count):
            name = names[index] if names and index < len(names) else f"dummy{index + 1}"
            # 每台的主机名/尺寸/颜色都不同 -- 这样广播回来一眼能看出是不是各回各的
            devices.append(DummyDevice(
                name=name,
                size=SIZES[index % len(SIZES)],
                color=PALETTES[index % len(PALETTES)],
                latency=latency,
                jitter=jitter,
                fail_ops=tuple(fail_ops) if index == count - 1 else (),
                note=f"仿真设备 #{index + 1}",
            ))
        return cls(devices)

    def by_name(self, name: str) -> DummyDevice:
        for device in self.devices:
            if device.name == name:
                return device
        raise KeyError(f"没有这台 dummy: {name!r}; 现有: {', '.join(d.name for d in self.devices)}")

    #: 后台线程模式下的事件循环 / 线程 / 停止钩子 (见 :meth:`serve_in_thread`)
    _loop: Any = field(default=None, repr=False)
    _thread: Any = field(default=None, repr=False)
    _stop: Any = field(default=None, repr=False)

    # ---------------- 生命周期 ----------------

    async def start(self, transports: Sequence[str] = ("ws",), host: str = "127.0.0.1") -> None:
        for device in self.devices:
            if "ws" in transports or "both" in transports:
                await device.start_ws(host=host)
            if "grpc" in transports or "both" in transports:
                await device.start_grpc(host=host)

    async def close(self) -> None:
        for device in self.devices:
            await device.close()

    def serve_in_thread(
        self,
        transports: Sequence[str] = ("ws",),
        host: str = "127.0.0.1",
        timeout: float = 15.0,
    ) -> None:
        """把整批 dummy 起在**独立线程的事件循环**里 (不占调用方的线程)。

        这是多机测试的必备姿势: 如果集群与控制端跑在同一个线程里, 那控制端一阻塞
        (``asyncio.run`` 本身、``thread.join()``、甚至只是同步等待结果) 服务端的 loop 就
        转不起来, 客户端请求没人处理 —— 现象是**所有机器一起超时的假死**, 很容易被误判成
        "并发分发有问题" 或者 "某台机器挂了"。

        用法::

            swarm = DummySwarm.build(count=3)
            swarm.serve_in_thread()          # 起起来
            ctl.main(["ping", "--all", ...]) # 在调用方的线程里跑控制端
            swarm.shutdown_thread()
        """
        if self._thread is not None:
            raise RuntimeError("这个 swarm 已经在跑了")
        loop = asyncio.new_event_loop()
        ready = threading.Event()
        self._loop = loop

        def runner() -> None:
            asyncio.set_event_loop(loop)
            stop = loop.create_future()
            self._stop = lambda: loop.call_soon_threadsafe(
                lambda: (stop.done() or stop.set_result(None))
            )

            async def boot() -> None:
                await self.start(transports=transports, host=host)
                ready.set()
                try:
                    await stop
                finally:
                    await self.close()

            loop.run_until_complete(boot())
            loop.close()

        self._thread = threading.Thread(target=runner, name="dummy-swarm", daemon=True)
        self._thread.start()
        if not ready.wait(timeout):
            raise RuntimeError(f"仿真集群 {timeout}s 内没起来")
        return None

    def shutdown_thread(self, timeout: float = 10.0) -> None:
        """停止 :meth:`serve_in_thread` 起起来的集群。"""
        if self._stop is not None:
            try:
                self._stop()
            except RuntimeError:  # loop 已关
                pass
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        self._loop = None
        self._stop = None

    # ---------------- 给 remote.ctl 用的产物 ----------------

    def endpoints(self, transport: str = "ws") -> Dict[str, str]:
        getter = "ws_endpoint" if transport == "ws" else "grpc_endpoint"
        return {device.name: getattr(device, getter) for device in self.devices}

    def to_registry(self, transport: str = "ws", group: str = "dummy",
                    aliases: Optional[Dict[str, str]] = None) -> dict:
        """产出 registry 文件内容 (直接能喂给 ``ctl.Registry.load``)。

        设备的 **名字** 不等于 registry 里的 **别名** —— 别名是控制端自己起的便利叫法,
        一个设备可以不注册别名, 也可以在两个控制端里叫不同的别名。默认同名, 可用
        ``aliases`` 做一次映射, 用来测「同一个设备在他人眼里叫别的名字」。
        """
        machines = {}
        for device in self.devices:
            endpoint = self.endpoints(transport).get(device.name)
            if not endpoint:
                continue
            alias = (aliases or {}).get(device.name, device.name)
            machines[alias] = {
                "alias": alias,
                "transport": transport,
                "endpoint": endpoint,
                "token": None,
                "os": "win",
                "groups": [group],
                "timeout": 10.0,
                "note": f"{device.note} ({device.name})",
                "state": "unknown",
                "last_seen": None,
                "last_error": None,
            }
        return {"version": 1, "machines": machines}

    def dump_registry(self, path: str, transport: str = "ws", group: str = "dummy",
                      aliases: Optional[Dict[str, str]] = None,
                      merge: bool = False) -> str:
        """写 registry 到 ``path``。``merge=True`` 时把已有条目保留下来。"""
        content = self.to_registry(transport, group, aliases)
        if merge and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                existing = json.load(handle)
            machines = existing.get("machines", {})
            machines.update(content["machines"])
            content["machines"] = machines
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(content, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        return path


# ================================================================ CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m remote.dummy",
        description="起一批仿真受控端 (没有真设备时的替身)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"dummy {VERSION}")
    parser.add_argument("--count", type=int, default=3, help="起几台")
    parser.add_argument("--names", default="", help="逗号分隔的名字, 缺省 dummy1..dummyN")
    parser.add_argument(
        "--transport", choices=("ws", "grpc", "both"), default="ws",
        help="开哪种传输",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--latency", type=float, default=0.0, help="每台固定延迟秒数")
    parser.add_argument("--jitter", type=float, default=0.0, help="额外随机抖动上限")
    parser.add_argument("--fail-ops", default="", help="最后一台要注入故障的 op, 逗号分隔")
    parser.add_argument("--group", default="dummy", help="写进 registry 的组名")
    parser.add_argument(
        "--bootstrap-registry", default="",
        help="顺便把这个 swarm 写成一份 registry 文件, 供 remote.ctl 直接用",
    )
    parser.add_argument("--json", action="store_true", help="打印端点 JSON 而不是列表")
    return parser


async def _serve(args: argparse.Namespace) -> None:
    names = [n for n in args.names.split(",") if n] or None
    fail_ops = [n for n in args.fail_ops.split(",") if n]
    swarm = DummySwarm.build(count=args.count, names=names, latency=args.latency,
                             jitter=args.jitter, fail_ops=fail_ops)
    transports = ("ws", "grpc") if args.transport == "both" else (args.transport,)
    await swarm.start(transports=transports, host=args.host)

    if args.bootstrap_registry:
        for transport in transports:
            swarm.dump_registry(args.bootstrap_registry, transport=transport,
                                group=args.group, merge=transport != transports[0])

    report = []
    for device in swarm.devices:
        report.append({
            "name": device.name,
            "ws": device.ws_endpoint,
            "grpc": device.grpc_endpoint,
            "size": list(device.size),
        })
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"起了 {len(swarm.devices)} 台仿真受控端 (Ctrl-C 停止):")
        for item in report:
            line = f"  {item['name']:<10} ws={item['ws']}"
            if item["grpc"]:
                line += f"  grpc={item['grpc']}"
            line += f"  size={item['size'][0]}x{item['size'][1]}"
            print(line)
        if args.bootstrap_registry:
            print(f"\nregistry 已写好: {args.bootstrap_registry}")
            print(f"试: python -m remote.ctl ping --all --registry {args.bootstrap_registry}")

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await swarm.close()
        print("\n已停止全部仿真设备。")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(_serve(args))
    except KeyboardInterrupt:
        print("\n已中断。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
