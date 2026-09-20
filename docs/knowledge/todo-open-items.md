# 已知未修 / 未做清单

## 未修的 bug

**目前没有**（2026-09-21 把最后两条一起修了，见下）。

> 2026-09-21 已修掉：proto `ClickMouseRequest` 的 `interval` 是普通标量、且没有
> `hold` 字段 —— 于是「gRPC 不支持 `--hold`」与「`--interval 0` 在 gRPC 侧被当成
> 没给」（见 `protocol-zero-is-valid-value.md` / `protocol-proto3-optional.md`）。
> 修法是 `interval` 改 `optional` + 新增 `optional hold`，**五处一起跟**：proto →
> 重新生成存根 → `grpc_server.py` 用 `HasField` 传 → `client.py` 的
> `_optional_number`（给了才写，`or 默认` 会把显式 0 吞掉）→ 文档。
> 顺带把 `interval` 也加进 `mouse.click` 的返回值 —— **返回值里没有的参数是测不出
> 跨传输差异的**：`hold` 一补就被 `test_new_ops_agree_across_transports` 抓到，
> `interval` 却静默通过了「把 0 换成 0.05」的变异测试，直到它进了返回值才红。
> （变异测试值得做：改一行看用例红不红，不红的判据等于没有判据。）
>
> 2026-09-20 已修掉：`mouse.click` 静默忽略 `x`/`y`（见
> `protocol-mouse-click-ignores-xy.md`）、`keyboard.paste` 中文经 `clip.exe`
> 变 GBK 乱码（见 `gui-chinese-input-via-clipboard.md`）、`{"key": " "}` 空格
> 被 strip 成空串后误报"键名不能为空"（见 `protocol-key-whitespace.md`）、
> `Capture.to_dict()` 漏 `backend` 字段（JSON 通道与二进制帧头不一致）、
> WS 二进制帧头漏 `source_width`（见 `env-ws-frame-missing-source-width.md`）。
> 同日也补上了 `notify` 的 gRPC RPC 与 **window 类 op**
> （`window.list` / `window.foreground` / `window.focus`，见 `gui-window-ops-module.md`）。

## 未做的能力

- **OCR / 文字识别** —— 视觉能定位"这里有个框"，认不出框里写的是什么
  （window 类 op 已补：`gui-window-ops-module.md`）

## 未做的验证

- **PeerJS 跨 NAT**（本机只有一台；同机 WebRTC 走 host 候选，跨网才真需要 STUN）
- 弱网 / 重连：现在的行为是超时后标 offline，**不重试**
- **多显示器上的坐标校准**：`screen.calibrate` 只在单屏上验过（1680×1050，残差
  0.44px）。它的主要价值 —— 那个帧头里没有的**平移量** `origin` —— 只有多显示器
  （虚拟桌面原点可能是 (-1920, 0)）才体现得出来，本机没这个条件验
- 分发相关：**代码签名**（SmartScreen 会拦未签名 exe）、**体积精简**（av 编解码器没挑过）

## 三传输其实不等价 —— 2026-09-20 全 op 核对的结果

仓库的约定是「一份实现、三种传输」，但逐一比对时发现**有三类 op 在某个传输上根本没有**:

| op / 能力 | WS | gRPC | PeerJS |
|---|---|---|---|
| `screen.grab` / `screen.watch` / `screen.unwatch` | ✅ | ❌（推流走 `StreamScreenshots` 服务端流式，不是 op） | ❌ |

> 2026-09-21 更新: `mouse.click` 的 `hold` 那行已修 (proto 补了 `optional hold`,
> 五处都跟上了)。2026-09-20 那次修的是 `notify` —— `grpc_server.py` 补了 `Notify`
> RPC。表里只剩 `screen.grab` / `screen.watch` 这一类「推流式」能力没进 gRPC,
> 而它们在 gRPC 上本来就走 `StreamScreenshots` 服务端流式, 不是同一套 op 语义。window 三个 op 是**补 op 时就三条传输一起加的**
> (`ListWindows` / `GetForegroundWindow` / `FocusWindow`), 等价性由
> `test_window_ops_agree_across_transports` 保证。

根因都一样：`ws_server` / `peerjs_server` 是把 op 名**直接透传给** `service.handle`，
而 `grpc_server` 是**一张 op→RPC 的手写映射表** —— 表上没写的就用不了。所以「加了新 op
两个翻译层都要跟」这条约定（《为自己写》见 `arch-one-impl-three-transports.md`）目前只对
鼠标键盘那部分成立。

**加完 op 要动的五处**：`service.py` 的 handlers/aliases → proto + 重新
`bash remote/proto/gen_proto.sh` → `grpc_server.py` 的映射 → `client.py` 的构造与
子命令 → `ctl.py` 的命令翻译。
少一处就静默少一条传输，跑
`test_remote.py::test_new_ops_agree_across_transports` 也发现不了 —— 它只测表里已有的 op。
（`screen.calibrate` 是 2026-09-20 加的，五处一开始就都跟上了，所以它没进上面那张表。）

## 小瑕疵

- 文档里客户端示例仍是 `/tmp/shot.png`、`/tmp/frames` 这类 Linux 风格路径
  （受控端已限定 Windows，示例该跟着改）
- `legacy/deepseek.html` 已加进 `.gitignore`（文件保留，只是不入库）
