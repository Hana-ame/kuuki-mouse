"""检查 PyInstaller 产物里**惰性导入**的依赖是否真的打进去了。

为什么需要它: 项目里有几处 import 发生在函数体内 (``remote/peerjs_*.py`` 里的
``from peerjs.peer import Peer``、``remote/service.py`` 的 ``from app import handle_message``、
``remote/input.py`` 的 ``from controller import PynputMouseController``), PyInstaller 的静态
分析看不见它们。``--help`` / ``--version`` 这类冒烟根本走不到这些分支 —— exe 看着是好的,
用户一开 PeerJS 就 ModuleNotFoundError。

它读的是构建中间产物 ``PYZ-00.toc`` 里记录的模块清单, 不需要屏幕、不需要网络,
所以在 CI runner 上也稳定可跑。

用法::

    python packaging/check_bundle.py build/agent/kuuki-agent
    python packaging/check_bundle.py --json build/agent/kuuki-agent

退出码: 0 全部命中 / 1 有缺失 / 2 读不出 TOC (构建目录不对或 PyInstaller 换了格式)
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys

#: 必须出现在产物里的模块。分三组, 缺哪组对应哪条链路会炸。
REQUIRED = {
    "远程核心": ["remote", "remote.service", "remote.screen", "remote.ws_server",
                 "remote.grpc_server", "remote.peerjs_server"],
    # 全是运行期惰性 import 的, 静态分析抓不到 —— 本脚本存在的理由
    "惰性导入": ["peerjs", "peerjs.peer", "peerjs.dataconnection", "peerjs.api",
                 "app", "controller", "attitude"],
    # WebRTC 栈: PeerJS 传输要连 broker 才用得上
    "WebRTC": ["aiortc", "av", "aioice", "pyee", "aiohttp"],
    "传输与平台": ["grpc", "google.protobuf", "websockets", "PIL", "PIL.ImageGrab",
                   "pynput", "pynput.mouse._win32", "pynput.keyboard._win32"],
}


def load_modules(toc_path: str) -> set[str]:
    """从 PYZ 的 TOC 里读出所有打包进去的模块名。

    TOC 是 PyInstaller 写出的 **repr 文本** ``(pyz_path, [(name, ...), ...])``
    (不是 pickle, 虽然长得像 protocol 0), 所以用 ``literal_eval``; pickle 留作回退。
    这是 PyInstaller 的内部实现细节, 换了版本可能变 —— 所以调用方要把读不出来
    当成"跳过检查"而不是"构建失败"。
    """
    raw = open(toc_path, encoding="utf-8", errors="replace").read()
    try:
        data = ast.literal_eval(raw)
    except Exception:  # noqa: BLE001 - 万一以后真变成 pickle
        import pickle

        with open(toc_path, "rb") as handle:
            data = pickle.load(handle)
    entries = data[1] if isinstance(data, tuple) and len(data) > 1 else data
    names = set()
    for entry in entries:
        if isinstance(entry, (tuple, list)) and entry:
            names.add(str(entry[0]))
        elif isinstance(entry, str):
            names.add(entry)
    return names


def find_toc(build_dir: str) -> str | None:
    """在构建目录里找 PYZ-*.toc (名字带编号, 别写死)。"""
    if not os.path.isdir(build_dir):
        return None
    candidates = sorted(
        os.path.join(build_dir, name)
        for name in os.listdir(build_dir)
        if name.startswith("PYZ-") and name.endswith(".toc")
    )
    return candidates[-1] if candidates else None


def _force_utf8_stdio() -> None:
    """跟 ``remote/__main__.py`` 里那个是同一个坑, 本脚本自己也踩了:

    分组名是中文 ("惰性导入" / "传输与平台"), 而英文 Windows 的控制台代码页是 cp1252,
    一个中文都编码不了 —— CI 的 windows-latest 上, 脚本还没检查东西就先
    UnicodeEncodeError 崩在 print 上了。所以打印之前先把流强制成 UTF-8。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # 被替换过的流 (capsys) 没有这个方法
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # noqa: B014 - 流已关闭 / detach
            pass


def main(argv=None) -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(description="检查 PyInstaller 产物里的惰性依赖")
    parser.add_argument(
        "build_dir",
        nargs="?",
        default=os.path.join("build", "agent", "kuuki-agent"),
        help="PyInstaller 的构建中间目录 (含 PYZ-*.toc)",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args(argv)

    toc = find_toc(args.build_dir)
    if toc is None:
        msg = f"找不到 PYZ-*.toc: {args.build_dir} (先跑 pyinstaller, 或路径给错了)"
        print(msg, file=sys.stderr)
        if args.json:
            print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
        return 2

    try:
        modules = load_modules(toc)
    except Exception as exc:  # noqa: BLE001 - TOC 格式变了不该让构建挂掉
        msg = f"读不出 {toc}: {exc.__class__.__name__}: {exc}"
        print(msg, file=sys.stderr)
        if args.json:
            print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
        return 2

    report = {}
    missing_total = []
    for group, names in REQUIRED.items():
        present = [n for n in names if n in modules]
        missing = [n for n in names if n not in modules]
        report[group] = {"present": present, "missing": missing}
        missing_total.extend(missing)
        if not args.json:
            flag = "ok  " if not missing else "MISS"
            print(f"  {flag} {group}: {len(present)}/{len(names)}")
            for name in missing:
                print(f"        缺 {name}")

    ok = not missing_total
    if args.json:
        print(json.dumps({"ok": ok, "toc": toc, "total": len(modules),
                          "groups": report, "missing": missing_total},
                         ensure_ascii=False, indent=2))
    else:
        print(f"\n共 {len(modules)} 个模块; 结果: {'PASS' if ok else 'FAIL'}")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
