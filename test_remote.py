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
import threading
import time

import pytest
from PIL import Image

from remote import ctl
from remote import dummy
from remote.dummy import FakeKeyboard, FakeMouse, fake_controller, fake_screen
from remote.input import InputController, KeyUnsupported, resolve_key
from remote.peerjs_client import PeerJsClient
from remote.peerjs_server import (CHUNK_SIZE, PEER_PREFIX, PeerJsChunker,
                                  PeerJsServer, split_message)
from remote.screen import Region, ScreenCapture
from remote.service import RemoteError, RemoteService
from remote.ws_server import WsServer, decode_frame, encode_frame

LINUX_X11 = sys.platform.startswith("linux") and bool(os.environ.get("DISPLAY"))


def run(coro):
    """在同步测试里跑一段协程 (不依赖 pytest-asyncio 的配置)。"""
    return asyncio.run(coro)


# ================================================================ 假后端
#
# 假屏幕 / 假输入统一住在 ``remote.dummy`` 里 —— 那里同一套假件还要给"多机仿真受控端"
# 用 (每台有自己的主机名/尺寸/延迟), 测试只是它的一个使用者。


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
# 假件本身在 remote.dummy 里定义 (上面已 import), 这里只留测试自己的辅助函数。


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

            # 命令 → op 的翻译: 直接喂真实的命令行动词
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


# ================================================================ 多机 / 多操纵端 (P3+)
#
# 真设备只有一台, 所以用 ``remote.dummy`` 起一批**仿真受控端**: 每台有自己的屏幕尺寸、
# 自己的延迟、自己的操作日志, 还能对某台单独注入故障。这样"命令到底发给了谁"是可验证的
# ——全都返回同一个答案的话, 广播和单发就分不出来。
#
# 这些测试**不碰真实光标**: dummy 后面是 FakeMouse / FakeKeyboard。


@pytest.fixture
def swarm():
    """3 台仿真受控端, 跑在独立线程的事件循环里。"""
    herd = dummy.DummySwarm.build(count=3, latency=0.05)
    herd.serve_in_thread()
    try:
        yield herd
    finally:
        herd.shutdown_thread()


def _registry(tmp_path, swarm, group="dummy", transport="ws") -> str:
    path = str(tmp_path / "registry.json")
    swarm.dump_registry(path, group=group, transport=transport)
    return path


def _machine(alias: str, **kwargs) -> ctl.Machine:
    payload = {"transport": "ws", "endpoint": "ws://127.0.0.1:1/"}
    payload.update(kwargs)
    return ctl.Machine(alias=alias, **payload)


def test_dummy_swarm_gives_each_device_its_own_identity(swarm):
    """新颖测试的前提: 每台 dummy 得能分辨 —— 尺寸/屏幕内容/收到的 op 都自己记一份。"""
    sizes = {device.size for device in swarm.devices}
    assert len(sizes) == len(swarm.devices)          # 屏幕尺寸互不相同
    assert len({device.name for device in swarm.devices}) == 3
    assert all(device.ws_port for device in swarm.devices)
    ports = {device.ws_port for device in swarm.devices}
    assert len(ports) == 3                            # 端口也不冲突


def test_multi_machine_broadcast_reaches_every_device(tmp_path, swarm):
    """一主多从: 一条广播命令, N 台**每台都收到一次**。"""
    registry = _registry(tmp_path, swarm)
    assert ctl.main(["ping", "--all", "--registry", registry]) == 0
    for device in swarm.devices:
        assert device.count_op("ping") == 1, f"{device.name} 没收到 ping"


def test_multi_machine_each_device_answers_for_itself(tmp_path, swarm, capsys):
    """广播回来的答案必须**各不相同** —— 否则说明其实是把同一台打了三遍。"""
    registry = _registry(tmp_path, swarm)
    assert ctl.main(["info", "--all", "--registry", registry, "--json"]) == 0
    answers = json.loads(capsys.readouterr().out)
    # 每台自报家门, 一份不多一份不少
    assert {item["result"]["device"] for item in answers} == {d.name for d in swarm.devices}

    # 屏幕尺寸也照自己的来 -- 证明了各拿各的画面参数
    capsys.readouterr()
    assert ctl.main(["op", "screen.size", "--all", "--registry", registry, "--json"]) == 0
    sizes = [tuple(item["result"][key] for key in ("width", "height"))
             for item in json.loads(capsys.readouterr().out)]
    assert len(set(sizes)) == 3


