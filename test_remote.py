"""remote/ 扩展的测试 (pytest)。

运行: ``python -m pytest test_remote.py -v``

设计原则: **不动用户的鼠标键盘**。所有输入类断言都走"预检 / 解析 / 只读位置",
真正会按键或移动光标的路径只在显式的冒烟脚本里测 (``python -m remote --selftest
--selftest-input``)。截屏是只读的, 可以放心在测试里抓。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys

import pytest
from PIL import Image

from remote import ctl
from remote.input import InputController, KeyUnsupported, resolve_key
from remote.screen import Region, ScreenCapture
from remote.service import RemoteError, RemoteService
from remote.ws_server import WsServer, decode_frame, encode_frame

LINUX_X11 = sys.platform.startswith("linux") and bool(os.environ.get("DISPLAY"))


def run(coro):
    """在同步测试里跑一段协程 (不依赖 pytest-asyncio 的配置)。"""
    return asyncio.run(coro)


# ================================================================ 假后端


def fake_screen() -> ScreenCapture:
    """造一个 ScreenCapture, 但 grab_image 打桩成内存里的假屏幕 (200x100)。

    不需要假后端了 —— 只有一个后端, 直接换掉它的抓图方法。
    """
    screen = ScreenCapture()

    def fake_grab():
        image = Image.new("RGB", (200, 100), (10, 20, 30))
        image.putpixel((50, 25), (255, 255, 255))
        return image

    screen.grab_image = fake_grab  # type: ignore[method-assign]
    return screen


def has_real_backend() -> bool:
    """本机能不能真的抓屏 (不能就跳过那些端到端测试)。"""
    try:
        ScreenCapture().screen_size()
        return True
    except Exception:
        return False


# ================================================================ 平台门禁


def test_platform_gate_is_windows_only():
    """受控端只支持 Windows: Windows 放行, 其余平台拒绝并给出可照做的提示。"""
    gate = pytest.importorskip("remote.__main__")

    assert gate.SUPPORTED_PLATFORMS == ("win32",)
    assert gate.platform_refusal("win32") is None

    for other in ("linux", "darwin", "freebsd13"):
        reason = gate.platform_refusal(other)
        assert reason is not None, f"{other} 应被拒绝"
        assert "只支持 Windows" in reason
        assert repr(other) in reason, "提示里要写清当前平台"
        assert "start-win.bat" in reason and "remote.client" in reason, "提示要给出路"


# ================================================================ 键名解析


def test_resolve_key_specials_and_aliases():
    from pynput.keyboard import Key

    assert resolve_key("enter") is Key.enter
    assert resolve_key("RETURN") is Key.enter
    assert resolve_key("esc") is Key.esc
    assert resolve_key("escape") is Key.esc
    assert resolve_key("control") is Key.ctrl
    assert resolve_key("win") is Key.cmd
    assert resolve_key("pageup") is Key.page_up
    assert resolve_key("arrowup") is Key.up
    assert resolve_key("f5") is Key.f5
    assert resolve_key("media_volume_up") is Key.media_volume_up


def test_resolve_key_chars_and_errors():
    assert resolve_key("a") == "a"
    assert resolve_key(",") == ","
    with pytest.raises(ValueError):
        resolve_key("not-a-key")
    with pytest.raises(ValueError):
        resolve_key("")


# ================================================================ 区域


def test_region_from_any():
    assert Region.from_any(None) is None
    assert Region.from_any([1, 2, 3, 4]).as_tuple() == (1, 2, 4, 6)
    assert Region.from_any({"x": 1, "y": 2, "w": 3, "h": 4}).to_dict() == {
        "left": 1, "top": 2, "width": 3, "height": 4
    }
    with pytest.raises(ValueError):
        Region.from_any([0, 0, 0, 10])
    with pytest.raises(ValueError):
        Region.from_any("nonsense")


# ================================================================ 抓屏 (假后端)


def test_capture_full_frame_and_encoding():
    screen = fake_screen()
    for fmt in ("png", "jpeg", "webp"):
        capture, data = _capture(screen, {"format": fmt})
        assert capture.width == 200 and capture.height == 100
        assert capture.source_width == 200
        assert data[:2] == (b"\x89P" if fmt == "png" else b"\xff\xd8" if fmt == "jpeg" else b"RI")
    with pytest.raises(ValueError):
        _capture(screen, {"format": "bmp"})


def test_capture_region_scale_and_limits():
    screen = fake_screen()
    capture, _ = _capture(screen, {"region": [50, 25, 10, 10]})
    assert (capture.width, capture.height) == (10, 10)
    assert capture.region.to_dict() == {"left": 50, "top": 25, "width": 10, "height": 10}

    capture, _ = _capture(screen, {"max_width": 100})
    assert (capture.width, capture.height) == (100, 50)
    assert capture.scale == pytest.approx(0.5)

    # 默认不放大
    capture, _ = _capture(screen, {"max_width": 1000})
    assert (capture.width, capture.height) == (200, 100)
    assert capture.scale == 1.0

    capture, _ = _capture(screen, {"scale": 2.0, "allow_upscale": True})
    assert (capture.width, capture.height) == (400, 200)

    with pytest.raises(ValueError):
        _capture(screen, {"region": [500, 500, 10, 10]})


def test_capture_draw_cursor():
    screen = fake_screen()
    capture, data = _capture(screen, {"draw_cursor": True, "cursor_position": (50, 25)})
    assert capture.cursor["in_frame"] is True
    image = Image.open(__import__("io").BytesIO(data)).convert("RGB")
    pixels = image.load()
    reds = sum(
        1
        for y in range(image.height)
        for x in range(image.width)
        if pixels[x, y][0] > 150 and pixels[x, y][1] < 100 and pixels[x, y][2] < 100
    )
    assert reds > 10  # 十字 + 圆圈

    capture, _ = _capture(screen, {"draw_cursor": True, "cursor_position": (9000, 9000)})
    assert capture.cursor["in_frame"] is False


def _capture(screen: ScreenCapture, args: dict):
    """便捷包装: 把 args 里的 cursor_position 之类的测试专用键摘出去。"""
    args = dict(args)
    cursor_position = args.pop("cursor_position", None)
    if "format" in args:  # 服务层用 format, ScreenCapture.capture 用 fmt
        args["fmt"] = args.pop("format")
    capture = screen.capture(cursor_position=cursor_position, **args)
    return capture, capture.data


# ================================================================ 服务层


def test_service_dispatch_basics():
    service = RemoteService(screen=fake_screen())
    ping = service.handle("ping", {})
    assert ping["pong"] is True and ping["version"]

    info = service.handle("info", {})
    assert info["os"] and info["python"]
    assert info["os"]
    assert "mouse.move" in info["capabilities"]

    # 别名
    assert service.resolve_op("screenshot") == "screen.screenshot"
    assert service.resolve_op("type") == "keyboard.type"

    with pytest.raises(RemoteError) as exc:
        service.handle("nope", {})
    assert exc.value.code == "unknown_op"

    with pytest.raises(RemoteError) as exc:
        service.handle("mouse.move", {"x": "abc"})
    assert exc.value.code == "bad_request"

    with pytest.raises(RemoteError) as exc:
        service.handle("mouse.move", {})
    assert exc.value.code == "bad_request"


def test_service_screenshot_and_request_envelope():
    service = RemoteService(screen=fake_screen())
    result = service.handle_request({"id": 7, "op": "screen.screenshot", "args": {"max_width": 100}})
    assert result["ok"] is True and result["width"] == 100
    assert base64.b64decode(result["image_b64"])[:4] == b"\x89PNG"

    # args 平铺
    flat = service.handle_request({"op": "screen.screenshot", "include_image": False})
    assert "image_b64" not in flat

    batch = service.handle_request(
        {"batch": [{"op": "ping"}, {"op": "nope"}, {"op": "screen.size"}]}
    )
    results = batch["results"]
    assert results[0]["ok"] is True
    assert results[1]["ok"] is False and results[1]["error"]["code"] == "unknown_op"
    assert results[2]["result"]["width"] == 200


def test_service_kuuki_passthrough_unknown_message():
    """kuuki 透传: 未识别的老协议消息应返回 handled=False, 且不碰鼠标。"""
    service = RemoteService(screen=fake_screen())
    result = service.handle("kuuki", {"message": {"t": "definitely-not-a-real-type"}})
    assert result["handled"] is False


# ================================================================ X11 键预检


@pytest.mark.skipif(not LINUX_X11, reason="只在 Linux/X11 上有键映射预检")
def test_key_preflight_rejects_unmapped_keys():
    controller = InputController()
    report = controller.check_keys(["a", "enter", "f1", "f13", "中", "😀"])
    by_key = {item["key"]: item for item in report["keys"]}
    assert by_key["a"]["supported"] is True
    assert by_key["enter"]["supported"] is True
    assert by_key["f13"]["supported"] is False
    assert by_key["中"]["supported"] is False
    assert by_key["😀"]["supported"] is False
    # 预检必须真的拦下, 不能走到 pynput 的 borrowing 路径
    with pytest.raises(KeyUnsupported):
        controller.tap_key("f13")
    with pytest.raises(KeyUnsupported):
        controller.type_text("你好")
    # 坏键之后仍然可以解析好键 (连接未被污染)
    assert controller.check_keys(["esc"])["keys"][0]["supported"] is True


# ================================================================ WS 端到端


def test_ws_frame_codec():
    payload = b"\x89PNG\r\n\x1a\nfake"
    header, back = decode_frame(encode_frame({"event": "frame", "seq": 3}, payload))
    assert header == {"event": "frame", "seq": 3}
    assert back == payload


@pytest.mark.skipif(not has_real_backend(), reason="没有可用的截屏后端")
def test_ws_end_to_end():
    run(_ws_end_to_end())


async def _ws_end_to_end():
    from remote.client import WsClient

    service = RemoteService(token="s3cret")
    server = WsServer(service, host="127.0.0.1", port=0, token="s3cret")
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    url = f"ws://127.0.0.1:{port}/"
    try:
        # 1) 无 token -> 被拒
        from websockets.asyncio.client import connect

        async with connect(url) as conn:
            await conn.send(json.dumps({"id": 1, "op": "ping"}))
            reply = json.loads(await asyncio.wait_for(conn.recv(), timeout=10))
            assert reply["ok"] is False and reply["error"]["code"] == "unauthorized"

        # 2) 带 token -> 正常
        async with WsClient(url, token="s3cret", timeout=20) as client:
            assert (await client.call("ping"))["pong"] is True
            info = await client.call("info")
            assert info["token_required"] is True
            position = await client.call("mouse.position")
            assert isinstance(position["x"], int)

            header, payload = await client.screenshot({"format": "png", "max_width": 120})
            assert header["event"] == "image"
            assert payload[:4] == b"\x89PNG"
            assert header["width"] <= 120

            with pytest.raises(RuntimeError) as exc:
                await client.call("definitely.not.an.op")
            assert "unknown_op" in str(exc.value)

            # 3) 推流
            frames = []
            count = await client.watch(
                {"fps": 5, "count": 2, "format": "jpeg", "max_width": 80},
                lambda h, p: frames.append((h, p)),
                count=2,
            )
            assert count == 2 and len(frames) == 2
            assert frames[0][1][:2] == b"\xff\xd8"

            # 4) kuuki 老协议自动路由
            await client.ws.send(json.dumps({"t": "definitely-not-a-real-type"}))
            reply = json.loads(await asyncio.wait_for(client.ws.recv(), timeout=10))
            assert reply["ok"] is True and reply["result"]["handled"] is False
    finally:
        await server.close()


# ================================================================ gRPC 端到端


@pytest.mark.skipif(not has_real_backend(), reason="没有可用的截屏后端")
def test_grpc_end_to_end():
    from remote.grpc_server import GrpcServer

    service = RemoteService(token="grpc-tok")
    server = GrpcServer(service, host="127.0.0.1", port=0, token="grpc-tok")
    port = server.start()
    try:
        from remote.client import GrpcClient

        with GrpcClient(f"127.0.0.1:{port}", token="grpc-tok", timeout=20) as client:
            assert client.call("ping")["ok"] is True
            info = client.call("info")
            assert info["version"]
            assert info["token_required"] is True

            position = client.call("mouse.position")
            assert "x" in position and "y" in position

            data = client.screenshot({"format": "jpeg", "quality": 60, "max_width": 100})
            assert data[:2] == b"\xff\xd8"
            assert client.last_image.width <= 100

            seen = []
            frames = client.stream({"fps": 5, "max_width": 80}, lambda img: seen.append(img), count=2)
            assert frames == 2 and len(seen) == 2

            # 键预检 (gRPC 侧也要和 WS 对齐)
            report = client.call("keyboard.check", {"keys": ["a", "enter"]})
            assert {item["key"]: item["supported"] for item in report["keys"]} == {
                "a": True,
                "enter": True,
            }

            # 鉴权失败
            with GrpcClient(f"127.0.0.1:{port}", token="wrong", timeout=10) as bad:
                import grpc

                with pytest.raises(grpc.RpcError) as exc:
                    bad.call("ping")
                assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        server.stop(0.5)


# ================================================================ PeerJS 分块


def test_peerjs_chunker_roundtrip():
    """分块协议: 小消息不分块, 大消息分块且可乱序重组, 缺块必须拒绝。"""
    import os as _os

    from remote.peerjs_server import PeerJsChunker, gen_room_code, split_message

    assert len(gen_room_code()) == 5

    small = {"id": 1, "ok": True, "result": {"x": 1}}
    assert split_message(small) == [small]

    blob = base64.b64encode(_os.urandom(200_000)).decode()
    big = {"id": 2, "ok": True, "result": {"image_b64": blob, "width": 8, "height": 6}}
    parts = split_message(big)
    assert len(parts) > 3 and parts[0]["_chunk"] == "head" and parts[-1]["_chunk"] == "end"

    # 顺序到达
    chunker = PeerJsChunker()
    out = None
    for part in parts:
        out = chunker.feed(json.loads(json.dumps(part))) or out
    assert out is not None and out["result"]["image_b64"] == blob
    assert out["result"]["width"] == 8

    # 乱序到达
    chunker = PeerJsChunker()
    out = None
    for part in [parts[0]] + list(reversed(parts[1:-1])) + [parts[-1]]:
        out = chunker.feed(part) or out
    assert out is not None and out["result"]["image_b64"] == blob

    # 缺块 -> 拒绝, 不给坏数据
    chunker = PeerJsChunker()
    for part in parts[:-2]:
        chunker.feed(part)
    assert chunker.feed(parts[-1]) is None

    # 普通消息透传
    assert PeerJsChunker().feed({"id": 9, "ok": True, "result": {}}) == {
        "id": 9, "ok": True, "result": {}
    }


# ================================================================ 动作增强 (P1)
#
# 下面全部用假鼠标/假键盘。真正的按下与移动只在 --selftest-input 冒烟里做。


class FakeMouse:
    """记录 scroll / 光标位置 / 按键次数, 不碰真实光标。"""

    def __init__(self):
        self.scrolls = []
        self.positions = []
        self.presses = 0
        self.releases = 0
        self._pos = (400, 300)

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
    def __init__(self):
        self.events = []

    def press(self, key):
        self.events.append(("press", str(key)))

    def release(self, key):
        self.events.append(("release", str(key)))

    def tap(self, key):
        self.events.append(("tap", str(key)))

    def type(self, text):
        self.events.append(("type", text))


def fake_controller():
    mouse, keyboard = FakeMouse(), FakeKeyboard()
    return InputController(mouse=mouse, keyboard=keyboard), mouse, keyboard


def side_effects(mouse, keyboard):
    return (list(mouse.scrolls), list(mouse.positions), list(keyboard.events))


def flatten(result):
    """把两条传输的响应拉平到同一形态再比较。

    WebSocket 侧 ``handle_request`` 会给 result 加一个 ``ok`` (传输信封约定);
    gRPC 侧把结果放进 ``Ack.message`` 的 JSON 字符串里。
    """
    if (
        isinstance(result, dict)
        and set(result) <= {"ok", "message"}
        and isinstance(result.get("message"), str)
    ):
        try:
            result = json.loads(result["message"])
        except json.JSONDecodeError:
            return result
    if isinstance(result, dict) and "ok" in result:
        result = {k: v for k, v in result.items() if k != "ok"}
    return result


@pytest.mark.parametrize("dy,steps", [(3, 5), (1, 3), (7, 3), (-4, 6), (10, 1), (0, 4)])
def test_scroll_steps_preserve_total(dy, steps):
    """多步滚动: 总增量必须精确等于 dy, 且不发空转的 0 格。"""
    controller, mouse, _ = fake_controller()
    controller.scroll(0, dy, steps=steps, interval=0)
    assert sum(step for _, step in mouse.scrolls) == dy
    assert all(step != 0 for _, step in mouse.scrolls)


def test_scroll_single_step_keeps_old_behaviour():
    controller, mouse, _ = fake_controller()
    controller.scroll(2, -3)
    assert mouse.scrolls == [(2, -3)], "默认 steps=1 应保持一次调用"


def test_scroll_position_then_scroll():
    """先定位再滚: 只给 x 时 y 保持当前坐标 (不是 0)。"""
    controller, mouse, _ = fake_controller()
    controller.scroll(0, 2, x=120, interval=0)
    assert mouse.positions[0] == (120, 300)
    assert mouse.scrolls == [(0, 2)]


def test_drag_path_points():
    controller, mouse, _ = fake_controller()
    result = controller.drag(points=[[10, 20], [30, 40], [50, 60]], duration=0)
    assert result["segments"] == 2
    assert result["from"] == [10, 20] and result["to"] == [50, 60]
    assert (30, 40) in mouse.positions, "路径点必须被真正经过"
    assert (mouse.presses, mouse.releases) == (1, 1), "整条路径只按一次、只松一次"


def test_drag_two_points_backward_compatible():
    controller, mouse, _ = fake_controller()
    result = controller.drag(1, 2, 3, 4, duration=0)
    assert result["from"] == [1, 2] and result["to"] == [3, 4]
    assert result["segments"] == 1


@pytest.mark.parametrize("kwargs", [{"points": [[1, 2]]}, {"points": [[1, 2], [3]]}, {}])
def test_drag_rejects_bad_points(kwargs):
    controller, _, _ = fake_controller()
    with pytest.raises(ValueError):
        controller.drag(**kwargs)


def test_combo_hold_ms_and_hold_key():
    controller, _, keyboard = fake_controller()

    result = controller.hotkey("ctrl+shift+s", hold_ms=10)
    assert result["hold_ms"] == 10
    order = [name for name, _ in keyboard.events]
    assert order == ["press", "press", "press", "release", "release", "release"]
    # 逆序松开: 最后按下的是 s, 最先松开
    keys = [key for _, key in keyboard.events]
    assert keys[2] == keys[3], "s 应紧邻地按下再松开"
    assert keys[5] == str(resolve_key("ctrl"))

    keyboard.events.clear()
    assert controller.hold_key("f2", 10) == {"key": "f2", "ms": 10}
    assert [name for name, _ in keyboard.events] == ["press", "release"]

    with pytest.raises(ValueError):
        controller.hold_key("f2", 0)


def test_parse_points_accepts_two_forms():
    from remote.service import parse_points

    assert parse_points([[1, 2], [3, 4]]) == [[1, 2], [3, 4]]
    assert parse_points([{"x": 1, "y": 2}, {"x": 3, "y": 4}]) == [[1, 2], [3, 4]]
    assert parse_points("1,2;3,4") == [[1, 2], [3, 4]]
    for bad in ("1,2;bad", [[1, 2], [3]], "nonsense"):
        with pytest.raises(RemoteError):
            parse_points(bad)


def test_service_new_ops_and_aliases():
    controller, mouse, keyboard = fake_controller()
    service = RemoteService(screen=fake_screen(), controller=controller)

    assert service.resolve_op("scroll_h") == "mouse.scroll_h"
    assert service.resolve_op("combo") == "keyboard.combo"
    assert service.resolve_op("hold") == "keyboard.hold"
    assert service.resolve_op("dragp") == "mouse.drag"

    capabilities = service.handle("info", {})["capabilities"]
    for op in ("mouse.scroll_h", "keyboard.combo", "keyboard.hold"):
        assert op in capabilities

    out = service.handle("scroll", {"dy": 6, "steps": 3, "interval": 0})
    assert out["steps"] == 3
    assert sum(step for _, step in mouse.scrolls) == 6

    out = service.handle("dragp", {"points": "10,20;30,40;50,60", "duration": 0})
    assert out["segments"] == 2

    out = service.handle("scroll_h", {"dx": 4, "steps": 2, "interval": 0})
    assert out["axis"] == "h" and out["dy"] == 0

    out = service.handle("combo", {"keys": "ctrl+c", "hold_ms": 5})
    assert out["keys"] == ["ctrl", "c"] and out["hold_ms"] == 5

    out = service.handle("hold", {"key": "f2", "ms": 5})
    assert out["ms"] == 5 and out["key"] == "f2"

    # 老协议的 delta 滚动不能被新增的 steps 参数弄坏
    mouse.scrolls.clear()
    service.handle("scroll", {"delta": 2})
    assert mouse.scrolls == [(0, 2)]


def test_service_rejects_bad_new_op_args():
    service = RemoteService(screen=fake_screen(), controller=fake_controller()[0])
    for op, args in (
        ("mouse.drag", {"points": [[1, 2]]}),
        ("mouse.drag", {"points": "1,2;bad"}),
        ("keyboard.hold", {"key": "f2", "ms": 0}),
        ("keyboard.hold", {"ms": 50}),
        ("keyboard.combo", {}),
    ):
        with pytest.raises(RemoteError) as exc:
            service.handle(op, args)
        assert exc.value.code == "bad_request", f"{op} {args}"


NEW_OP_CASES = [
    ("mouse.scroll", {"dy": 7, "steps": 3, "interval": 0}),
    ("mouse.scroll", {"dy": 2, "x": 111}),
    ("mouse.scroll", {"dy": 2, "y": 222}),
    ("mouse.scroll_h", {"dx": 5, "steps": 2, "interval": 0}),
    ("mouse.drag", {"points": [[10, 20], [30, 40], [50, 60]], "duration": 0}),
    ("mouse.drag", {"points": "10,20;30,40", "duration": 0}),
    ("keyboard.hotkey", {"keys": "ctrl+shift+s"}),
    ("keyboard.combo", {"keys": "ctrl+shift+s", "hold_ms": 5}),
    ("keyboard.hold", {"key": "f2", "ms": 5}),
]


def test_new_ops_agree_across_transports():
    """同一动作经 WS 与 gRPC 下发, 返回值与副作用必须一致。

    这条是 P1 的底线: 三个传输共用一个 ``RemoteService``, 但两边的**参数翻译层**
    各写各的, 很容易出现"WS 认得 interval=0、gRPC 当成没给"这种偏差
    (proto3 的普通标量分不清显式 0 与缺省, 所以相关字段都用了 optional)。
    """
    from remote.grpc_server import GrpcServer
    from remote.client import GrpcClient, WsClient

    async def over_ws():
        controller, mouse, keyboard = fake_controller()
        service = RemoteService(screen=fake_screen(), controller=controller)
        server = WsServer(service, host="127.0.0.1", port=0)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]
        collected = []
        try:
            async with WsClient(f"ws://127.0.0.1:{port}/", timeout=20) as client:
                for op, args in NEW_OP_CASES:
                    mouse.scrolls.clear()
                    mouse.positions.clear()
                    keyboard.events.clear()
                    result = await client.call(op, args)
                    collected.append((result, side_effects(mouse, keyboard)))
        finally:
            await server.close()
        return collected

    ws_results = run(over_ws())

    controller, mouse, keyboard = fake_controller()
    service = RemoteService(screen=fake_screen(), controller=controller)
    grpc_server = GrpcServer(service, host="127.0.0.1", port=0)
    port = grpc_server.start()
    grpc_results = []
    try:
        with GrpcClient(f"127.0.0.1:{port}", timeout=20) as client:
            for op, args in NEW_OP_CASES:
                mouse.scrolls.clear()
                mouse.positions.clear()
                keyboard.events.clear()
                result = client.call(op, args)
                grpc_results.append((result, side_effects(mouse, keyboard)))
    finally:
        grpc_server.stop(0.5)

    for (op, args), (ws_result, ws_side), (grpc_result, grpc_side) in zip(
        NEW_OP_CASES, ws_results, grpc_results
    ):
        assert flatten(ws_result) == flatten(grpc_result), f"{op} {args} 返回值不一致"
        assert ws_side == grpc_side, f"{op} {args} 副作用不一致"

# ================================================================ 控制端 (P3)
#
# ``remote/ctl.py`` = 多机编排层。这些测试全都不联网: 要么直接调 registry /
# 分发函数, 要么把 WS 服务端起在**随机端口**上做端到端 —— 不允许碰真实光标。


def _machine(alias: str, **kwargs) -> ctl.Machine:
    payload = {"transport": "ws", "endpoint": "ws://127.0.0.1:1/"}
    payload.update(kwargs)
    return ctl.Machine(alias=alias, **payload)


def test_ctl_registry_crud_and_persistence(tmp_path):
    path = str(tmp_path / "registry.json")
    registry = ctl.Registry(path)
    registry.put(_machine("a", groups=["office"], timeout=3.0, note="工位机"))
    registry.put(_machine("b", transport="grpc", endpoint="10.0.0.2:50051", groups="lab,office"))
    registry.save()

    # 逗号字符串应被拆成列表
    assert registry.get("b").groups == ["lab", "office"]
    reloaded = ctl.Registry.load(path)
    assert list(reloaded.machines()) == ["a", "b"]
    machine = reloaded.get("a")
    assert (machine.transport, machine.timeout, machine.note) == ("ws", 3.0, "工位机")
    assert reloaded.get("missing") is None

    assert registry.remove("b") is True
    assert registry.remove("b") is False
    registry.save()  # 注销要落盘才算数
    assert list(ctl.Registry.load(path).machines()) == ["a"]


def test_ctl_resolve_all_group_alias_and_dedupe():
    registry = ctl.Registry("unused.json")
    for alias, groups in (("a", ["office"]), ("b", ["office"]), ("c", ["lab"])):
        registry.put(_machine(alias, groups=groups))

    assert [m.alias for m in registry.resolve(["a"])] == ["a"]
    assert [m.alias for m in registry.resolve(["a", "b"])] == ["a", "b"]
    assert [m.alias for m in registry.resolve([], all_machines=True)] == ["a", "b", "c"]
    assert [m.alias for m in registry.resolve(["all"])] == ["a", "b", "c"]
    # 组播与单播重叠时不应重复下发同一台
    assert [m.alias for m in registry.resolve(["c"], group="office")] == ["a", "b", "c"]

    with pytest.raises(KeyError):
        registry.resolve(["nope"])
    with pytest.raises(KeyError):
        registry.resolve([], group="ghost")


def test_ctl_dispatch_collects_per_machine_outcome():
    """一台挂了不能拖垮整批 —— 结果按机器逐条列出。"""
    good, bad = _machine("good"), _machine("bad")

    async def worker(machine):
        if machine.alias == "bad":
            raise RuntimeError("连接被拒")
        return {"ok": True}

    results = run(ctl.dispatch([good, bad], worker))
    by_alias = {item["alias"]: item for item in results}
    assert by_alias["good"]["ok"] is True
    assert by_alias["bad"]["ok"] is False
    assert by_alias["bad"]["error"] == "RuntimeError: 连接被拒"
    assert all(isinstance(item["elapsed_ms"], float) for item in results)


def test_ctl_dispatch_honours_per_machine_timeout():
    slow = _machine("slow", timeout=0.05)

    async def worker(_machine):
        await asyncio.sleep(0.5)

    results = run(ctl.dispatch([slow], worker))
    assert results[0]["ok"] is False
    assert results[0]["error"].startswith("TimeoutError:")
    assert "0.05s" in results[0]["error"]


def test_ctl_update_states_marks_offline_and_online():
    registry = ctl.Registry("unused.json")
    registry.put(_machine("a"))
    registry.put(_machine("b"))
    ctl.update_states(registry, [
        {"alias": "a", "ok": True},
        {"alias": "b", "ok": False, "error": "TimeoutError: 超过 1s 未响应"},
    ])
    assert registry.get("a").state == "online"
    assert registry.get("a").last_seen
    assert registry.get("b").state == "offline"
    assert "TimeoutError" in registry.get("b").last_error


@pytest.mark.parametrize("command, want", [
    ("ping all", ("ping", {})),
    ("pos -a", ("mouse.position", {})),
    ("move self 400 300 --duration .3", ("mouse.move", {"x": 400, "y": 300, "duration": 0.3})),
    ("move-rel self --dx 5 --dy -5", ("mouse.move_rel", {"dx": 5, "dy": -5, "duration": 0.0})),
    ("click self --button right --clicks 2",
     ("mouse.click", {"button": "right", "clicks": 2, "interval": 0.05})),
    ("down self", ("mouse.down", {"button": "left"})),
    ("up self --button middle", ("mouse.up", {"button": "middle"})),
    ("scroll self --dy 3 --steps 5", ("mouse.scroll", {"dx": 0, "dy": 3, "steps": 5})),
    ("scroll-h self --dx 4 --steps 2", ("mouse.scroll_h", {"dx": 4, "dy": 0, "steps": 2})),
    ("drag self 10 10 200 200 --duration .5",
     ("mouse.drag", {"x1": 10, "y1": 10, "x2": 200, "y2": 200, "button": "left",
                     "duration": 0.5})),
    ("dragp self 10,10;100,100", ("mouse.drag", {"points": "10,10;100,100", "button": "left"})),
    ("type self hello --interval 0.01", ("keyboard.type", {"text": "hello", "interval": 0.01})),
    ("paste self 中文", ("keyboard.paste", {"text": "中文"})),
    ("key self f5", ("keyboard.key", {"key": "f5", "action": "tap", "modifiers": []})),
    ("combo self ctrl+shift+s --hold 200",
     ("keyboard.combo", {"keys": "ctrl+shift+s", "hold_ms": 200.0})),
    ("hold self f2 500", ("keyboard.hold", {"key": "f2", "ms": 500.0})),
])
def test_ctl_translates_cli_to_ops(command, want):
    """文档里那套命令必须真能被 parser 认下, 且翻译成对的 op —— 防止悄悄失效。"""
    args = ctl._fill_defaults(ctl.build_parser().parse_args(command.split()))
    op, payload = ctl._machine_op(args)
    assert (op, payload) == want


def test_ctl_op_and_check_take_targets_by_flag():
    """op 名 / check 键名与别名列表都是位置参数, 会互相吞 —— 目标只能走 -t/-g/-a。"""
    args = ctl._fill_defaults(ctl.build_parser().parse_args("op screen.size -t self".split()))
    assert (args.command, args.name, ctl._wanted(args)) == ("op", "screen.size", ["self"])

    args = ctl._fill_defaults(ctl.build_parser().parse_args("check -a ctrl shift".split()))
    assert ctl._machine_op(args)[1] == {"keys": ["ctrl", "shift"]}


def test_ctl_expand_path_avoids_overwrite():
    # 单机: 原样
    assert ctl.expand_path("shot.png", "a", False) == "shot.png"
    # 多机 + 文件名: 插别名
    assert ctl.expand_path("shot.png", "a", True) == "shot-a.png"
    # 多机 + 无扩展名: 当目录
    assert ctl.expand_path("shots", "a", True).replace("\\", "/") == "shots/a.png"
    # 占位符
    assert ctl.expand_path("f-{alias}.png", "a", True) == "f-a.png"


def test_ctl_end_to_end_over_ws(tmp_path):
    """真的起一个 WS 受控端, 走一遍控制端的完整链路。

    底线是这个链条不能断: registry → 目标解析 → 传输适配 → 结果汇总 → 状态回填。
    全程假屏幕 + 假输入, 不碰真实光标。

    注意: 整段必须**在同一个协程里**跑完 —— ``ctl.main()`` 内部会自己
    ``asyncio.run``, 服务端也就跟着被那次 run 结束时关闭了 (现有测试的多次
    ``run()`` 之所以没事, 是因为它们每次都在协程内连完就走)。
    """
    service = RemoteService(screen=fake_screen(), controller=fake_controller()[0])
    server = WsServer(service, host="127.0.0.1", port=0)
    registry_path = str(tmp_path / "registry.json")

    async def scenario():
        await server.start()
        # 端口要在协程里取: 事件循环关闭后 socket 也被收掉, 再 getsockname() 会报 10038
        port = server._server.sockets[0].getsockname()[1]
        try:
            registry = ctl.Registry(registry_path)
            registry.put(ctl.Machine(alias="ag", transport="ws",
                                     endpoint=f"ws://127.0.0.1:{port}/", groups=["local"]))
            registry.save()

            # 命令⁻>op 的翻译: 直接喂真实的命令行动词
            machines = registry.resolve(["all"])
            for command, op in (
                ("ping all", "ping"),
                ("pos all", "mouse.position"),
                ("info -g local", "info"),
            ):
                args = ctl._fill_defaults(ctl.build_parser().parse_args(command.split()))
                assert ctl._machine_op(args)[0] == op
                results = await ctl.dispatch(
                    registry.resolve(ctl._wanted(args), group=args.group, all_machines=args.all),
                    lambda m, op=op: ctl.call_machine(m, op, {}),
                )
                assert [item["ok"] for item in results] == [True], f"{op} 失败: {results}"

            meta, data = await ctl.capture_machine(machines[0], {"format": "png"})
            assert meta["width"] == 200 and len(data) > 100  # 假屏幕是 200x100

            ctl.update_states(registry, [{"alias": "ag", "ok": True}])
            registry.save()
            assert ctl.Registry.load(registry_path).get("ag").state == "online"
        finally:
            await server.close()

    run(scenario())


def test_ctl_reports_unknown_alias(tmp_path):
    registry = str(tmp_path / "registry.json")
    ctl.main(["machines", "add", "a", "--transport", "ws",
              "--endpoint", "ws://127.0.0.1:1/", "--registry", registry])
    # 未知别名是用法错误, 退出码 2, 不是"执行失败"的 1
    assert ctl.main(["ping", "ghost", "--registry", registry]) == 2
