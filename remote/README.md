# kuuki-mouse · remote (远程控制扩展)

把 kuuki-mouse 从"手机当空气鼠标"扩展成一台**本机的电脑操作服务**:
用鼠标、键盘操作这台机器, 并且能把屏幕截图取回来; 通过 **PeerJS** / **WebSocket** /
**gRPC** 三种传输对外暴露, 供 agent / 脚本 / 别的机器调用。三者 op 语义完全一致。

> ### ⚠️ 受控端只支持 Windows
> "受控端"指**被操作的那台机器**, 也就是这里这个服务本身 —— 它必须**原生跑在
> Windows 上**。非 Windows 会在启动之前直接拒绝 (退出码 2, 提示里给出可照做的命令,
> 见第 2 节)。WSL / Linux / 手机一侧只跑**客户端** (`python -m remote.client …`),
> 客户端不挑平台。
>
> 这条是**产品定位**, 不是临时限制: 屏幕采集走 `PIL.ImageGrab`, 输入注入走 pynput
> 的 Win32 后端, 目标桌面只有 Windows 一种。曾经在 WSLg / X11 上跑过, 那些分支在
> 代码里保留作参考但**不再维护** (第 8 节留了当时的实测结论)。

```
┌──────────────┐   PeerJS   kuuki-mouse-<房间码>    ┌─────────────────────────┐
│ agent / 脚本 │   WebSocket  ws://127.0.0.1:8765    │  RemoteService (ops)    │
│ 手机 / 异地  │◄───────────────────────────────────►│   ├─ InputController    │──► pynput ──► 鼠标/键盘
└──────────────┘   gRPC       127.0.0.1:50051        │   └─ ScreenCapture      │──► PIL.ImageGrab
                                                     └─────────────────────────┘
                                              (右半边这一整块只在 Windows 上跑)
```

**只做三件事**: ① 控制鼠标键盘; ② 抓屏并编码返回; ③ 顺带兼容原有空气鼠标协议
(老协议 JSON 直接透传给 `app.handle_message`, 所以 `web/` 页面可以不用 PeerJS,
直接把传感器数据发到这个 WS)。

---

## 1. 安装

```bash
pip install -r requirements.txt -r requirements-remote.txt
```

- **服务端只装/只跑在 Windows 上** (见开头的定位说明); 客户端用同一份依赖, 平台不限。
- `pynput` / `Pillow` 已由 `requirements.txt` 提供, 不重复安装。截屏只有一个后端
  (`PIL.ImageGrab`), 没有可选后端也没有降级链 —— 见第 8 节。
- gRPC 存根已随仓库提供 (`remote/proto/kuuki_remote_pb2*.py`); 改过 `.proto` 后重新生成:

  ```bash
  bash remote/proto/gen_proto.sh
  ```

## 2. 启动

```bash
python -m remote                          # 三个传输全开: WS 8765 + gRPC 50051 + PeerJS
python -m remote --no-peerjs              # 只要本机两个端口 (不连公开 broker)
python -m remote --no-grpc                # WebSocket + PeerJS
python -m remote --no-ws --no-grpc        # 只要 PeerJS (房间码配对, 不需要端口)
python -m remote --ws-port 9000 --grpc-port 9001
python -m remote --room ABCD123           # 指定 PeerJS 房间码 (默认随机生成)
python -m remote --token secret           # 三个传输都要求 token
python -m remote --allow-remote --token secret   # 绑 0.0.0.0 (必须带 token)
python -m remote --selftest               # 自检: 报告环境 + 抓一帧, 不动鼠标
python -m remote --selftest --selftest-input     # 额外测一次鼠标移动(会动光标)
```

**非 Windows 拒绝启动** (退出码 2, 与 `--allow-remote` 缺 token 的拒绝一致):

```
拒绝启动: 受控端只支持 Windows, 当前平台是 'linux'。

  受控端 = 被操作的那台机器, 它必须原生跑在 Windows 上:
      Windows 侧:  python -m remote        (或 start-win.bat)

  要从 WSL / Linux / 手机侧操作它, 在那边只跑客户端, 连到 Windows 上的服务端:
      python -m remote.client peerjs --peer kuuki-mouse-<房间码> info
      ...
```

`--help` / `--version` 不受门禁影响, 照样能用。

启动后打印 (三传输全开时的真实格式):

```
kuuki remote 0.1.0 已启动
  WebSocket : ws://127.0.0.1:8765/
  gRPC      : 127.0.0.1:50051  (kuuki.remote.v1.RemoteControl)
  PeerJS    : kuuki-mouse-ABCDE  (已注册, 0.peerjs.com:443 (公开 cloud broker))
  token     : 未设置 (仅回环安全)
  客户端示例: python -m remote.client ws ping
```

