# 系统 OCR 怎么用: PowerShell + WinRT, 零依赖但只能跑在受控端

## 现象

视觉定位 (`vision.find_color` / `match_template`) 能回答"屏幕上有一块像输入框的
东西", 回答不了"这块东西里写着什么"。于是调用方知道该点哪儿, 却不知道自己点的是
"发送"还是"清空" —— 差一个 OCR 就闭环不了。

而 OCR 的常规路子都要付出依赖代价: tesseract 要装二进制 + 语言数据,
paddle / rapidocr 要下 onnx 模型。受控端是要分发到别人电脑上的 exe, 每加一个
依赖都要跟着进包、跟着被体积检查盯 —— 为一个"附带工具"抬所有人的安装成本不划算。

## 根因 / 取舍

Windows 10/11 **自带** `Windows.Media.Ocr`, 语言包随系统装 (中文/日文/英文常见),
离线可用, 也不需要 pip 装任何东西。代价有三条, 都得接受才算划算:

1. **只有 Windows 受控端有** —— 而这正好是它唯一支持的平台, 所以这条不算损失;
2. 每次调用要起一次外部进程, 约 1 秒 (冷启动 + WinRT 加载), 不能当逐帧监控用;
3. CPython 没有 WinRT 绑定, 得借道 PowerShell。

**为什么是 PowerShell 而不是 `winsdk` 那个 pip 包**: 后者要往依赖表里加一项,
又回到"加依赖"那条路; 而 PowerShell 5.1 天生能直接实例化 WinRT 类型
(`[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]`)。
代价是多一层进程与文本解析, 换来的是零依赖。

## 踩过的坑

- **WinRT 的异步在 PowerShell 里没有 await**。 `IAsyncOperation<T>` 得靠
  `System.WindowsRuntimeSystemExtensions.AsTask<T>` 反射出来转成 `Task` 再
  `Wait()`; 而那个类所在的程序集**默认没加载**, 必须先
  `Add-Type -AssemblyName System.Runtime.WindowsRuntime`。少了这一行报的是
  "找不到类型 [System.WindowsRuntimeSystemExtensions]" —— 看不出是"没加载"。
- **脚本文件要写 UTF-8 带 BOM**。PowerShell 5.1 不认无 BOM 的 UTF-8, 会按系统
  ANSI 代码页读, 中文注释/报错信息全糊 (`.sh` 那边相反: BOM 会破坏 shebang)。
- **`OcrLine` 自己没有 BoundingRect**, 只有 `OcrWord` 有。行矩形只能把这一行所有
  词的矩形取并集 —— 词才是这一层的原始信息。
- **中文会被切成一个字一个"词"** (`"中 文 识 别"`)。要整句就用 `lines[].text`,
  别自己拿 `words` 拼 (拼出来的空格是引擎切的, 不是原文)。
- **指定语言就必须只用它**: 没装对应语言包要直接报错, 不能悄悄换引擎 —— 换了的表
  现是中文被当别的语言认, 调用方完全看不出来。语言列表从
  `OcrEngine.AvailableRecognizerLanguages` 拿 (本机实测 `ja,zh-Hans-CN`,
  反而没有 `en-US`)。
- 输出格式用 `键=值` 文本行, **别用 `ConvertTo-Json`**: PS 5.1 对单元素数组、
  对中文转义的处理各版本不一样; 而这里要传的只有"文本 + 四个整数"。

## 怎么做

- **脚本是运行时生成的字符串**, 写到临时目录再 `powershell -File` 跑, 不要往仓库
  里放 `.ps1` —— 打包 exe 时不用给它配 `datas`, 少一处"打包后找不到文件"。
- 起进程时带 `CREATE_NO_WINDOW`: 受控端常是后台 exe, 每认一次字闪一个黑框很烦。
- 结果写进临时文件再读回来 (别走 stdout): 编码可控, 也避开控制台代码页。
- 留一个**打桩缝** (`_run_script`), 让单元测试能替换"跑外部进程"这一步 —— 解析与
  坐标换算才是要盯的地方, 而真起一次进程要 1 秒、CI 上还不一定有中文语言包。
- 坐标一律先按**图坐标**返回, 由 service 那一层统一换算成屏幕坐标 (除缩放 → 加
  裁剪偏移 → 加帧原点)。换算漏一步的结果都是"看着很合理但点偏"。

## 测的时候

- 解析 / 并集 / 换算都用打桩验, 不真起进程;
- 引擎本身只在"这台机器真有语言包"时跑一次 (`_ocr_available()` 判), 只断言**数字**
  被认出来 —— 中英文识别率随语言包版本浮动, 这里要证明的是链路通, 不是识别率;
- 每条新判据都过一遍变异测试 (改一行看红不红)。`include_words=False` 那条就是这么
  抓出来的: protobuf 的**空 repeated 在 `MessageToDict` 里照样会出现**
  (`"words": []`), 而 WS 侧是普通 dict —— 一边给空列表、一边不给键, 跨传输比对
  立刻红。所以"不要词"的正确表达是**空列表**, 不是"没有这个键"。
