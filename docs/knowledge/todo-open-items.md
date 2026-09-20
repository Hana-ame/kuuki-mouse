# 已知未修 / 未做清单

## 未修的 bug

| 位置 | 问题 | 详见 |
|---|---|---|
| `remote/service.py:313` | `mouse.click` **静默忽略 `x`/`y`**，只点当前光标位置 | `protocol-mouse-click-ignores-xy.md` |
| proto `ClickMouseRequest` | `interval` 是普通标量，分不清显式 0 与缺省 | `protocol-proto3-optional.md` |
| proto `ClickMouseRequest` | **没有 `hold` 字段** —— WS 支持 `hold`、gRPC 不支持 | — |

## 未做的验证

- **PeerJS 跨 NAT**（本机只有一台；同机 WebRTC 走 host 候选，跨网才真需要 STUN）
- 弱网 / 重连：现在的行为是超时后标 offline，**不重试**
- 分发相关：**代码签名**（SmartScreen 会拦未签名 exe）、**体积精简**（av 编解码器没挑过）

## 小瑕疵

- 文档里客户端示例仍是 `/tmp/shot.png`、`/tmp/frames` 这类 Linux 风格路径
  （受控端已限定 Windows，示例该跟着改）
- `legacy/deepseek.html` 已加进 `.gitignore`（文件保留，只是不入库）