## 3. 命令行客户端 (调试用)

```bash
python -m remote.client ws ping
python -m remote.client ws info
python -m remote.client ws op mouse.position
python -m remote.client ws op mouse.move --args '{"x":400,"y":300,"duration":0.2}'
python -m remote.client ws op keyboard.check --args '{"keys":["a","enter","f13","中"]}'
python -m remote.client ws screenshot /tmp/shot.png --max-width 1280 --draw-cursor
python -m remote.client ws watch /tmp/frames --fps 2 --count 5 --format jpeg --max-width 1280
python -m remote.client grpc info
python -m remote.client grpc screenshot /tmp/shot.jpg --format jpeg --quality 70
python -m remote.client grpc stream /tmp/frames --fps 2 --count 5
```

带 token 时加 `--token XXX` (WS 拼进 URL + 握手头, gRPC 放进 metadata)。

## 4. WebSocket 协议

请求 (文本帧, JSON):

```jsonc
{"id": 1, "op": "mouse.move", "args": {"x": 100, "y": 200}}
{"id": 2, "op": "mouse.move", "x": 100, "y": 200}          // args 可平铺
{"id": 3, "batch": [{"op": "ping"}, {"op": "mouse.position"}]}
```

响应:

```jsonc
{"id": 1, "ok": true,  "result": {"x": 100, "y": 200}}
{"id": 1, "ok": false, "error": {"code": "bad_request", "message": "..."}}
```

错误码: `bad_request` / `unknown_op` / `unauthorized` / `internal`。

### 二进制截屏帧

`screen.grab` (或 `screen.screenshot` + `"binary": true`) 回一帧二进制:

```
[4 字节大端: JSON 头长度 N][N 字节 JSON 头][图片字节]
```

头里带 `event/format/width/height/bytes/ts/duration_ms/backend`。
`screen.watch` 按 fps 持续推同样的帧 (头里 `event="frame"`, 带 `watch_id`/`seq`):

```jsonc
{"op": "screen.watch", "args": {"fps": 2, "format": "jpeg", "quality": 60, "max_width": 1280}}
{"op": "screen.unwatch", "args": {"watch_id": "w1"}}
```

### 兼容原空气鼠标协议

不带 `op`、但带 `t` / `mouse` / `text` / `key` 的消息会被直接交给 `app.handle_message`,
即 `web/` 页面可以不走 PeerJS, 直接把传感器数据发到本机 WS:

```js
const ws = new WebSocket("ws://127.0.0.1:8765/");
ws.onopen = () => ws.send(JSON.stringify({
  t: "sensor", x: 0, y: 0, z: 9.8, alpha: 0, beta: 0, gamma: 0, gx: 0, gy: 0, gz: 0,
}));
```

### 鉴权

设置 token 后, 三种方式任一即可:

1. `ws://host:port/?token=XXX`
2. 握手头 `Authorization: Bearer XXX` (或 `X-Kuuki-Token: XXX`)
3. 首条消息 `{"op":"auth","args":{"token":"XXX"}}`

未通过鉴权只允许发 `auth`, 其余请求回 `unauthorized` 并断开 (1008)。

## 5. PeerJS 传输 (推荐跨网络用)

和 WS/gRPC 并列的第三种传输, **op 语义完全一致**。走公开 cloud broker
(`0.peerjs.com:443`), 靠房间码配对 —— **不需要端口、域名、证书、端口转发**。

### ⚠️ 这是**两个端点**, 服务端必须跑在你要控制的那台机器上

PeerJS 不是一个"端口", 而是两端各自连 broker、由 broker 牵线:

```
 被控端 (要操作的那台机器)              控制端 (发起方)
   python -m remote --no-ws --no-grpc      PeerJsClient("kuuki-mouse-<房间码>")
        │                                        │
        └────────► 0.peerjs.com:443 ◄────────────┘
              (只做牵线, 数据走 P2P)
```

**最常见的错误**: 在 A 机器起了服务, 却想让 B 机器"连自己" —— 那没有服务端。
要在哪台机器上动鼠标键盘、截哪台机器的屏, **服务端就必须跑在那台机器上**。

服务端**只能跑在 Windows 上** (见开头的定位说明), 所以"想控制谁"就只剩一个答案:

| 想控制谁 | 服务端跑在哪 | 控制端跑在哪 |
|---|---|---|
| **Windows 桌面** | **必须**是 Windows (`start-win.bat --no-ws --no-grpc`) | WSL / Linux / 手机 / 任何地方 |

