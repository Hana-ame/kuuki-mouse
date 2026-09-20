"""ocr —— 让受控端读出屏幕上的**文字** (Windows 内置 OCR, 经 PowerShell)。

为什么要有这个模块
------------------
视觉定位 (``remote/vision.py``) 能回答"屏幕上有一块像输入框的东西", 回答不了
"这块东西里写着什么"。于是调用方拿到一帧图, 知道该点哪儿, 却不知道自己点的是
"发送"还是"清空" —— 差一个 OCR 就闭环不了。

选型的取舍
----------
OCR 有两条主流路子, 这里选的是**系统内置的 WinRT OCR**:

- 装第三方 (tesseract / paddle / rapidocr): 要么得额外装二进制与语言数据, 要么
  要下一堆 onnx 模型。受控端是要分发到别人电脑上的 exe, 每加一个依赖都要跟着
  进包、跟着被体积检查 —— 为一个"附带工具"抬所有人的安装成本不划算。
- 用系统内置的 ``Windows.Media.Ocr``: Windows 10/11 自带, 中文/日文/英文的语言
  包随系统装, 没有任何 pip 依赖, 也不需要联网。代价是**只能跑在受控端**
  (而这正好是它唯一支持的平台), 以及每次调用要起一次 PowerShell (约 1 秒)。

代价换来的东西很实在: 零依赖、离线可用、中英文都能认。

为什么经过 PowerShell
---------------------
``Windows.Media.Ocr`` 是 WinRT API, CPython 里没有内置绑定 —— 要调它得装
``winsdk`` 之类的包 (那就是又回到"加依赖"那条路)。但 PowerShell 5.1 **天生
能直接实例化 WinRT 类型**, 所以这里生成一段 .ps1 写到临时目录, 用
``powershell -File`` 跑一次, 让它把结果写成文本, Python 再读回来。

脚本是运行时生成的字符串, 不是仓库里的 .ps1 文件 —— 打进 exe 时不需要给
PyInstaller 配 datas, 少一处"打包后找不到文件"的风险。

两个踩过的坑
------------
1. WinRT 的异步方法在 PowerShell 里**没有 await**: ``IAsyncOperation<T>`` 得靠
   ``System.WindowsRuntimeSystemExtensions.AsTask<T>`` 反射出来转成 ``Task``
   才能 ``Wait()``。而且那个类所在的程序集**默认没加载**, 必须先
   ``Add-Type -AssemblyName System.Runtime.WindowsRuntime`` —— 少了这一行报的
   错是"找不到类型 [System.WindowsRuntimeSystemExtensions]", 看不出是没加载。
2. ``OcrLine`` 自己**没有** BoundingRect (只有 ``OcrWord`` 有)。所以行矩形是脚本
   里把这一行所有词的矩形取并集算出来的。

坐标系
------
返回的 ``x/y/w/h`` 一律是**图坐标** (喂进来的那张图的像素), 与
``remote/vision.py`` 的约定一致。要换成能喂给 ``mouse.click`` 的屏幕坐标, 由
``service._op_screen_ocr`` 统一做 (那里才知道帧有没有被缩放、裁过、以及这一帧
在虚拟桌面里的原点)。
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image

__all__ = [
    "ocr_supported",
    "list_languages",
    "recognize",
    "line_rect",
    "OcrError",
    "DEFAULT_TIMEOUT",
]

#: 一次识别最多等多久。PowerShell 冷启动约 1 秒, 大图 + 中文也就几秒
DEFAULT_TIMEOUT = 30.0

#: PowerShell 的位置: 先找 PATH, 找不到就用系统目录 (精简版系统 / 打包后
#: 环境变量不全时常见)
_PS_CANDIDATES = (
    "powershell.exe",
    os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"),
        "System32",
        "WindowsPowerShell",
        "v1.0",
        "powershell.exe",
    ),
)

_LANG_CACHE: Optional[List[str]] = None


class OcrError(Exception):
    """OCR 相关的业务异常: 平台不支持 / 没语言包 / 引擎跑不起来。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------- PowerShell 脚本
#
# 文件是以 UTF-8 **带 BOM** 落盘的 (见 ``_run_script``) —— PowerShell 5.1 只认
# 带 BOM 的 UTF-8, 不然就按系统 ANSI 代码页读, 中文全糊。
#
# 输出格式刻意用 ``键=值`` 文本行而不是 JSON: PowerShell 5.1 的 ConvertTo-Json
# 对单元素数组、对中文转义的处理各版本不一样, 而这里要传的东西只有"文本 + 四个
# 整数", 用制表符分隔的文本行没有任何歧义。