def test_multi_machine_group_casts_only_to_members(tmp_path, swarm):
    """组播: 只有组内的那几台动。"""
    path = str(tmp_path / "registry.json")
    entries = swarm.to_registry()["machines"]          # 先拿到 3 台
    for alias, raw in entries.items():
        raw["groups"] = ["alpha"] if alias in ("dummy1", "dummy2") else ["beta"]
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "machines": entries}, handle)

    assert ctl.main(["ping", "-g", "alpha", "--registry", path]) == 0
    touched = {d.name for d in swarm.devices if d.count_op("ping")}
    assert touched == {"dummy1", "dummy2"}              # dummy3 没被波及

    assert ctl.main(["ping", "-g", "beta", "--registry", path]) == 0
    assert swarm.by_name("dummy3").count_op("ping") == 1


def test_multi_machine_input_lands_on_every_device(tmp_path, swarm):
    """真给每台发输入 op: 每台自己的假鼠标/假键盘都应各自留下痕迹。"""
    registry = _registry(tmp_path, swarm)
    assert ctl.main(["key", "--all", "--registry", registry, "f5"]) == 0
    for device in swarm.devices:
        assert any(event[0] == "tap" for event in device.keyboard.events)

    assert ctl.main(["move", "--all", "--registry", registry, "640", "480"]) == 0
    for device in swarm.devices:
        assert device.mouse.positions[-1] == (640, 480)


def test_multi_machine_one_faulty_device_does_not_sink_the_batch(tmp_path):
    """最后一台注定失败: 整批不能跟着完蛋, 而且**只有它**被标 offline。"""
    herd = dummy.DummySwarm.build(count=3, latency=0.0, fail_ops=("ping",))
    herd.serve_in_thread()
    try:
        registry = str(tmp_path / "registry.json")
        herd.dump_registry(registry)
        broken = herd.devices[-1].name

        assert ctl.main(["ping", "--all", "--registry", registry]) == 1

        states = ctl.Registry.load(registry).data["machines"]
        assert states[broken]["state"] == "offline"
        healthy = [name for name in states if name != broken]
        assert all(states[name]["state"] == "online" for name in healthy)
        # 故障那一台的 error 要能看懂是从哪台来的
        assert broken in states[broken]["last_error"]
    finally:
        herd.shutdown_thread()


def test_multi_machine_concurrent_really_is_concurrent(tmp_path):
    """并发不是口号: 3 台各慢 0.25s, 并发总耗时应明显小于串行累加。"""
    herd = dummy.DummySwarm.build(count=3, latency=0.25)
    herd.serve_in_thread()
    try:
        registry = str(tmp_path / "registry.json")
        herd.dump_registry(registry)

        started = time.perf_counter()
        concurrent_rc = ctl.main(["ping", "--all", "--registry", registry])
        parallel = time.perf_counter() - started

        started = time.perf_counter()
        serial_rc = ctl.main(["ping", "--all", "--registry", registry, "--serial"])
        serial = time.perf_counter() - started

        assert concurrent_rc == serial_rc == 0
        assert parallel < serial * 0.75, (parallel, serial)
    finally:
        herd.shutdown_thread()


# ---------------- 多个操纵端并存 ----------------


