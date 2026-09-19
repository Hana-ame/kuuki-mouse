# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec —— 构建 **受控端** ``kuuki-agent.exe`` (仅 Windows)。

定位: 控制端 (``-m remote.ctl`` / ``-m remote.client``) 保持源码运行, 不打进这个包。
受控端才是要分发到"被操作的那台机器"上、机器上往往没有 Python 的那个。

构建::

    pyinstaller packaging/kuuki-agent.spec --noconfirm

产物在 ``dist/kuuki-agent/`` (onedir, 不是 onefile) —— 故意选 onedir:
aiortc/av 有一堆 DLL, onefile 每次启动都要把几百 MB 解到临时目录, 冷启动慢得没法用。

为什么需要这一大堆 hiddenimports: 项目里有三处**运行期才 import** 的地方,
PyInstaller 的静态分析看不见, 打出来的 exe 会在用到时才 ModuleNotFoundError:

  1. ``remote/peerjs_*.py`` 在函数内 ``from peerjs.peer import Peer`` —— peerjs 是
     项目里 fork 的一份 (``peerjs/`` 目录, 无 ``__init__.py``, 隐式命名空间包),
     PyInstaller 的 collect_submodules 对命名空间包不保证奏效, 所以显式列全。
  2. ``remote/service.py`` 的 ``from app import handle_message`` (kuuki 透传)。
  3. ``remote/input.py`` 的 ``from controller import PynputMouseController``。

另外 grpc / av / aioice 这些带 C 扩展的包, hook 覆盖不全, 一并补。
"""

import os

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

# SPECPATH 由 PyInstaller 注入, 指向本文件所在目录 (packaging/)。仓库根是它的上一级。
ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))


def _collect(name):
    """collect_submodules 的容错版: 命名空间包 / 未安装的包会抛异常, 返回空即可。"""
    try:
        return collect_submodules(name)
    except Exception:  # noqa: BLE001 - 构建期尽力而为
        return []


def _libs(name):
    try:
        return collect_dynamic_libs(name)
    except Exception:  # noqa: BLE001
        return []


def _data(name):
    try:
        return collect_data_files(name)
    except Exception:  # noqa: BLE001
        return []


# 项目内模块: remote 包 + 运行期惰性 import 的几个仓库根模块
project_hidden = (
    _collect("remote")
    + [
        # remote/service.py 的 kuuki 透传 (函数内 import, 静态分析抓不到)
        "app",
        "attitude",
        # remote/input.py 的鼠标控制器 (同上)
        "controller",
    ]
)

# peerjs fork: 无 __init__.py 的命名空间包, 显式列全, 别指望 collector
peerjs_hidden = [
    "peerjs.api",
    "peerjs.baseconnection",
    "peerjs.dataconnection",
    "peerjs.enums",
    "peerjs.negotiator",
    "peerjs.peer",
    "peerjs.peerroom",
    "peerjs.servermessage",
    "peerjs.socket",
    "peerjs.util",
    "peerjs.ext.http_proxy",
]

# peerjs fork 拉进来的 WebRTC 栈
webrtc_hidden = (
    _collect("aiortc")
    + _collect("av")
    + _collect("aioice")
    + [
        "pyee",
        "pyee.asyncio",
        "dataclasses_json",
        "loguru",
        "yaml",
        "aiohttp",
        "cryptography",
        "OpenSSL",
    ]
)

# 传输层: gRPC 的 C 扩展与 protobuf 运行时
transport_hidden = [
    "grpc",
    "grpc._cython.cygrpc",
    "grpc._cython",
    "google.protobuf",
    "google.protobuf.internal",
    "websockets",
    "websockets.legacy",
]

# 屏幕 + 输入: ImageGrab 走 GDI, pynput 的 win32 后端是运行期按平台选的
platform_hidden = [
    "PIL",
    "PIL.Image",
    "PIL.ImageGrab",
    "PIL.ImageOps",
    "pynput",
    # pynput 的 win32 后端是运行期按 sys.platform 选的, 静态分析选不出来
    "pynput.keyboard._win32",
    "pynput.mouse._win32",
    "pynput._util.win32",
    # 注意: 别加 pywin32 (win32api/win32con/win32gui) —— 项目不用它, pynput 走 ctypes,
    # 加了只会在没装 pywin32 的机器上刷一串 "Hidden import not found"。
]

hiddenimports = list(
    dict.fromkeys(  # 去重且保持顺序
        project_hidden + peerjs_hidden + webrtc_hidden + transport_hidden + platform_hidden
    )
)

binaries = _libs("av") + _libs("aiortc") + _libs("aioice") + _libs("grpc")
datas = _data("grpc") + _data("av")

a = Analysis(
    # 相对路径是相对 **spec 所在目录** (packaging/) 解析的, 不是 CWD —— 用绝对路径免得误解
    [os.path.join(SPECPATH, "agent_entry.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 打包机/开发期才用的, 别进产物
        "pytest",
        "_pytest",
        "tkinter",
        "IPython",
        "notebook",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="kuuki-agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # 受控端是命令行程序: 要 --json 端点输出, 要 Ctrl+C 停
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="kuuki-agent",
)
