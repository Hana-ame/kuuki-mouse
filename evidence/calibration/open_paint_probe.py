"""打开画图的几条路都试一遍, 看这台机器上哪条通。

为什么写成文件: 每台的"启动菜单吃掉输入 / Win+R 被安全策略拦 / 只有某种终端
能起进程"都不一样, 需要反复组合着试 —— 内联脚本一改就重打一遍, 不如留下百度云。
"""

import asyncio

from remote.client import WsClient

URL = "ws://127.0.0.1:8765/"


async def has(client, process: str) -> dict:
    found = await client.call("window.list", {"process": process})
    return found


async def route_run_paste(client) -> bool:
    """Win+R -> **粘贴**路径 -> 回车。逐字输入在这台机器上会被吞, 粘贴不会。"""
    print("\n-- 路线1: Win+R + keyboard.paste")
    await client.call("keyboard.hotkey", {"keys": "win+r"})
    await asyncio.sleep(1.2)
    await client.call("keyboard.paste", {"text": r"C:\Windows\System32\mspaint.exe"})
    await asyncio.sleep(0.6)
    await client.call("keyboard.key", {"key": "enter"})
    await asyncio.sleep(3.5)
    return bool((await has(client, "mspaint"))["count"])


async def route_shell(client) -> bool:
    """已有终端窗口的话, 直接在里面敲 —— 终端总是收得下键盘输入。"""
    for process in ("powershell", "cmd", "ubuntu", "WindowsTerminal"):
        found = await has(client, process)
        if not found["count"]:
            continue
        print(f"\n-- 路线2: 在 {process} 窗口里敲命令")
        focused = await client.call("window.focus", {"process": process})
        print(f"   focus={focused.get('focused')} method={focused.get('method')} "
              f"title={focused.get('title')[:40]!r}")
        await asyncio.sleep(0.6)
        await client.call("keyboard.type", {"text": "mspaint", "interval": 0.05})
        await asyncio.sleep(0.4)
        await client.call("keyboard.key", {"key": "enter"})
        await asyncio.sleep(3.5)
        found = await has(client, "mspaint")
        print(f"   mspaint 窗口数: {found['count']}")
        if found["count"]:
            for window in found["windows"]:
                print(f"   - {window['title'][:44]!r} rect={window['rect']}")
            return True
    return False


async def main() -> int:
    async with WsClient(URL, timeout=30) as client:
        existing = await has(client, "mspaint")
        print(f"一开始就有 {existing['count']} 个画图窗口")
        if existing["count"]:
            return 0
        if await route_run_paste(client):
            print("\n路线1 成了")
            return 0
        if await route_shell(client):
            print("\n路线2 成了")
            return 0
        print("\n两条都没成")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