def test_registry_concurrent_writes_keep_every_entry(tmp_path):
    """真正的坑: 多个操纵端同时改 registry, 后写的不能把先写的整份冲掉。

    修复前实测丢一半 (50 台并发 add 只剩 26 台): load -> 改 -> save 不是原子操作。
    """
    path = str(tmp_path / "registry.json")
    width = 12

    def add_group(prefix: str) -> None:
        for index in range(width):
            assert ctl.main([
                "machines", "add", f"{prefix}{index}", "--transport", "ws",
                "--endpoint", f"ws://127.0.0.1:{9000 + index}/", "--registry", path,
            ]) == 0

    threads = [threading.Thread(target=add_group, args=(prefix,)) for prefix in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    machines = ctl.Registry.load(path).data["machines"]
    assert len(machines) == 2 * width
    assert all(f"{prefix}{index}" in machines
               for prefix in "ab" for index in range(width))


def test_registry_transaction_does_nothing_when_block_raises(tmp_path):
    """``transaction`` 里抛异常 / return 时不许落盘 —— 校验失败就该什么都不改。"""
    path = str(tmp_path / "registry.json")
    registry = ctl.Registry(path)
    registry.put(_machine("kept"))
    registry.save()

    with pytest.raises(RuntimeError):
        with registry.transaction() as live:
            live.put(_machine("thrown-away"))
            raise RuntimeError("模拟校验失败")

    after = ctl.Registry.load(path).data["machines"]
    assert "kept" in after
    assert "thrown-away" not in after


def test_registry_save_leaves_no_temporary_files(tmp_path):
    path = str(tmp_path / "registry.json")
    registry = ctl.Registry(path)
    registry.put(_machine("a"))
    registry.save()
    registry.put(_machine("b"))
    registry.save()

    leftovers = [name for name in os.listdir(str(tmp_path)) if ".tmp" in name]
    assert leftovers == []
    assert set(ctl.Registry.load(path).data["machines"]) == {"a", "b"}


def test_two_controllers_dispatch_at_the_same_time(tmp_path, swarm):
    """两个操纵端同时对同一批机器下发: 各拿各的结果, registry 不被写坏。"""
    registry = _registry(tmp_path, swarm)
    outcomes: Dict[str, int] = {}

    def controller(name: str) -> None:
        outcomes[name] = ctl.main(["ping", "--all", "--registry", registry])

    threads = [threading.Thread(target=controller, args=(name,)) for name in ("c1", "c2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes == {"c1": 0, "c2": 0}
    for device in swarm.devices:
        assert device.count_op("ping") == 2        # 每个操纵端各来一次

    machines = ctl.Registry.load(registry).data["machines"]
    assert all(raw["state"] == "online" for raw in machines.values())


def test_second_controller_can_use_its_own_aliases(tmp_path, swarm):
    """同一个设备可以在两个操纵端里叫不同的别名 —— 别名是控制端各自的便利叫法。"""
    first = str(tmp_path / "c1.json")
    second = str(tmp_path / "c2.json")
    aliases = {"dummy1": "左", "dummy2": "中", "dummy3": "右"}
    swarm.dump_registry(first, aliases=aliases)
    swarm.dump_registry(second, aliases={name: f"{name}-alt" for name in swarm.endpoints()})

    assert ctl.main(["ping", "左", "--registry", first]) == 0
    assert ctl.main(["ping", "dummy1-alt", "--registry", second]) == 0
    assert swarm.by_name("dummy1").count_op("ping") == 2
    assert swarm.by_name("dummy2").count_op("ping") == 0


# ================================================================ dummy 命令行

def test_dummy_cli_smoke(tmp_path):
    """:mod:`remote.dummy` 的 CLI 参数至少要能被自己的 parser 认下。"""
    args = dummy.build_parser().parse_args([
        "--count", "4", "--transport", "both", "--latency", "0.1",
        "--fail-ops", "mouse.click", "--bootstrap-registry", str(tmp_path / "reg.json"),
    ])
    assert (args.count, args.transport, args.latency) == (4, "both", 0.1)
    assert args.fail_ops == "mouse.click"
# ================================================================ PeerJS (回环验证)
#
# PeerJS 这条链路要真跑通, 需要外网 broker (0.peerjs.com) + WebRTC 打洞, 单机
# CI 里永远做不到。但"我们的代码"和"网络"其实是两层:
#
#   [我们的] 信封协议 / 鉴权 / op 路由 / 大响应分块重组 / 控制端的 peerjs 分支
#   [网络的] broker 注册、ICE 候选交换与打洞、DataChannel 真实吞吐
#
# 下面用一个**内存回环**把前一层完整跑通: 拿一对假 DataConnection 把
# PeerJsServer 与 PeerJsClient 直接对接, 中间的 send() 就是一次函数调用, 不经过
# 任何 Socket。于是除了"网络那一层", 其余都和真机跑的路径完全一致 —— 包括
# ctl 的 peerjs 分支 (下面 test_ctl_peerjs_branch_end_to_end 让 ctl 真以为是 PeerJS)。
#
# 剩下必须真机验证的三件事写在 docs/peerjs-analysis.md 第 5 节。


class _FakeConn:
    """``DataConnection`` 的行为替身: 只实现 send/close, 把消息交给对端。"""

    def __init__(self, peer_id: str = "peer"):
        self.peerId = peer_id
        self.open = True
        self.sent: List[dict] = []
        self.on_data = None

    async def send(self, data):
        if not self.open:
            raise RuntimeError("connection closed")
        self.sent.append(data)
        if self.on_data is not None:
            await self.on_data(data)

    async def close(self):
        self.open = False


class _Loopback:
    """把 PeerJsServer 和 PeerJsClient 背靠背接起来。"""

    def __init__(self, service, token=None, room="TEST01", client=None):
        self.server = PeerJsServer(service, room=room, token=token)
        self.client = client or PeerJsClient(
            f"{PEER_PREFIX}-{room}", token=token, timeout=2.0
        )
        self.server_chunker = PeerJsChunker()
        self.client_conn = _FakeConn("client")
        self.server_conn = _FakeConn("server")
        self.client._conn = self.client_conn
        self.client_conn.on_data = self._to_server
        self.server_conn.on_data = self._to_client
        #: 打开后故意吞掉 end 块, 用来验证"分块不全时是超时而不是拿到错答案"
        self.drop_end = False
        self.dropped = 0

    async def _to_server(self, data):
        msg = self.server_chunker.feed(data)
        if msg is None:
            return
        await self.server._handle(self.server_conn, msg)

    async def _to_client(self, data):
        # sleep(0) 让出一次: 并发请求时两个响应的分块会**交错**到达,
        # 这正是分块 id 必须唯一的场合 (见 split_message 的 _CHUNK_SEQ)
        await asyncio.sleep(0)
        if self.drop_end and data.get("_chunk") == "end":
            self.dropped += 1
            return
        msg = self.client._chunker.feed(data)
        if msg is not None:
            self.client._inbox.put_nowait(msg)


def _noisy_screen(width: int = 240, height: int = 160) -> ScreenCapture:
    """噪点假屏幕: PNG 压不动, 一帧上百 KB —— 用来逼出大响应分块。"""
    import random as _random

    rng = _random.Random(7)
    raw = bytes(rng.randrange(256) for _ in range(width * height * 3))
    screen = ScreenCapture()

    def fake_grab():
        return Image.frombytes("RGB", (width, height), raw)

    screen.grab_image = fake_grab  # type: ignore[method-assign]
    screen.screen_size = lambda *a, **k: (width, height)  # type: ignore[method-assign]
    return screen


def _service(**kwargs) -> RemoteService:
    kwargs.setdefault("screen", fake_screen())
    kwargs.setdefault("controller", fake_controller()[0])
    return RemoteService(**kwargs)


def test_peerjs_loopback_roundtrip_matches_direct_call():
    """第一层证明: 一个 op 走完 PeerJS 全链路, 结果与直接调 service 一致。"""
    service = _service()
    lb = _Loopback(service)

    for op, args in [("ping", {}), ("info", {}), ("mouse.position", {}),
                     ("screen.size", {}), ("keyboard.check", {"keys": ["ctrl", "f5"]})]:
        over_wire = run(lb.client.call(op, args))
        direct = service.handle_request({"op": op, "args": dict(args)})
        # ts / uptime_s 每次都在变, 比对时剔掉; 剩下的键集合必须完全一致
        stable = lambda payload: {k: v for k, v in payload.items()
                                  if k not in ("ts", "uptime_s")}
        assert stable(over_wire) == stable(direct), f"{op}: 回环 {over_wire} != 直调 {direct}"


def test_peerjs_loopback_reaches_the_controller():
    """不只是"有返回值" —— op 真的落到了假鼠标键盘上。"""
    mouse, keyboard = FakeMouse(), FakeKeyboard()
    lb = _Loopback(RemoteService(screen=fake_screen(),
                                 controller=InputController(mouse=mouse, keyboard=keyboard)))

    assert run(lb.client.call("mouse.move", {"x": 120, "y": 80}))["x"] == 120
    assert mouse.positions[-1] == (120, 80)

    run(lb.client.call("keyboard.type", {"text": "hi"}))
    assert ("type", "hi") in keyboard.events

    run(lb.client.call("mouse.scroll", {"dy": 3, "steps": 3}))
    assert sum(abs(dy) for _, dy in mouse.scrolls) == 3


def test_peerjs_screenshot_survives_chunking():
    """大截图必须走分块, 且重组后与服务端原始字节**逐字节相同**。

    这是 PeerJS 最脆弱的一环: fork 把库内分块注释掉了, 全靠应用层这层。
    """
    lb = _Loopback(RemoteService(screen=_noisy_screen(), controller=fake_controller()[0]))

    payload = run(lb.client.screenshot({"format": "png"}))
    assert len(payload) > 60000, f"假屏幕不够大, 没触发分块: {len(payload)}B"

    # 服务端发出的确实是分块序列 (head + N*data + end), 而不是一条整包
    kinds = [part.get("_chunk") for part in lb.server_conn.sent]
    assert kinds[0] == "head" and kinds[-1] == "end"
    assert kinds.count("data") >= 2

    # 与直接抓的对比
    _, direct = lb.server.service.capture({"format": "png"})
    assert payload == direct
    assert lb.client.last_capture["width"] == 240
    assert lb.client.last_capture["format"] == "png"


def test_peerjs_split_message_threshold_and_unique_ids():
    """阈值与分块 id: 两个都曾是隐患 (见 CHUNK_THRESHOLD / _CHUNK_SEQ 的注释)。"""
    small = {"id": 1, "ok": True, "result": {"x": "a" * 5000}}
    assert len(split_message(small)) == 1, "5KB 不该分块"

    # 比单块大、但没到阈值: 也不该拆 —— 这是 CHUNK_THRESHOLD 存在的意义
    # (拿 chunk_size 当阈值的话, 这里会被无谓地拆成两条)
    middle = {"id": 3, "ok": True, "result": {"x": "c" * (CHUNK_SIZE * 5)}}
    assert len(split_message(middle)) == 1

    # 超过阈值才拆, 且每块不超过 chunk_size
    big = {"id": 2, "ok": True, "result": {"x": "b" * (CHUNK_SIZE * 12)}}
    parts = split_message(big)
    assert len(parts) > 1
    assert all(len(p.get("d", "")) <= CHUNK_SIZE for p in parts if p.get("_chunk") == "data")

    # 连开 200 组分块, id 不能重复 —— 撞了就会两组块混进一组然后被丢弃
    ids = [split_message(big)[0]["_id"] for _ in range(200)]
    assert len(set(ids)) == 200

    # 头里必须保留 id/ok, 否则重组端认不出这条响应是回给谁的
    assert {"id": 2, "ok": True}.items() <= parts[0]["header"].items()

    # 完整走一遍重组, 能还原出原始 result
    chunker = PeerJsChunker()
    restored = None
    for part in parts:
        restored = chunker.feed(part) or restored
    assert restored["result"] == big["result"]


def test_peerjs_auth_matrix():
    """三种组合: 都不设 token / 都设且一致 / 服务端设了客户端没给。"""
    # 1) 无 token: 直通
    lb = _Loopback(_service())
    assert run(lb.client.call("ping"))["pong"] is True

    # 2) 有 token 且一致: 先 auth 再正常用 (这里不能调真 connect(), 那会去连 broker)
    authed = _Loopback(_service(), token="s3cret")
    assert run(authed.client.call("auth", {"token": "s3cret"}))["authenticated"] is True
    assert run(authed.client.call("ping"))["pong"] is True

    # 3) 服务端要 token、客户端没给: 第一个 op 就该被顶回来
    lb = _Loopback(_service(), token="s3cret")
    with pytest.raises(RuntimeError) as info:
        run(lb.client.call("ping"))
    assert "unauthorized" in str(info.value)
    assert lb.server_conn.open is False, "鉴权失败后服务端应当断开这条连接"

    # 4) token 不对: 同样被顶回来, 不能因为"带了 token"就放过
    lb = _Loopback(_service(), token="s3cret")
    with pytest.raises(RuntimeError) as info:
        run(lb.client.call("auth", {"token": "wrong"}))
    assert "unauthorized" in str(info.value)


def test_peerjs_reports_remote_error_not_silence():
    """服务端的 RemoteError 要带着 code 传回来, 不能变成"超时"这种假象。"""
    lb = _Loopback(_service())
    with pytest.raises(RuntimeError) as info:
        run(lb.client.call("nope.not_an_op"))
    assert "unknown_op" in str(info.value) or "bad_request" in str(info.value)

    with pytest.raises(RuntimeError) as info:
        run(lb.client.call("mouse.move", {"x": "abc"}))
    assert "bad_request" in str(info.value)


def test_peerjs_batch_and_legacy_kuuki_route():
    """batch 信封与 kuuki 老协议消息都要能被路由到。"""
    lb = _Loopback(_service())

    # batch 列表必须在信封顶层: 塞进 args 只会被当成未知 op
    with pytest.raises(RuntimeError) as info:
        run(lb.client.call("batch", {"batch": [{"op": "ping"}]}))
    assert "unknown_op" in str(info.value)

    result = run(lb.client.batch([{"op": "ping"}, {"op": "screen.size"}]))
    assert [item["ok"] for item in result["results"]] == [True, True]

    # 老协议: 没有 op 字段, 靠 t/mouse/text/key 识别
    enqueued = {"t": "move", "x": 10, "y": 20}
    lb.client._next_id += 1
    req_id = lb.client._next_id
    run(lb.client._conn.send({"id": req_id, **enqueued}))
    answered = run(asyncio.wait_for(lb.client._inbox.get(), timeout=2.0))
    assert answered["id"] == req_id and answered["ok"] is True


def test_peerjs_incomplete_chunks_time_out_instead_of_lying():
    """分块没收齐时, 客户端必须超时报错 —— 绝不能拿半截数据当成功。"""
    lb = _Loopback(RemoteService(screen=_noisy_screen(), controller=fake_controller()[0]))
    lb.drop_end = True

    # py3.10 里 asyncio.TimeoutError 还不是内置 TimeoutError, 两种都要认
    with pytest.raises((TimeoutError, asyncio.TimeoutError)):
        run(lb.client.screenshot({"format": "png"}))
    assert lb.dropped == 1


def test_peerjs_concurrent_big_requests_stay_separate():
    """同一条连接上并发两条大请求: 分块交错, 各拿各的。

    回环里 _to_client 会让出一次, 所以两个响应的块是真的交错的 ——
    这个用例同时压住了"响应 id 匹配"和"分块 id 唯一"两件事。
    """
    service = _service()
    service.handlers["big.left"] = lambda args: {"blob": "L" * 90000}
    service.handlers["big.right"] = lambda args: {"blob": "R" * 90000}
    lb = _Loopback(service)

    async def both():
        return await asyncio.gather(
            lb.client.call("big.left"), lb.client.call("big.right")
        )

    left, right = run(both())
    assert left["blob"] == "L" * 90000
    assert right["blob"] == "R" * 90000


def test_peerjs_fork_api_contract():
    """我们依赖的 fork API 必须还在 —— 换 fork / 升级时第一时间炸出来。"""
    try:
        from peerjs.dataconnection import DataConnection
        from peerjs.enums import ConnectionEventType, PeerEventType
        from peerjs.peer import Peer, PeerOptions
    except ImportError:  # pragma: no cover - fork 不在 sys.path 时
        pytest.skip("peerjs fork 不可用 (需要在项目根目录运行)")

    for name in ("Open", "Close", "Data", "Error"):
        assert hasattr(ConnectionEventType, name), f"ConnectionEventType.{name} 没了"
    for name in ("Open", "Close", "Connection", "Error"):
        assert hasattr(PeerEventType, name), f"PeerEventType.{name} 没了"

    import inspect

    for func in (Peer.start, Peer.connect, Peer.destroy, DataConnection.send,
                 DataConnection.close):
        assert inspect.iscoroutinefunction(func), f"{func} 不再是协程"

    assert "secure" in vars(PeerOptions).get("__annotations__", {}) or hasattr(
        PeerOptions, "secure"
    ), "PeerOptions.secure 没了 (cloud broker 只收 wss)"

    # 单块必须远小于 fork 的缓冲阈值, 否则会撞上它的背压路径
    # (而那条路径是坏的: _tryBuffer 里 await 了个协程却没 await, 见 dataconnection.py)
    assert CHUNK_SIZE * 4 < DataConnection.MAX_BUFFERED_AMOUNT


def test_ctl_validates_endpoint_against_transport():
    """三种传输的地址长得很不一样; 拼错了要当场报错, 别等到下发时才看不懂。"""
    assert ctl.validate_endpoint("ws", "ws://1.2.3.4:8765") is None
    assert ctl.validate_endpoint("ws", "1.2.3.4:8765") is not None
    assert ctl.validate_endpoint("grpc", "1.2.3.4:50051") is None
    assert ctl.validate_endpoint("grpc", "http://1.2.3.4") is not None
    assert ctl.validate_endpoint("peerjs", "kuuki-mouse-ABCDE") is None
    assert ctl.validate_endpoint("peerjs", "ws://broker/kuuki-mouse-ABCDE") is not None
    assert ctl.validate_endpoint("peerjs", "kuuki mouse") is not None
    assert ctl.validate_endpoint("ws", "") is not None


def test_ctl_add_rejects_mismatched_endpoint(tmp_path):
    registry = str(tmp_path / "registry.json")
    assert ctl.main(["machines", "add", "bad", "--transport", "peerjs",
                     "--endpoint", "ws://1.2.3.4:8765", "--registry", registry]) == 2
    assert ctl.main(["machines", "add", "good", "--transport", "peerjs",
                     "--endpoint", "kuuki-mouse-ABCDE", "--registry", registry]) == 0


def test_ctl_gives_peerjs_a_wider_default_timeout(tmp_path):
    registry = str(tmp_path / "registry.json")
    ctl.main(["machines", "add", "w", "--transport", "ws",
              "--endpoint", "ws://1.2.3.4:8765", "--registry", registry])
    ctl.main(["machines", "add", "p", "--transport", "peerjs",
              "--endpoint", "kuuki-mouse-ABCDE", "--registry", registry])
    loaded = ctl.Registry.load(registry)
    assert loaded.get("w").timeout == ctl.DEFAULT_TIMEOUT["ws"]
    # peerjs 的预算里含 broker 注册 + ICE 打洞, 不能按 ws 那套给
    assert loaded.get("p").timeout == ctl.DEFAULT_TIMEOUT["peerjs"] > loaded.get("w").timeout
    # 显式给了就听用户的
    ctl.main(["machines", "add", "p2", "--transport", "peerjs", "--timeout", "5",
              "--endpoint", "kuuki-mouse-FGHIJ", "--force", "--registry", registry])
    assert ctl.Registry.load(registry).get("p2").timeout == 5.0


def test_ctl_peerjs_branch_end_to_end(tmp_path, monkeypatch):
    """控制端的 peerjs 分支: 真的一条 ctl 命令走到底。

    把 ``remote.peerjs_client.PeerJsClient`` 换成接在内存回环上的版本 —— ctl 自己
    不知道, 它照常走 machines add -> 目标解析 -> call_machine/capture_machine ->
    结果汇总 -> 状态回填。这条通了, 就只剩网络那一层没验证。
    """
    import remote.peerjs_client as peerjs_client_module

    service = _service()

    class LoopbackClient(PeerJsClient):
        def __init__(self, peer_id, token=None, secure=True, timeout=30.0,
                     serialization="json"):
            super().__init__(peer_id, token=token, timeout=timeout)

        async def connect(self, timeout=None):
            _Loopback(service, client=self, room=self.peer_id.split("-")[-1])
            self._connected.set()

        async def close(self):
            self._conn = None

    monkeypatch.setattr(peerjs_client_module, "PeerJsClient", LoopbackClient)

    registry = str(tmp_path / "registry.json")
    assert ctl.main(["machines", "add", "px", "--transport", "peerjs",
                     "--endpoint", "kuuki-mouse-ABCDE", "--group", "wan",
                     "--registry", registry]) == 0

    assert ctl.main(["ping", "--all", "--registry", registry]) == 0
    assert ctl.main(["info", "-g", "wan", "--registry", registry]) == 0

    shot = str(tmp_path / "shot.png")
    assert ctl.main(["shot", shot, "--all", "--registry", registry]) == 0
    assert os.path.getsize(shot) > 100

    frames = str(tmp_path / "frames")
    assert ctl.main(["watch", frames, "--all", "--fps", "20", "--count", "2",
                     "--registry", registry]) == 0
    assert sorted(os.listdir(os.path.join(frames, "px"))) == ["0001.png", "0002.png"]

    assert ctl.Registry.load(registry).get("px").state == "online"


# ================================================================ 终端编码
#
# 受控端是要分发到别人机器上的, 那台机器的系统语言不可控。中文 Windows 的代码页能编码
# 中文, 英文 Windows 是 cp1252 —— 一个中文都编码不了。帮助文字 / 错误提示 / --json 里的
# 主机名都有中文, 不处理的话 argparse 打帮助时直接 UnicodeEncodeError, 连 --help 都用不了
# (CI 的 windows-latest 就是英文系统, 第一次构建就撞在这个上)。


def test_force_utf8_stdio_makes_chinese_printable():
    """把 stdout 强制成 UTF-8: 中文能写进去, 而不是抛 UnicodeEncodeError。"""
    import io

    gate = pytest.importorskip("remote.__main__")

    original = sys.stdout
    fake = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    try:
        sys.stdout = fake
        gate.force_utf8_stdio()
        assert sys.stdout.encoding.lower() == "utf-8"
        sys.stdout.write("受控端只支持 Windows")  # 修之前这一步就炸
    finally:
        sys.stdout = original


def test_help_survives_a_non_utf8_console():
    """端到端: cp1252 终端上跑 --help, 要正常退出 (argparse 打印帮助即 SystemExit(0))。"""
    import io

    gate = pytest.importorskip("remote.__main__")

    original = sys.stdout
    try:
        sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        with pytest.raises(SystemExit) as excinfo:
            gate.main(["--help"])
        assert excinfo.value.code == 0
    finally:
        sys.stdout = original


def test_force_utf8_stdio_tolerates_replaced_streams():
    """流被换掉时 (pytest 的 capsys 就没有 reconfigure) 要静默跳过, 不能反过来炸掉测试。"""
    import io

    gate = pytest.importorskip("remote.__main__")

    original = sys.stdout
    try:
        sys.stdout = io.StringIO()  # 没有 reconfigure
        gate.force_utf8_stdio()  # 不抛即为通过
    finally:
        sys.stdout = original


# ---------------- notify (角标通知) ----------------
# 打包后的 exe 排除了 tkinter, 所以这条路在分发版上是走不通的 —— 必须报明确的
# unsupported, 而不是返回 shown=false 让人以为弹了。之前这个 op 一条测试都没有。


def test_notify_without_gui_backend_reports_unsupported(monkeypatch):
    """没有图形环境时要说清楚是 unsupported, 不是假装成功了。"""
    from remote.service import RemoteError, RemoteService

    toast = pytest.importorskip("remote.toast")
    monkeypatch.setattr(toast, "notify_supported", lambda: False)

    service = RemoteService(screen=fake_screen())
    with pytest.raises(RemoteError) as excinfo:
        service.handle("notify", {"message": "hello"})
    assert excinfo.value.code == "unsupported"


def test_notify_shows_when_backend_available(monkeypatch):
    """有图形环境时真的去弹, 并把结果照实报回来。"""
    from remote.service import RemoteService

    toast = pytest.importorskip("remote.toast")
    calls = []
    monkeypatch.setattr(toast, "notify_supported", lambda: True)
    monkeypatch.setattr(
        toast,
        "notify",
        lambda message, detail="", seconds=6.0, corner="br", width=420: (
            calls.append((message, detail, seconds, corner)) or True
        ),
    )

    service = RemoteService(screen=fake_screen())
    result = service.handle("notify", {"message": "hi", "detail": "d", "corner": "tl"})
    assert result["shown"] is True
    assert result["corner"] == "tl"
    assert calls == [("hi", "d", 6.0, "tl")]


def test_notify_validates_arguments(monkeypatch):
    from remote.service import RemoteError, RemoteService

    toast = pytest.importorskip("remote.toast")
    monkeypatch.setattr(toast, "notify_supported", lambda: True)

    service = RemoteService(screen=fake_screen())
    with pytest.raises(RemoteError) as excinfo:
        service.handle("notify", {})
    assert excinfo.value.code == "bad_request"

    with pytest.raises(RemoteError) as excinfo:
        service.handle("notify", {"message": "hi", "corner": "middle"})
    assert excinfo.value.code == "bad_request"
