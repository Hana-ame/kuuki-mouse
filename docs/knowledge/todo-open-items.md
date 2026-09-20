# 已知未修 / 未做清单

## 未修的 bug

| 位置 | 问题 | 详见 |
|---|---|---|
| proto `ClickMouseRequest` | `interval` 是普通标量，分不清显式 0 与缺省 | `protocol-proto3-optional.md` |
| proto `ClickMouseRequest` | **没有 `hold` 字段** —— WS 支持 `hold`、gRPC 不支持 | — |

> 2026-09-20 已修掉：`mouse.click` 静默忽略 `x`/`y`（见
> `protocol-mouse-click-ignores-xy.md`）、`keyboard.paste` 中文经 `clip.exe`
> 变 GBK 乱码（见 `gui-chinese-input-via-clipboard.md`）、`{"key": " "}` 空格
> 被 strip 成空串后误报"键名不能为空"（见 `protocol-key-whitespace.md`）、
> `Capture.to_dict()` 漏 `backend` 字段（JSON 通道与二进制帧头不一致）、
> WS 二进制帧头漏 `source_width`（见 `env-ws-frame-missing-source-width.md`）。

## 未做的能力

- **window 类 op**（`window.list` / `window.focus` / `window.title`）—— 纯视觉
  定位无法区分"长得像"的应用窗口，2026-09-20 已实战翻车：前台是 WorkBuddy，
  视觉脚本把它的会话标签栏当成了浏览器标签栏（见 `gui-window-focus-gap.md`）
- **OCR / 文字识别** —— 视觉能定位"这里有个框"，认不出框里写的是什么

## 未做的验证

- **PeerJS 跨 NAT**（本机只有一台；同机 WebRTC 走 host 候选，跨网才真需要 STUN）
- 弱网 / 重连：现在的行为是超时后标 offline，**不重试**
- 分发相关：**代码签名**（SmartScreen 会拦未签名 exe）、**体积精简**（av 编解码器没挑过）

## 小瑕疵

- 文档里客户端示例仍是 `/tmp/shot.png`、`/tmp/frames` 这类 Linux 风格路径
  （受控端已限定 Windows，示例该跟着改）
- `legacy/deepseek.html` 已加进 `.gitignore`（文件保留，只是不入库）
