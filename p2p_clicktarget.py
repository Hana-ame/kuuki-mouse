"""点击靶测试: 起一个本地 HTTP 页面, 用 PeerJS 控制鼠标点它, 由服务端记录结果。

为什么要有这个
--------------
之前拿 DSH 界面当试验场, 有两个问题:
  1. 侵入 —— 在用户真实工作的页面上乱点
  2. 难验证 —— "消息有没有发出去"要肉眼看截图猜

这里改成自己的靶页:
  * 纯品红(#ff00ff)大按钮 —— 截图里做像素分析能精确定位到像素
  * 点了会 POST /click, 服务端写进 clicks.jsonl
  * 控制端读那个文件就知道点击有没有真的生效, 不用猜

关键: 截图**不缩放** (不传 max_width), 坐标即实际屏幕坐标, 避免比例换算误差。
"""

import asyncio
import http.server
import io
import json
import logging
import os
import sys
import threading
import time

sys.path.insert(0, r"D:\Workplace\kuuki-mouse")
logging.basicConfig(level=logging.WARNING)

from PIL import Image  # noqa: E402

from remote.peerjs_client import PeerJsClient  # noqa: E402
from remote.peerjs_server import PeerJsServer, gen_room_code  # noqa: E402
from remote.service import RemoteService  # noqa: E402

OUT = r"D:\Workplace\kuuki-mouse"
PORT = 8799
CLICKS = os.path.join(OUT, "_clicks.jsonl")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=OUT, **kw)

    def do_POST(self):
        if self.path != "/click":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        with open(CLICKS, "a", encoding="utf-8") as fh:
            fh.write(body + "\n")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *a):  # 静音
        pass


def find_magenta(png_bytes):
    """在截图里找品红按钮, 返回 (中心x, 中心y, 包围盒, 图尺寸)。

    不缩放截图, 所以返回的坐标可直接当屏幕坐标用。
    """
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    w, h = im.size
    xs, ys = [], []
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            r, g, b = im.getpixel((x, y))
            if r > 200 and g < 90 and b > 200:      # 品红
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return (
        (min(xs) + max(xs)) // 2,
        (min(ys) + max(ys)) // 2,
        (min(xs), max(xs), min(ys), max(ys)),
        (w, h),
        len(xs),
    )


async def main():
    # 清空旧的点击记录
    if os.path.exists(CLICKS):
        os.remove(CLICKS)

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"[http] http://127.0.0.1:{PORT}/click.html", flush=True)

    server = PeerJsServer(RemoteService(), room=gen_room_code())
    await server.start()
    if not await server.wait_ready(30):
        print("broker 注册失败", flush=True)
        return 1
    print(f"[peerjs] {server.peer_id}", flush=True)

    client = PeerJsClient(server.peer_id, timeout=90)
    await client.connect(timeout=90)
    print("[client] connected", flush=True)

    try:
        size = await client.call("screen.size", {})
        sw, sh = size["width"], size["height"]
        print(f"[screen] {sw}x{sh}", flush=True)

        await client.call("notify", {
            "message": "kuuki-mouse 点击靶测试",
            "detail": "会用浏览器打开靶页, 然后移动鼠标点它。", "seconds": 6,
        })

        # 用浏览器打开靶页 (用系统默认浏览器, 不抢我们的控制流程)
        import subprocess
        subprocess.Popen(
            ["cmd", "/c", "start", "", f"http://127.0.0.1:{PORT}/click.html"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print("[browser] 已打开靶页, 等 6 秒", flush=True)
        await asyncio.sleep(6)

        # 无缩放截图 -> 坐标可直接用
        png = await client.screenshot({"format": "png"})
        with open(f"{OUT}\\_click_1_full.png", "wb") as fh:
            fh.write(png)
        hit = find_magenta(png)
        if not hit:
            print("[!] 截图里没找到品红按钮", flush=True)
            return 1
        cx, cy, box, dims, npx = hit
        print(f"[target] 屏幕坐标 ({cx},{cy})  包围盒 {box}  图尺寸 {dims}  像素数 {npx}", flush=True)

        # 移动并点击
        print(f"[step] move -> ({cx},{cy})", flush=True)
        await client.call("mouse.move", {"x": cx, "y": cy, "duration": 0.25})
        await asyncio.sleep(0.4)
        pos = await client.call("mouse.position", {})
        print(f"[step] cursor now {pos}", flush=True)

        print("[step] click", flush=True)
        await client.call("mouse.click", {"button": "left"})
        await asyncio.sleep(1.2)

        png2 = await client.screenshot({"format": "png"})
        with open(f"{OUT}\\_click_2_after.png", "wb") as fh:
            fh.write(png2)

        # 读服务端记录, 机器判定点击是否生效
        clicks = []
        if os.path.exists(CLICKS):
            with open(CLICKS, encoding="utf-8") as fh:
                clicks = [json.loads(line) for line in fh if line.strip()]
        print(f"[verify] 服务端收到 {len(clicks)} 次点击上报", flush=True)
        for c in clicks:
            print(f"         {c}", flush=True)
        print(f"[result] {'CLICK OK' if clicks else 'CLICK FAILED'}", flush=True)
    finally:
        await client.close()
        await server.close()
        httpd.shutdown()
    print("DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
