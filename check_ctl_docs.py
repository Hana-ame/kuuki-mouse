"""用真 parser 校验 README / docs 里出现的 ``remote.ctl`` 命令。

文档比代码更容易过期 —— 命令表、复现指南里抄着几十条命令行, parser 一改它们
就悄悄失真, 而 markdown 不会报错。这个脚本把文档里的命令喂给真正的
``remote.ctl.build_parser()``, 认不下来的直接失败。

只对**代码块里**的命令做校验。

理由: 正文里的命令往往是叙述的一部分 —— 比如「写 `...shot out.png self` 会报错」
这种反例 —— 它们故意是错的, 不该参与校验。代码块里的才是能照抄执行的。
宁可漏掉正文里的命令, 也不要把反例误报成失败。
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from remote.__main__ import force_utf8_stdio  # noqa: E402
from remote.ctl import build_parser, _fill_defaults  # noqa: E402

ROOT = Path(__file__).resolve().parent

# 会展开成 remote.ctl 的写法: 文档里有用裸 python 的, 也有用 $PY 变量占位解释器的
INVOCATIONS = ("python -m remote.ctl", "$PY -m remote.ctl")

SKIP_TOKENS = ("<", "{", "|")


def clean(line: str) -> str:
    """把一行 markdown 里的命令行剥出来 (去掉列表符、提示符、行尾注释)。"""
    line = re.sub(r"^[-*>]\s*", "", line)
    line = re.sub(r"^\$\s*", "", line)
    line = re.split(r"\s+#", line)[0]
    return line.strip()


def extract(line: str) -> str | None:
    for prefix in INVOCATIONS:
        index = line.find(prefix)
        if index >= 0:
            tail = line[index + len(prefix):].strip()
            # 续行符后面的内容是 shell 拼接, 不当命令看
            tail = tail.split("\\")[0].strip()
            return tail
    return None


def main() -> int:
    docs = [ROOT / "README.md", ROOT / "remote" / "README.md"]
    docs += sorted((ROOT / "docs").glob("*.md"))

    total = failed = 0
    for path in docs:
        if not path.exists():
            continue
        relative = path.relative_to(ROOT)
        fenced = False
        for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = raw.strip()
            if stripped.startswith("```") or stripped.startswith("~~~"):
                fenced = not fenced
                continue
            if not fenced:
                continue
            line = clean(stripped)
            if line.startswith("#") or line.startswith("|"):
                continue
            if not any(prefix in line for prefix in INVOCATIONS):
                continue
            command = extract(line)
            if command is None or not command:
                continue
            if any(token in command for token in SKIP_TOKENS):
                continue

            total += 1
            try:
                argv = shlex.split(command, posix=True)
            except ValueError as exc:
                failed += 1
                print(f"FAIL {relative}:{number} 引号不成对: {command} ({exc})")
                continue
            try:
                args = _fill_defaults(build_parser().parse_args(argv))
            except SystemExit:
                failed += 1
                print(f"FAIL {relative}:{number} parser 不认: {command}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"FAIL {relative}:{number} {exc.__class__.__name__}: {exc} <- {command}")
            else:
                print(f"OK   {relative}:{number} command={args.command:<10} {command}")

    print(f"\n{total - failed}/{total} 条文档命令通过 parser")
    return 1 if failed else 0


if __name__ == "__main__":
    # 文档里的命令带中文注释 (例: "ping all  # 广播"), 打印时在英文系统 (cp1252) 上
    # 会 UnicodeEncodeError —— 和 remote/__main__.py 里修的是同一个坑, 这里复用同一份
    force_utf8_stdio()
    raise SystemExit(main())
