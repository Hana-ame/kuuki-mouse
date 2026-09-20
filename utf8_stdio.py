"""把 stdout / stderr 强制成 UTF-8 —— 任何要打印中文的入口, 在 print 之前调一下。

为什么需要它
------------
Windows 控制台的编码由**系统语言**决定, 英文版是 cp1252, 一个中文都编码不了。
而本仓库的输出大量含中文: 帮助文字、日志、测试结果、文档里的命令行。于是一个
普通的 ``print`` 就会::

    UnicodeEncodeError: 'charmap' codec can't encode characters

**只在非中文系统上崩** —— 中文 Windows (cp936) 永远复现不出来, 所以靠本地开发
发现不了, 只有英文环境的 CI / 英文系统的用户会撞上。

已经踩过四次 (都是英文 windows-latest CI 抓出来的):

  1. ``remote/__main__.py`` —— ``--help`` 打中文帮助, 用户连帮助都看不到
  2. ``packaging/check_bundle.py`` —— 分组名 ("惰性导入") 没打印出来就先崩了
  3. ``check_ctl_docs.py`` —— 文档命令带中文注释 ("ping all  # 广播")
  4. ``test_attitude.py`` —— 测试结果里的 "[PASS]" 与 "全部通过"

所以这里是**唯一一份实现**, 上面四处都 import 它, 别再各写一份。

用法
----
必须排在**第一次输出之前** —— argparse 的 ``--help`` / ``--version`` 是在
``parse_args`` 内部输出并退出的, 调用晚了照样崩::

    from utf8_stdio import force_utf8_stdio
    force_utf8_stdio()
    args = build_parser().parse_args(argv)

打包注意
--------
``packaging/kuuki-agent.spec`` 的 hiddenimports 里有 ``utf8_stdio`` —— 它是仓库根的
顶层模块, 不是 remote 包的子模块, PyInstaller 不会自动跟着 remote 走。
"""

from __future__ import annotations

import sys

__all__ = ["force_utf8_stdio"]


def force_utf8_stdio() -> None:
    """把 stdout / stderr 切到 UTF-8。失败就静默放过, 不因为换个流就崩。

    ``errors="replace"`` 是最后一道保险: 万一终端编码真不支持, 也好歹把命令跑完,
    而不是抛异常退出 —— 用户拿到的是能用的程序, 不是一句 traceback。
    """
    for stream in (sys.stdout, sys.stderr):
        # 被 pytest 的 capsys 之类换掉的流没有 reconfigure, 跳过即可
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - 流已关闭 / detach
            pass
