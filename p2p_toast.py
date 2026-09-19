"""通过 PeerJS 控制 Windows 发消息 —— 每步都在被控端弹无焦点角标说明在做什么。

与前几版的区别:
  1. 每个动作前调 ``notify`` 角标, 在被控端屏幕上显示当前步骤 (不抢焦点)
  2. 发送用 ``Ctrl+Enter`` (DSH 生成中时蓝色按钮是「停止生成」, 不是发送)
  3. 输入用剪贴板粘贴 (``keyboard.paste``) 而不是逐字符按键 —— 绕开中文输入法
"""

import asyncio
import logging
import sys

sys.path.insert(0, r"D:\Workplace\kuuki-mouse")
logging.basicConfig(level=logging.WARNING)

from remote.peerjs_client import PeerJsClient  # noqa: E402
from remote.peerjs_server import PeerJsServer, gen_room_code  # noqa: E402
from remote.service import RemoteService  # noqa: E402

OUT = r"D:\Workplace\kuuki-mouse"
TEXT = "kuuki-mouse PeerJS test: screenshot -> click -> paste -> send (Ctrl+Enter)."
INPUT_XY = (700, 903)


async def toast(client, message, detail="", seconds=5, corner="br"):
    """在被控端弹角标; 失败不影响主流程。"""
    try:
        await client.call("notify", {"message": message, "detail": detail,
                                     "seconds": seconds, "corner": corner})
    except Exception as exc:  # noqa: BLE001
        print(f"    (角标失败: {exc})", flush=True)


async def shot(client, name):
    data = await client.screenshot({"format": "png", "max_width": 1200})
    with open(f"{OUT}\\_toast_{name}.png", "wb") as fh:
        fh.write(data)
    print(f"    [shot] {name} {len(data)}B", flush=True)
    return data


async def main():
    server = PeerJsServer(RemoteService(), room=gen_room_code())
    await server.start()
    if not await server.wait_ready(30):
        print("broker 注册失败", flush=True)
        return 1
    print(f"[server] {server.peer_id}", flush=True)

    client = PeerJsClient(server.peer_id, timeout=90)
    await client.connect(timeout=90)
    print("[client] connected", flush=True)

    try:
        await toast(client, "kuuki-mouse 已连接", "接下来会通过 PeerJS 操作鼠标键盘。", 5)
        await asyncio.sleep(0.5)

        await toast(client, "步骤 1/4：截屏", "读取当前屏幕，用于定位输入框。", 4)
        await shot(client, "1_start")

        await toast(client, "步骤 2/4：点击输入框", f"光标移到 {INPUT_XY} 并单击。", 4)
        await client.call("mouse.move", {"x": INPUT_XY[0], "y": INPUT_XY[1], "duration": 0.2})
        await asyncio.sleep(0.3)
        await client.call("mouse.click", {"button": "left"})
        await asyncio.sleep(0.5)

        await toast(client, "步骤 3/4：输入文本", "用剪贴板粘贴（绕开输入法）。", 4)
        await client.call("keyboard.hotkey", {"keys": ["ctrl", "a"]})
        await asyncio.sleep(0.2)
        await client.call("keyboard.key", {"key": "delete"})
        await asyncio.sleep(0.2)
        pasted = await client.call("keyboard.paste", {"text": TEXT})
        print(f"    paste -> {pasted}", flush=True)
        await asyncio.sleep(0.8)
        await shot(client, "2_pasted")

        await toast(client, "步骤 4/4：Ctrl+Enter 发送", "生成中时按钮是「停止生成」，所以用快捷键。", 5)
        await client.call("keyboard.hotkey", {"keys": ["ctrl", "enter"]})
        await asyncio.sleep(2.0)
        await shot(client, "3_after_send")

        await toast(client, "kuuki-mouse 操作完成", "本次流程结束。", 5)
        print("[done]", flush=True)
    finally:
        await client.close()
        await server.close()
    print("DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
