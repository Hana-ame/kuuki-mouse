"""``kuuki-agent.exe`` 的 PyInstaller 入口。

等价于源码运行时的 ``python -m remote`` —— 受控端 (被操作的那台机器) 的三种传输
(WebSocket / gRPC / PeerJS), **默认只开 PeerJS**, 其余按需 ``--ws`` / ``--grpc`` 开启。
命令行参数与 ``python -m remote`` 完全一致::

    kuuki-agent.exe                     # 只开 PeerJS (默认)
    kuuki-agent.exe --ws --grpc         # 本机两个端口
    kuuki-agent.exe --token secret --allow-remote
    kuuki-agent.exe --selftest

为什么单独要一个入口文件, 而不是直接拿 ``remote/__main__.py`` 当脚本:
``__main__.py`` 顶部有一段"允许 ``python remote/__main__.py`` 直跑"的 sys.path 补丁,
作为脚本入口时 ``__package__`` 为空, 补丁里的相对路径语义和打包后不一致。这里显式
从包里 import, 语义唯一。

打包: 见同目录 ``kuuki-agent.spec`` 与 ``docs/pyinstaller-ci.md``。
"""

from __future__ import annotations

import sys

from remote.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