_PS_SCRIPT = r'''
param([string]$Path, [string]$Out, [string]$Lang, [string]$Mode)

$ErrorActionPreference = 'Stop'
$script:lines = New-Object System.Collections.ArrayList
$script:avail = ''

function Emit($s) { [void]$script:lines.Add([string]$s) }

function Flush {
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Out, ($script:lines -join "`n"), $enc)
}

# WinRT 的异步操作在 PowerShell 里没有 await —— 只能把 AsTask<T> 从
# System.WindowsRuntimeSystemExtensions (这个程序集默认没加载, 见上面 Add-Type)
# 反射出来, 转成 Task 再 Wait。
function Await($op, [type]$ResultType) {
    $method = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]
    $generic = $method.MakeGenericMethod($ResultType)
    $task = $generic.Invoke($null, @($op))
    [void]$task.Wait()
    return $task.Result
}

function Load-Ocr {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    [void][Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
    [void][Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
    [void][Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
    [void][Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
    [void][Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
}

function Get-Languages {
    $tags = @()
    foreach ($item in [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages) {
        $tags += $item.LanguageTag
    }
    return $tags
}

function New-Engine($tags, $lang) {
    if ($lang) {
        if ($tags -notcontains $lang) { return $null }
        return [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage(
            (New-Object Windows.Globalization.Language $lang))
    }
    # 没指定就按用户的语言偏好挑 —— 中文界面上它自然选中文引擎, 比写死一个强
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    if ($null -ne $engine) { return $engine }
    if ($tags.Count -gt 0) {
        return [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage(
            (New-Object Windows.Globalization.Language $tags[0]))
    }
    return $null
}

function Main {
    Load-Ocr
    $tags = Get-Languages
    $script:avail = ($tags -join ',')
    if ($Mode -eq 'langs') {
        if ($tags.Count -eq 0) { Emit 'OK=0'; Emit 'ERROR=no_language_pack'; return }
        Emit 'OK=1'
        return
    }
    if ($tags.Count -eq 0) { Emit 'OK=0'; Emit 'ERROR=no_language_pack'; return }

    $engine = New-Engine $tags $Lang
    if ($null -eq $engine) {
        # 指定了语言但没有那个语言包 -> 报清楚, 别默默换一个引擎 (换了的后果是
        # 中文被当成日文认, 调用方完全看不出来)
        if ($Lang) { Emit 'OK=0'; Emit 'ERROR=no_such_language'; Emit ('MESSAGE=' + $Lang); return }
        Emit 'OK=0'; Emit 'ERROR=no_engine'; return
    }

    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

    Emit 'OK=1'
    Emit ('LANG=' + $engine.RecognizerLanguage.LanguageTag)
    Emit ('WIDTH=' + $bitmap.PixelWidth)
    Emit ('HEIGHT=' + $bitmap.PixelHeight)
    foreach ($line in $result.Lines) {
        Emit ('LINE=' + $line.Text)
        # 行矩形在这里不算: OcrLine 没有 BoundingRect, 只有词有。Python 侧取并集
        foreach ($word in $line.Words) {
            $r = $word.BoundingRect
            Emit ('WORD=' + $word.Text + "`t" + [int][math]::Round($r.X) + "`t" +
                  [int][math]::Round($r.Y) + "`t" + [int][math]::Round($r.Width) + "`t" +
                  [int][math]::Round($r.Height))
        }
    }
}

try {
    Main
}
catch {
    Emit 'OK=0'
    Emit 'ERROR=failed'
    Emit ('MESSAGE=' + $_.Exception.Message)
}
Emit ('AVAILABLE=' + $script:avail)
Flush
'''


# ---------------------------------------------------------------- 运行环境


def ocr_supported() -> bool:
    """这台机器能不能做 OCR (Windows + 能找到 PowerShell)。"""
    if sys.platform != "win32" or platform.system() != "Windows":
        return False
    return _powershell() is not None


