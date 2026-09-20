# 按文字定位比按坐标定位稳 (screen.find_text 的取舍)

**日期**: 2026-09-21
**场景**: `remote` · 视觉/文字定位 (`screen.ocr` → `screen.find_text`)
**结论**: 「点写着『发送』的那个按钮」要做成**服务端一次调用**返回**中心点坐标**，
而不是让调用方自己拿 OCR 结果过滤；三个默认值 (按行 / 只回一个 / 没找到不报错)
是踩过之后定的。

## 为什么不是"调用方自己筛 OCR 结果"

`screen.ocr` 已经把每行每词的坐标都给了，看上去调用方自己 `find` 一下就行。但那个
坐标有两套：图上的 `x/y/w/h` 和屏幕上的 `screen`。调用方算中心点时很容易直接拿图
坐标算 —— 漏掉"除缩放 → 加裁剪偏移 → 加帧原点"这三步中的任何一步，点下去就偏一整块
屏（副屏摆在主屏左边时差 1920px）。**中心点必须在服务端算**，因为只有服务端持有
`scale` / `region` / `origin` 这三个数。

（`locate` 之所以是客户端算，是因为它用的 `vision.py` 是纯 Pillow、不依赖受控端；
OCR 不是 —— 它必须跑在 Windows 受控端上，所以这一层只能跟着进 service。）

## 三个默认值是挑过的

- **`unit` 默认 `line`，不是 `word`**：WinRT 会**逐字**切中文（"发送" 常被切成两个
  词），词级匹配在这种文本上几乎命中不了。要更紧的框才显式给 `word`。
- **`all` 默认 False**（只回最靠上的那个）：OCR 给行的顺序**不保证**是阅读顺序，
  所以要自己按 `(y, x)` 排一遍再取第一个 —— 否则"换台机器/换个语言包，同一个按钮
  位置就变了"。
- **没找到不报错**（`found=false` + `items: []`）：屏幕上没有这个字是正常结果，
  不是调用方的错。报错留给 `bad_request`（参数写错）和 `unsupported`（非 Windows）。

## 参数必须回显

`query` / `match` / `unit` / `case_sensitive` 全部在返回值里回显。理由见
`protocol-optional-param-needs-echo.md`：不回显的话，"某条传输把这个参数弄丢了"永远
测不出来 —— 服务端按默认值处理，一切看起来正常，只有调用方指定的口径没人理会。
这次的变异测试里，"gRPC 侧漏传 unit" 与"漏传 case_sensitive" 两条正是靠回显抓到的。

## 一个测试假设的错误：前台窗口未必在窗口列表里

`test_window_list_on_real_desktop` 原本断言"前台窗口一定在 `list_windows()` 结果里"，
在挂着远程桌面的机器上一直是红的：前台是 Parsec 的覆盖层窗口，`IsWindowVisible`
返回 False，而 `list_windows()` 默认**只列可见且有标题的**窗口 —— 那种情况下"前台
不在列表里"是正确行为。判据改成"前台窗口可见且有标题时才要求它在列表里"。

## 相关

- `gui-ocr-via-powershell-winrt.md` —— OCR 引擎本身（选型、WinRT 异步、BOM）
- `gui-screenshot-scale.md` / `gui-imagegrab-primary-not-virtual.md` —— 三步坐标换算
- `protocol-optional-param-needs-echo.md` —— 参数回显为什么是必需的