控制端不挑平台 —— 它只发起出站连接, 不碰自己的桌面。

```bash
# 被控端 (Windows): 只开 PeerJS, 打印房间码, 不需要任何端口
start-win.bat --no-ws --no-grpc --room ABCDE

# 控制端 (WSL / Linux / 异地 / 手机): 连过去
python -m remote.client peerjs --peer kuuki-mouse-ABCDE op mouse.position
```

> 曾经的做法是"服务端跑在 WSL 里、去操作 Windows 桌面", 现在**不允许**了: 受控端限定
> Windows, 而且那条路截到的也一直不是 Windows 桌面 (见第 8 节)。`remote/win/` 下那套
> PowerShell 桥 (WSL → Windows) 仍在仓库里, 但**没有接入服务端**, 属实验性代码。

**为什么这里不用管防火墙/端口**: WS 与 gRPC 绑 `127.0.0.1` 就出不了本机, 绑
`0.0.0.0` 又要开防火墙、还要处理 WSL↔Windows 的网关地址; PeerJS 两端都只**出站**
连 broker, 不监听任何端口, NAT 后面也能用 —— 这是它在跨机场景下比 WS/gRPC 省事的地方。

主机 id = `kuuki-mouse-<房间码>`, 客户端用随机 id 连过来:

```python
import asyncio
from remote.peerjs_client import PeerJsClient

async def main():
    async with PeerJsClient("kuuki-mouse-ABCDE", token="secret") as client:
        print(await client.call("mouse.position"))
        await client.call("mouse.move", {"x": 400, "y": 300})
        await client.call("keyboard.type", {"text": "hello"})
        png = await client.screenshot({"format": "jpeg", "quality": 70, "max_width": 1280})
        open("shot.jpg", "wb").write(png)

asyncio.run(main())
```

### ⚠️ Python fork 的两个硬限制 (决定了协议长相)

1. **只能 JSON 序列化** —— fork 里 `SerializationType.Binary` 的收发代码是**注释掉的**,
   所以图片必须 base64 (比 WS 的二进制帧大约 +33%)。
2. **库内分块也是注释掉的** —— `DataConnection.handleMessage` 里处理 `__peerData`
   的那段被注释, 所以**大消息要自己在协议层分块**。

因此 PeerJS 传输多了一层分块 (WS 侧没有):

```
{"_chunk":"head","_id":"c1","total":85,"size":691816,"header":{"id":1,"ok":true}}
{"_chunk":"data","_id":"c1","n":0,"d":"<base64 片段>"}
...
{"_chunk":"end","_id":"c1"}
```

超过 60KB 的响应才分块, 每块 8KB; 带 `_chunk` 的消息与普通消息不会撞车。
**实测**: 1600x1200 噪声 PNG → 691KB base64 → 87 块 → 完整重组且图片可解码;
乱序到达也能重组, 缺块则明确丢弃 (不给坏数据)。

### fork 补丁 8: `peer.connect()` 传 dict 必炸

上游 `Peer.connect(peer, options)` 默认值是可变字面量 `{}` (dict), 实现里却调
`dataclasses.asdict(options)` —— 照抄 JS 文档的 `{serialization:'json'}` 会直接
`TypeError: asdict() should be called on dataclass instances`。已修: 默认改 `None`
并兼容 dict 入参。

## 6. gRPC 接口

服务 `kuuki.remote.v1.RemoteControl`, 定义在
[`remote/proto/kuuki_remote.proto`](proto/kuuki_remote.proto)。

| RPC | 说明 |
|---|---|
| `GetInfo` / `Ping` | 环境信息 / 连通性 |
| `Screenshot(ScreenshotRequest) → Image` | 抓一帧, `data` 是图片字节 |
| `StreamScreenshots(StreamScreenshotsRequest) → stream Image` | **服务端流式推帧** (WS 侧要自己轮询) |
| `GetMonitors` | 屏幕列表 |
| `GetMousePosition` / `MoveMouse` / `MoveMouseRelative` | 光标 |
| `ClickMouse` / `MouseDown` / `MouseUp` / `Scroll` / `Drag` | 鼠标动作 |
| `TypeText` / `PressKey` / `Hotkey` / `PasteText` | 键盘 |
| `SendKuukiMessage` | 老协议 JSON 透传 |

鉴权: metadata `authorization: Bearer <token>`。

