# window.*: 视觉定位的语义地基

## 为什么要有窗口 op

`gui-window-focus-gap.md` 记过一次实战翻车: 只有视觉定位时, 脚本把 WorkBuddy 的
会话标签栏当成了浏览器标签栏, 整条"找图标 → 点击 → 粘贴"流程点进了错误的窗口。
视觉的天花板在于: 截图能告诉你"这里有个像输入框的东西", 说不出**它属于哪个应用**。

2026-09-20 补上了这一层 —— `remote/window.py` (纯 ctypes, 零第三方依赖) + 三个 op:

| op | 作用 | 关键返回 |
|---|---|---|
| `window.list` (`windows`) | 列出顶层窗口, 按 Z 序 | `windows[]`: `hwnd` `title` `process` `pid` `rect` `visible` `minimized` `foreground` |
| `window.foreground` (`foreground`) | 当前前台是谁 | 平铺的一个窗口 dict, `hwnd=0` = 没有前台 (锁屏) |
| `window.focus` (`focus`) | 切前台并确认 | `focused` (确认后的结果) `method` `matched` |

客户端/ctl 对应有 `windows` / `focus` 子命令; gRPC 对应
`ListWindows` / `GetForegroundWindow` / `FocusWindow` 三个 RPC; 三传输返回值一致
(`test_window_ops_agree_across_transports` 用打桩后端比对, 不动真桌面)。

## 设计里值得记的决定

- **`focused` 不是"调用成功了"**: Windows 有前台锁, 后台进程直接抢前台会被拒。
  实现按"正常 `SetForegroundWindow` → `AttachThreadInput` 挂前台线程 → 敲一下
  Alt 再试"三级退让, 切完轮询 `GetForegroundWindow` 直到超时 (`wait`, 默认 0.5s),
  把**真的**结果回报给调用方。实测三级都用得上过 (Edge 常年吃锁)。
- **命中多个不报错**: 按 Z 序取第 `index` 个 (默认 0), 总数放在 `matched` 里 ——
  调用方看到 `matched > 1` 就该换更窄的过滤条件。报错反而让自动化脚本难写。
- **过滤默认狠**: 隐藏的、被 DWM 隐藏的 (cloaked, UWP 后台)、无标题的一律不列 ——
  用户看不到也点不到, 列出来只会搅乱。`include_hidden` 留给调试。
- **protobuf 用 `uint32 hwnd`**: 两个理由。键名跟着 service 的 dict 走 (`hwnd` 不是
  `handle`), 控制端要把 proto JSON 与 WS 结果互比; 64 位整数在 proto3 JSON 里会变
  **字符串**, 与 WS 的 int 对不上, HWND 实际取值远小于 2^32, 32 位装得下。
- **`window.foreground` 返回平铺 dict** 而不是 `{"window": {...}}`: gRPC 侧直接回一个
  `WindowInfo` 消息, 嵌套一层就没法与 WS 对齐。

## 验证方式 (端到端, 全程纯 repo)

`evidence/verify_window_fix.py`:

1. `window.focus` 先切到 VS Code、再切回 msedge, 每步用 `window.foreground` 确认
   —— 证明 focus 真的会换窗口, 不是碰巧;
2. ctrl+t 输入网址打开 Gemini, **轮询 `window.list --title gemini` 确认真的到了**
   (第一次实跑 Enter 被吞, 粘贴全进了地址栏 → 之后必须确认再动手);
3. 点进提问框 → `keyboard.paste` → 用 repo 自己的 `vision.diff` 否证"提问框附近
   真的变了" → 变了才回车;
4. Gemini 收到消息并回复, 窗口标题变成对话名。

## 遗留

- `window.title` (只读某个 hwnd 的标题) 没做 —— `window.list` 已经带 title, 需求不成立。
- 坐标换算: `rect` 是屏幕坐标, 与截图的 `source_width` 同一坐标系; 窗口最小化时
  `rect` 不可信 (`focus` 会先 restore)。