def _powershell() -> Optional[str]:
    for candidate in _PS_CANDIDATES:
        if os.path.isabs(candidate):
            if os.path.exists(candidate):
                return candidate
            continue
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _run_script(
    path: Optional[str],
    lang: str = "",
    mode: str = "ocr",
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """把脚本写到临时目录跑一次, 返回它写出来的那份输出文本。

    单独一个函数是为了让测试能替换它 —— 单元测试不该真的去起 PowerShell (CI 的
    runner 上也没有中文语言包), 而"文本行 -> 结构化结果"这段解析逻辑才是我们要
    盯的地方。
    """
    powershell = _powershell()
    if powershell is None:
        raise OcrError("backend_unavailable", "找不到 PowerShell, 没法调系统 OCR")

    with tempfile.TemporaryDirectory(prefix="kuuki-ocr-") as tmp:
        script_path = os.path.join(tmp, "run.ps1")
        out_path = os.path.join(tmp, "out.txt")
        # UTF-8 **带 BOM** 写: PowerShell 5.1 不写 BOM 就按系统 ANSI 代码页读,
        # 脚本里的中文注释会被读成乱码。注释乱了不影响执行, 但报错信息里那几句
        # 会跟着一起糊 —— 带 BOM 是 5.1 唯一认得 UTF-8 的方式。
        with open(script_path, "w", encoding="utf-8-sig", newline="\r\n") as handle:
            handle.write(_PS_SCRIPT)
        command = [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            script_path,
            "-Path",
            path or "",
            "-Out",
            out_path,
            "-Lang",
            lang or "",
            "-Mode",
            mode or "ocr",
        ]
        # 不弹控制台窗口: 受控端常常是打包成 exe 后台跑的, 每认一次字闪一个黑框
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                creationflags=flags,
            )
        except subprocess.TimeoutExpired as exc:
            raise OcrError("backend_unavailable", f"OCR 超时 ({timeout}s)") from exc
        except OSError as exc:
            raise OcrError("backend_unavailable", f"调 PowerShell 失败: {exc}") from exc

        if not os.path.exists(out_path):
            raise OcrError("backend_unavailable", "PowerShell 没有写出结果文件")
        with open(out_path, "r", encoding="utf-8") as handle:
            return handle.read()


# ---------------------------------------------------------------- 解析


def _parse(raw: str) -> Dict[str, Any]:
    """把脚本的输出文本解析成结构化结果。

    ``OK=0`` 时按 ``ERROR`` 抛出对应的 :class:`OcrError` —— 语言包缺失与"指定了
    没有的语言"是两种不同的错, 调用方要能分得清。
    """
    lines: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {}
    available: List[str] = []
    ok = False
    error = ""
    message = ""

    for row in (raw or "").splitlines():
        if not row:
            continue
        key, _, value = row.partition("=")
        if key == "WORD":
            parts = value.split("\t")
            if len(parts) < 5 or not lines:
                continue
            lines[-1]["words"].append(
                {
                    "text": parts[0],
                    "x": int(parts[1]),
                    "y": int(parts[2]),
                    "w": int(parts[3]),
                    "h": int(parts[4]),
                }
            )
        elif key == "LINE":
            lines.append({"text": value, "words": [], "x": 0, "y": 0, "w": 0, "h": 0})
        elif key == "AVAILABLE":
            available = [tag for tag in value.split(",") if tag]
        elif key == "ERROR":
            error = value
        elif key == "MESSAGE":
            message = value
        elif key == "OK":
            ok = value == "1"
        elif key in ("LANG", "WIDTH", "HEIGHT"):
            meta[key.lower() if key != "LANG" else "language"] = (
                value if key == "LANG" else int(value)
            )

    if not ok:
        if error == "no_language_pack":
            raise OcrError(
                "unsupported",
                "系统没装任何 OCR 语言包 (设置 -> 语言里加一个就有了)",
            )
        if error == "no_such_language":
            raise OcrError(
                "bad_request",
                f"没有这个 OCR 语言: {message or ''}".strip()
                + (f"; 可用: {', '.join(available)}" if available else "")
                + " (没装语言包时列表是空的)",
            )
        if error == "no_engine":
            raise OcrError("unsupported", "建不出 OCR 引擎 (语言包可能没装全)")
        raise OcrError("internal", f"OCR 失败: {message or error or '未知原因'}")

    for line in lines:
        line.update(line_rect(line["words"]))
    return {
        "ok": True,
        "text": "\n".join(line["text"] for line in lines),
        "language": meta.get("language", ""),
        "width": int(meta.get("width", 0)),
        "height": int(meta.get("height", 0)),
        "lines": lines,
        "count": len(lines),
        "available": available,
    }


