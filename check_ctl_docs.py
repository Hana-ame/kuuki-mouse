"""用真 parser 校验 README / docs 里的命令行 —— 防文档悄悄过期。

文档比代码更容易过期: 命令表、复现指南里抄着几十条命令行, parser 一改它们就悄悄
失真, 而 markdown 不会报错。这个脚本把文档里的命令喂给**真正的 parser**, 认不下来
的直接失败。

覆盖四个 CLI:

| 写法 | 喂给 |
|---|---|
| `python -m remote ...` | `remote.__main__.build_parser()` |
| `python -m remote.ctl ...` | `remote.ctl.build_parser()` (+ `_fill_defaults`) |
| `python -m remote.client ...` | `remote.client._build_parser()` |
| `python -m remote.dummy ...` | `remote.dummy.build_parser()` |

文件名还是 ``check_ctl_docs`` 是历史包袱 —— CI 与好几处文档都按这个名字引用它,
改名要连带改 workflow。**权衡过一次**: 改名要碰 CI + README + runbook + 知识库四处,
收益只是文件名更贴合, 不值得。文档统一说它是「文档命令 parser 校验」, 名字当专有名词看。

## 覆盖哪些位置

代码块不用说。后来发现 **README 的传输对照表也是表格**, 而表格行整段带着 `|` 被
跳过了 —— 恰恰是最容易过期的地方没人看着。现在表格按**单元格**拆开再验: 单元格里
是 `` `python -m remote --ws` `` 这样的整条命令就验, 是 `` `--url ws://...` `` 这种
参数片段 (没有调用前缀) 自然不会被认出来, 不会误报。

## 刻意不扫正文

正文里会有「写成 `shot out.png self` 会报错」这种**故意写错的反例**。扫全文会误报,
然后你会为了让数字好看而去改正文, 把反例改对了 —— 那就本末倒置了。

**先想清楚「哪些内容是承诺可执行的」**, 再决定扫描范围。

## 占位符 vs 真 JSON

早期版本见到 `{` 就跳过整条命令, 于是文档里 `--args '{"x":400}'` 这类**最该被校验**
的参数一个都没验到。现在改成: `<` 和 `|` 一律跳过 (shell 管道 / 占位符),
`{...}` 只有在**像占位符** (`{max_width}` 这种单词) 时才跳过, 长得像 JSON 的照验。

## 它管不到的

这只验「parser 认不认得」, **不验参数值的合法性**。曾经 README 里写
`--transport ws --endpoint 192.168.1.5:8765`, parser 认得 (字符串参数), 但运行期
`validate_endpoint` 会拒 (ws 必须 `ws://` 开头)。见 ctl 的相应知识点。
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from remote.__main__ import build_parser as remote_build_parser  # noqa: E402
from remote.client import _build_parser as client_build_parser  # noqa: E402
from remote.ctl import _fill_defaults, build_parser as ctl_build_parser  # noqa: E402
from remote.dummy import build_parser as dummy_build_parser  # noqa: E402
from utf8_stdio import force_utf8_stdio  # noqa: E402

ROOT = Path(__file__).resolve().parent

# 会展开成各 CLI 的写法: 文档里有用裸 python 的, 也有用 $PY 变量占位解释器的。
# 顺序要按**前缀从长到短**排 —— `python -m remote` 是 `python -m remote.ctl` 的
# 前缀, 放前面的话所有 ctl 命令都会被认成 remote 命令报错。
INTERPRETERS = ("python", "$PY", "$PYTHON")


def _invocations():
    specs = [
        ("remote.ctl", ctl_build_parser, True),
        ("remote.client", client_build_parser, False),
        ("remote.dummy", dummy_build_parser, False),
        ("remote", remote_build_parser, False),
    ]
    out = []
    for module, builder, fill in specs:
        for interp in INTERPRETERS:
            out.append((f"{interp} -m {module}", module, builder, fill))
    return out


INVOCATIONS = _invocations()

# 整行出现就跳过: 管道符、尖括号占位符
LINE_SKIP = ("|", "<")

# 单个 token 长得像占位符 ({room} 这种单词) 才跳过; JSON 不算
PLACEHOLDER = re.compile(r"^\{[A-Za-z_][\w.\[\]]*\}$")


def clean(line: str) -> str:
    """把一行 markdown 里的命令行剥出来 (去掉列表符、提示符、行尾注释)。"""
    line = re.sub(r"^[-*>]\s*", "", line)
    line = re.sub(r"^\$\s*", "", line)
    line = re.split(r"\s+#", line)[0]
    return line.strip()


def extract(line: str):
    """返回 (命令尾部, 模块名, builder, 是否要补默认值), 没匹配到返回 None。

    长前缀优先: 一行里同时可能有多种写法时按最长的那个认。
    """
    best = None
    for prefix, module, builder, fill in INVOCATIONS:
        index = line.find(prefix)
        if index < 0:
            continue
        # 必须是**独立出现**: `python -m remote --ws` 里的 remote 后面要是空格或行尾
        tail = line[index + len(prefix):]
        if tail and not tail[0].isspace():
            continue
        if best is None or len(prefix) > len(best[0]):
            best = (prefix, tail.strip(), module, builder, fill)
    if best is None:
        return None
    prefix, tail, module, builder, fill = best
    # 占位符判定要用**没加工过**的尾巴: remote/README 有张 PeerJS 拓扑图, 一行里
    # 写 `python -m remote    PeerJsClient("...<房间码>")`, 先去括号会把 `<` 摘掉,
    # 于是这条图注被当成命令。先看原样。
    if any(token in tail for token in LINE_SKIP):
        return None
    tail = tail.strip()
    # 续行符后面的内容是 shell 拼接, 不当命令看
    tail = tail.split("\\")[0].strip()
    # 括号里的通常是旁注 —— remote/README 里引用了一段非 Windows 的拒绝启动输出,
    # 里面有 "python -m remote        (或 start-win.bat)", 它不是命令。截到括号前
    # 反而还能把真正的 `python -m remote` 验上。
    tail = tail.split("(")[0].strip()
    return tail, module, builder, fill


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
            # 代码块里的行全都看; 代码块外面只认 | 开头的表格行 (README 的传输
            # 对照表就是表格写的, 不在围栏里)
            if not fenced and not stripped.startswith("|"):
                continue
            line = clean(stripped)
            if line.startswith("#"):
                continue
            candidates = [line]
            if line.startswith("|"):
                # 表格行: 拆单元格。单元格里可能是 `命令`（**旁注**）这种混排 --
                # 反引号外面的中文注解不是命令的一部分, 只取第一段行内代码。
                cells = []
                for cell in line.strip("|").split("|"):
                    cell = cell.strip()
                    if "`" in cell:
                        parts = cell.split("`")
                        # parts: [前缀, 代码, 后缀, 代码, ...] → 取第一段代码
                        cell = parts[1] if len(parts) > 1 else cell
                    cells.append(cell.strip())
                candidates = cells
            for candidate in candidates:
                got = extract(candidate)
                if got is None:
                    continue
                command, module, builder, fill = got
                if not command:
                    continue

                total += 1
                try:
                    argv = shlex.split(command, posix=True)
                except ValueError as exc:
                    failed += 1
                    print(f"FAIL {relative}:{number} 引号不成对: {command} ({exc})")
                    continue
                if any(PLACEHOLDER.match(token) for token in argv):
                    total -= 1
                    continue
                try:
                    args = builder().parse_args(argv)
                    if fill:
                        args = _fill_defaults(args)
                except SystemExit:
                    failed += 1
                    print(f"FAIL {relative}:{number} {module} parser 不认: {command}")
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    print(
                        f"FAIL {relative}:{number} {module} {exc.__class__.__name__}: "
                        f"{exc} <- {command}"
                    )
                else:
                    print(f"OK   {relative}:{number} [{module}] {command}")

    print(f"\n{total - failed}/{total} 条文档命令通过 parser")
    return 1 if failed else 0


if __name__ == "__main__":
    # 文档里的命令带中文注释 (例: "ping all  # 广播"), 打印时在英文系统 (cp1252) 上
    # 会 UnicodeEncodeError —— 和 remote/__main__.py 里修的是同一个坑, 这里复用同一份
    force_utf8_stdio()
    raise SystemExit(main())