> **proto3 JSON 的小坑**: `python -m remote.client grpc ...` 打印的是 proto3 JSON,
> **默认值 (false / 0 / "") 会被省略**。例如 `CheckKeys` 里不可用的键只会出现
> `reason` 而没有 `supported`, `info` 在没设 token 时不会出现 `token_required`
> ——按"缺省即 false"读即可。WS 侧没有这个问题 (显式给 `false`)。
> 客户端已用 `preserving_proto_field_name=True`, 所以字段名与 WS 一样是 snake_case。

## 7. 操作 (op) 一览

WS 的 `op` 与 gRPC 的 RPC 语义一致; 带 `*` 的是短别名。

| op | 参数 | 说明 |
|---|---|---|
| `ping` | — | 连通性 + uptime |
| `info` | — | 系统/屏幕/后端/剪贴板/能力清单 |
| `screen.size` *`size`* | — | 屏幕尺寸 |
| `screen.monitors` *`monitors`* | — | 屏幕列表 (index 0 = 整个虚拟桌面) |
| `screen.screenshot` *`screenshot`/`capture`* | `format` `quality` `region` `monitor` `max_width` `max_height` `scale` `draw_cursor` `include_image` `binary` | 抓一帧 |
| `screen.grab` | 同上 | 同上, 强制二进制帧 (WS) |
| `screen.watch` / `screen.unwatch` | `fps` `count` `watch_id` | 推流 |
| `mouse.position` *`position`* | — | 当前光标 |
| `mouse.move` *`move`* | `x` `y` `duration` | 绝对定位 (`duration>0` 平滑) |
| `mouse.move_rel` *`move_rel`* | `dx` `dy` `duration` | 相对移动 |
| `mouse.click` *`click`* | `button` `clicks` `interval` | 点击 |
| `mouse.down` / `mouse.up` | `button` | 按住 / 松开 |
| `mouse.scroll` *`scroll`* | `dx` `dy` (兼容老协议 `delta`) | 滚轮, `dy>0` 向上 |
| `mouse.drag` *`drag`* | `x1` `y1` `x2` `y2` `button` `duration` | 拖拽 |
| `keyboard.type` *`type`/`text`* | `text` `interval` | 输入文本 |
| `keyboard.key` *`key`* | `key` `action`(tap/press/release) `modifiers` | 单键 |
| `keyboard.hotkey` *`hotkey`* | `keys` (`"ctrl+shift+s"` 或数组) | 组合键 |
| `keyboard.paste` *`paste`* | `text` | 写剪贴板 + Ctrl/Cmd+V (**中文/emoji 用这个**) |
| `keyboard.check` *`check`/`keys`* | `keys` 或 `key` | **预检**键能不能发 (不按键) |
| `kuuki` *`sensor`* | `message` | 老协议透传 |

## 8. 截屏实现与已知限制

### 只有一个后端: `PIL.ImageGrab`

曾经有 `mss` → `Pillow` → `ffmpeg -f x11grab` 三级降级 + 失败拉黑, 实测只有一个跑得起来,
另外两个纯属负担, 已整条删掉 —— 真跑不了就报错, 让人看见。

同样不做的事还有屏幕尺寸探测: 曾经有 Xlib / xdpyinfo / `GetSystemMetrics` / 环境变量
四套, 还会和截图实际尺寸对不上 (实测报 1920x1080 而截图是 1680x1050), 现在直接取截图
本身的 `.size`, 一条路, 永远准。

所以 `info` 里**没有** `capture_backends` 字段了 (只有一个后端, 没什么可报);
WS / gRPC 的截图响应里带 `"backend": "pillow"`。

**2026-09-20 Windows 实测** (`.venv-win`, py3.10.7, 1680x1050): 整屏 PNG 约 322 KB,
PNG 魔数正确; region 裁剪 + `max_width` 缩放 (320x200 区域 → 160x100) 正常。
`info.clipboard_tool` 在 Windows 上命中 `clip`。

### ⚠️ 中文 / emoji 用 `keyboard.paste`, 不要用 `keyboard.type`

`keyboard.type` 是逐字符注入, 非 ASCII 不稳; 中文 / emoji 走 `keyboard.paste`
(写剪贴板 + 发 `Ctrl+V`)。剪贴板工具探测顺序
`wl-copy` → `xclip` → `xsel` → `pbcopy` → `clip`。

### 历史实测: WSLg / X11 (不再支持, 仅作参考)

受控端限定 Windows 之前, 服务端在 WSL2 + WSLg 上跑过。当时的结论解释了为什么现在
必须原生跑 Windows:

- **截到的不是 Windows 桌面**: 1680x1050 的 WSLg X 显示内容, 没有窗口时就是全黑,
  **不包含 Windows 桌面 / Windows 应用窗口 / Windows 光标**。
