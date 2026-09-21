"""用本机的 remote 受控端打开一个网址, 截图 + OCR 看看上面有什么。

本机就是 Windows 受控端, 所以直接 import 控制器, 不起网络服务 (省掉端口与
进程存活期的麻烦)。

为什么地址栏用 paste 而不是 type: 中文输入法开着时, keytype 打的 ASCII 会进
输入法的候选框, 最后那个 Enter 上屏的是候选词而不是"导航到这个网址"。
paste 走剪贴板 + Ctrl+V, 不受输入法影响。
"""

import base64
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from remote.input import InputController  # noqa: E402
from remote.screen import ScreenCapture  # noqa: E402
from remote.service import RemoteService  # noqa: E402

URL = "https://stage1st.com/2b/forum.php"
SHOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stage1st.png")
BROWSERS = ("msedge", "chrome", "firefox", "brave", "opera", "vivaldi", "iexplore")


def pick_browser(service: RemoteService):
    """桌面上有没有已经开着的浏览器窗口 (只挑有标题的, 空标题是各种隐形壳)。

    走 window.list op 而不是直接调 remote.window: 这台机器既当受控端又当控制端,
    所有动作理应经过同一份 op 实现 —— 好处是这段脚本换个 target 就能远程跑。
    """
    try:
        windows = service.handle("window.list", {}).get("windows") or []
    except Exception as exc:  # pragma: no cover
        print("列窗口失败:", exc)
        return None
    for item in windows:
        proc = str(item.get("process") or "").lower()
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        if any(name in proc for name in BROWSERS):
            return item
    return None


def main() -> int:
    service = RemoteService(screen=ScreenCapture(), controller=InputController())

    target = pick_browser(service)
    if target is None:
        print("没有正在跑的浏览器 —— 用 Win+R 让默认浏览器打开")
        service.handle("keyboard.hotkey", {"keys": ["win", "r"]})
        time.sleep(0.8)
        service.handle("keyboard.paste", {"text": URL})
        time.sleep(0.3)
        service.handle("keyboard.key", {"key": "enter"})
    else:
        print("聚焦已有浏览器:", target.get("process"), "|", target.get("title")[:60])
        service.handle("window.focus", {"hwnd": target["hwnd"]})
        time.sleep(0.8)
        service.handle("keyboard.hotkey", {"keys": ["ctrl", "t"]})  # 新标签页
        time.sleep(0.6)
        service.handle("keyboard.hotkey", {"keys": ["ctrl", "l"]})  # 定位地址栏
        time.sleep(0.4)
        service.handle("keyboard.paste", {"text": URL})
        time.sleep(0.3)
        service.handle("keyboard.key", {"key": "enter"})

    print("等页面加载 ...")
    time.sleep(7)

    fg = service.handle("window.foreground", {})
    print("前台窗口:", str(fg.get("title"))[:70], "|", fg.get("process"))

    # 截图也走 op: op 返回的就是 base64, 解码即 PNG 字节 —— 顺手验证了"这条 op
    # 真的能出图", 而不是只有 diagnose 脚本自己拼得出来。
    shot = service.handle("screen.screenshot", {})
    with open(SHOT, "wb") as handle:
        handle.write(base64.b64decode(shot["image_b64"]))
    print(
        "截图:",
        SHOT,
        "%dx%d %s %d bytes"
        % (shot.get("width", 0), shot.get("height", 0), shot.get("format"), len(shot["image_b64"])),
    )

    try:
        result = service.handle("screen.ocr", {})
    except Exception as exc:
        print("OCR 失败:", exc)
        return 0
    print("识别出 %d 行" % result.get("count", 0))
    for line in (result.get("lines") or [])[:25]:
        rect = line.get("screen") or {}
        print(
            "  (%4d,%4d) %s"
            % (rect.get("x", 0), rect.get("y", 0), line.get("text", "")[:70])
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