def line_rect(words: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """一行文字的外接矩形 = 这一行所有词矩形的并集。

    ``OcrLine`` 没有 BoundingRect (只有 ``OcrWord`` 有), 所以只能在词上算。没有
    词的行给全零 —— 调用方按"这一段没有位置"处理, 而不是崩在这里。
    """
    if not words:
        return {"x": 0, "y": 0, "w": 0, "h": 0}
    x0 = min(int(w.get("x", 0)) for w in words)
    y0 = min(int(w.get("y", 0)) for w in words)
    x1 = max(int(w.get("x", 0)) + int(w.get("w", 0)) for w in words)
    y1 = max(int(w.get("y", 0)) + int(w.get("h", 0)) for w in words)
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


# ---------------------------------------------------------------- 对外接口


def list_languages(refresh: bool = False) -> List[str]:
    """系统里装了哪些 OCR 语言 (``zh-Hans-CN`` / ``en-US`` 这类 tag)。

    结果会缓存: 每问一次都要起一次 PowerShell (约 1 秒), 而语言包不会在运行中变。
    """
    global _LANG_CACHE
    if _LANG_CACHE is not None and not refresh:
        return list(_LANG_CACHE)
    if not ocr_supported():
        raise OcrError("unsupported", "文字识别需要被控端是 Windows")
    parsed = _parse(_run_script(None, mode="langs"))
    _LANG_CACHE = list(parsed.get("available") or [])
    return list(_LANG_CACHE)


def _to_png(source: Any, tmp: str) -> str:
    """把 bytes / 路径 / PIL Image 落成一张临时 PNG, 返回路径。

    WinRT 的解码器挑格式: PNG 是它一定认的那种, 而传进来的可能是 jpeg/webp
    (截图默认就是 png, 但调用方也可能把上一帧存成了 jpg)。统一转一遍最省事,
    顺带把 RGBA / 调色板这类模式也抹平。
    """
    if isinstance(source, Image.Image):
        image = source
    elif isinstance(source, (bytes, bytearray)):
        import io

        image = Image.open(io.BytesIO(bytes(source)))
    else:
        image = Image.open(source)
    if image.mode != "RGB":
        image = image.convert("RGB")
    path = os.path.join(tmp, "frame.png")
    image.save(path, "PNG")
    return path


def recognize(
    source: Any,
    lang: str = "",
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """认一张图里的文字, 返回逐行逐词的结果 (图坐标)。

    ``source`` 可以是 PNG/JPEG 的 bytes、文件路径或 PIL Image —— 与
    ``vision.load`` 的口径一致, 调用方不必为了 OCR 多记一套。

    ``lang`` 空着表示"按用户语言偏好挑" (中文界面上就是中文引擎); 给了就**只用**
    那个语言, 没有对应的语言包直接报错, 不悄悄换引擎。
    """
    if not ocr_supported():
        raise OcrError("unsupported", "文字识别需要被控端是 Windows (走系统内置 OCR)")
    with tempfile.TemporaryDirectory(prefix="kuuki-ocr-") as tmp:
        path = _to_png(source, tmp)
        return _parse(_run_script(path, lang=lang, timeout=timeout))


def to_screen_rect(
    rect: Dict[str, Any],
    scale: float = 1.0,
    origin: Tuple[int, int] = (0, 0),
    offset: Tuple[int, int] = (0, 0),
) -> Dict[str, int]:
    """图坐标矩形 -> 虚拟桌面坐标 (能直接喂给 ``mouse.click`` 的那种)。

    三步, 一步都不能少:

    * ``/scale`` —— 帧被缩过 (``--max-width``) 时图上的 1px 不是屏幕的 1px;
    * ``+offset`` —— 帧被裁过 (``region``) 时图左上角是屏幕的 ``region.left``;
    * ``+origin`` —— 抓的不是原点那一帧时 (多屏 / 副屏), 整帧还要平移。

    ``scale`` 用 0 或 None 传进来时按 1.0 处理 —— 除零比"少换算一步"更难查。
    """
    factor = float(scale or 0.0) or 1.0
    x = int(rect.get("x", 0))
    y = int(rect.get("y", 0))
    w = int(rect.get("w", 0))
    h = int(rect.get("h", 0))
    left = origin[0] + offset[0] + x / factor
    top = origin[1] + offset[1] + y / factor
    return {
        "x": int(round(left)),
        "y": int(round(top)),
        "w": int(round(w / factor)),
        "h": int(round(h / factor)),
    }