- `PIL.ImageGrab` 和 `python-xlib` 直连 `root.get_image()` 都在 root window 上失败
  (`OSError: X get_image failed: error 8`, BadMatch) —— WSLg 的 XWayland 不支持
  `GetImage`; 当时能用的是 `ffmpeg -f x11grab`, 约 240–320 ms/帧 (含进程启动)。
- **X11 键预检**: pynput 遇到键盘映射里没有的键会走"借键"路径 (临时
  `change_keyboard_mapping` 改布局), 在 WSLg 的 XWayland 上这会把整条 X 连接**打死** ——
  实测 `Key.f13` 抛 `AttributeError: 'BadRRModeError' object has no attribute
  'sequence_number'`, 之后同一条连接上连 `esc` / `a` / `enter` 都**永久阻塞**
  (换新进程才正常)。当时的对策是按键前做纯查询式预检: 未映射的键 → 干净的
  `bad_request`, 出错后重建键盘控制器; `keyboard.type` 会先整串预检, 只要有字符打不出来
  就整体拒绝并提示改用 `keyboard.paste`。可以这样问一句:

  ```bash
  python -m remote.client ws op keyboard.check --args '{"keys":["a","enter","f1","f13","中"]}'
  # f13 -> {"supported": false, "reason": "在当前 X 键盘映射里没有对应 keycode"}
  # 中  -> {"supported": false, "reason": "不在当前 X 键盘映射里 ..."}
  ```

  X11 下 pynput 也打不出中文 —— 这正是 `keyboard.paste` 存在的原因。

这些分支 (`remote/input.py` 的 `_LINUX`、`remote/screen.py` 的 `xdisplay` 分支) 仍留在
仓库里并标了「不再维护」, 但服务端在非 Windows 根本不会启动, 所以是**死代码**。
`keyboard.check` 这个 op 本身还有用 (Windows 上恒为 supported), 保留。

`draw_cursor: true` 会把光标位置画成红色十字+圆圈 (截图本身通常不含光标)。

## 9. 安全

- 默认只绑 `127.0.0.1`。
- 不设 token 时**任何能连到该端口的本机进程都能控制你的鼠标键盘并读屏**。
- 要暴露到网络必须 `--allow-remote --token <强随机值>`, 且建议再套一层 SSH 隧道 / 反向代理。
- gRPC 用的是 `insecure_channel` (明文), token 只是防误连, 不是加密; 跨机请走隧道。

## 10. 测试与验证状态

```bash
python -m pytest test_remote.py -v      # 15 项
python -m remote --selftest --selftest-input
```

**2026-09-20 Windows 实测** (`.venv-win`, py3.10.7, 1680x1050):

- 受控端正常启动 (`python -m remote --no-grpc --no-peerjs --ws-port 8766`),
  客户端 `python -m remote.client ws --url ws://127.0.0.1:8766 ping` 与同一命令换 `info`
  端到端都通, `info` 报 `os=Windows` / `clipboard_tool=clip`。
- 平台门禁: 伪装成非 Windows 时 `main()` 返回 2 并在 stderr 给出提示 (`--selftest` 同样被拦);
  Windows 上 `--help` / `--version` 不受影响。
- 真实抓屏: `PIL.ImageGrab` 1680x1050, PNG 魔数正确; region 裁剪 + 缩放正常。
- 光标位置读取正常 (`mouse.position`); `keyboard.check` 在 Windows 上恒为 supported。

> 跑整套测试前先 `pip install pytest` —— `.venv-win` 里没装, 所以上面这些结论是直测
> 出来的, 不是 `pytest` 的汇总。

**2026-09-19 本机 (WSL2/WSLg, conda py3.12, `DISPLAY=:0`) 实测通过** —— 该路径自受控端
限定 Windows 起不再支持, 结论保留在第 8 节:

- `test_remote.py` 当时 14 项全过 (键名解析 / 区域 / 编码 / 裁剪缩放 / 光标叠加 /
  服务调度 / 请求信封与 batch / kuuki 透传 / 键预检 / WS 帧编解码 / WS 端到端 /
  gRPC 端到端含流式与鉴权失败 / PeerJS 分块协议)。
- 真实抓屏走 ffmpeg 后端; 真实键盘用 `pynput.keyboard.Listener` (XRecord) 抓 XTEST
  注入事件验证 (`kuuki-remote` 逐字符到达, `enter` 与 `ctrl+shift+k` 到达)。
- 未实测: 多显示器 (本机只有一块虚拟屏)、跨机网络调用。

测试套件**不动鼠标键盘**; 会按键/移光标的验证都在 `--selftest-input` 与冒烟脚本里,
需要显式开启。
