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

from remote.input import InputController, KeyUnsupported, resolve_key
from remote.screen import Monitor, Region, ScreenCapture
from remote.service import RemoteError, RemoteService
from remote.ws_server import WsServer, decode_frame, encode_frame

LINUX_X11 = sys.platform.startswith("linux") and bool(os.environ.get("DISPLAY"))


def run(coro):
    """在同步测试里跑一段协程 (不依赖 pytest-asyncio 的配置)。"""
    return asyncio.run(coro)


# ================================================================ 假后端


class FakeBackend:
    """内存里的假屏幕: 200x100, 在 (50,25) 放一个白点。"""

    name = "fake"
    priority = 1

    def available(self):
        return True, ""

    def screen_size(self):
        return 200, 100

    def monitors(self):
        return [Monitor(0, 0, 0, 200, 100, primary=True, name="fake")]

    def grab_image(self):
        image = Image.new("RGB", (200, 100), (10, 20, 30))
        image.putpixel((50, 25), (255, 255, 255))
        return image


def fake_screen() -> ScreenCapture:
    screen = ScreenCapture(backend="auto")
    screen._catalog = [FakeBackend()]  # type: ignore[list-item]
    screen._active = None
    screen._failed = {}
    return screen


def has_real_backend() -> bool:
    try:
        ScreenCapture(backend="auto").active_backend()
        return True
    except Exception:
        return False


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
        assert capture.backend == "fake"
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
    assert info["monitors"][0]["width"] == 200
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
