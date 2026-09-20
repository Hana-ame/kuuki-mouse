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
python -m remote                          # 默认只开 PeerJS (房间码配对, 不需要端口)
python -m remote --ws                     # 只开 WebSocket 8765
python -m remote --grpc                   # 只开 gRPC 50051
python -m remote --ws --grpc              # 本机两个端口 (不连公开 broker)
python -m remote --ws --peerjs            # WebSocket + PeerJS
python -m remote --ws --grpc --peerjs     # 三个全开
python -m remote --ws --ws-port 9000 --grpc --grpc-port 9001
python -m remote --room ABCD123           # 指定 PeerJS 房间码 (默认随机生成)
python -m remote --token secret           # 所有已开的传输都要求 token
python -m remote --allow-remote --token secret   # 绑 0.0.0.0 (必须带 token)
python -m remote --selftest               # 自检: 报告环境 + 抓一帧, 不动鼠标
python -m remote --selftest --selftest-input     # 额外测一次鼠标移动(会动光标)
python -m remote --qr                     # 额外打印配对二维码 (终端 + pair_<房间码>.png)
python -m remote --page-url https://me.github.io/kuuki-mouse/   # 页面不在默认地址时
```

**开关语义**: 点名即选择 —— 给了任何 `--ws` / `--grpc` / `--peerjs` 就以给的那几个为准,
一个都没给才用默认 (只 PeerJS)。所以 `--ws` 是"只要 WebSocket", 不会顺带把 PeerJS 也
注册到公开 broker 上。`--no-xxx` 是在这个结果上再减 (`--ws --no-peerjs` 仍是只要 WS)。

一个传输都不开会拒绝启动; 给了 `--ws-port` / `--grpc-port` / `--room` 却没开对应传输
同样拒绝 —— 这类组合换了端口服务却没起来, 静默放行会让人排查半天。

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
  token     : 未设置 —— PeerJS 经公开 broker 配对, 知道房间码的人都能控这台机器
              不可信网络下请先加 --token <口令> (手机端配对时要填同一个)
  客户端示例: python -m remote.client ws ping
```

> **"仅回环安全"这个说法只在关掉 PeerJS 时才成立。** PeerJS 是本机主动连出去注册到
> 公开 broker 的，别人拿到房间码就能连进来 —— 和本地绑 `127.0.0.1` 还是 `0.0.0.0`
> 没有关系。房间码 31⁵ ≈ 2860 万种，够挡误撞，挡不住有意枚举：它是配对用的，不是凭证。
> 只在可信网络里裸奔；不确定就加 `--token`（手机端配对区填同一个值，或把
> `#/<房间码>?token=<口令>` 做成二维码）。

## 3. 控制端: 命令行客户端 + 多机控制器

### 3.1 `python -m remote.client` (单机调试级)

一条命令打**一台**机器**一个** op，地址临时写在命令行上，**不记任何东西**。

```bash
python -m remote.client ws ping
python -m remote.client ws info
python -m remote.client ws op mouse.position
python -m remote.client ws op mouse.move --args '{"x":400,"y":300,"duration":0.2}'
python -m remote.client ws op mouse.scroll --args '{"dy":10,"steps":5,"x":800,"y":400}'
python -m remote.client ws op mouse.drag --args '{"points":[[100,100],[300,200],[500,150]],"duration":0.2}'
python -m remote.client ws op keyboard.combo --args '{"keys":"ctrl+shift+s","hold_ms":150}'
python -m remote.client ws op keyboard.hold --args '{"key":"f2","ms":600}'
python -m remote.client ws op keyboard.check --args '{"keys":["a","enter","f13","中"]}'
python -m remote.client ws windows --process msedge
python -m remote.client ws focus --title "Gemini"   # 切前台 + 确认, 之后输入有确定归宿
python -m remote.client ws screenshot /tmp/shot.png --max-width 1280 --draw-cursor
python -m remote.client ws calibrate --max-width 1280 --save /tmp/calib.json
python -m remote.client ws locate --saturated --calib /tmp/calib.json --max-width 1280
python -m remote.client ws watch /tmp/frames --fps 2 --count 5 --format jpeg --max-width 1280
python -m remote.client grpc info
python -m remote.client grpc screenshot /tmp/shot.jpg --format jpeg --quality 70
python -m remote.client grpc stream /tmp/frames --fps 2 --count 5
```

带 token 时加 `--token XXX` (WS 拼进 URL + 握手头, gRPC 放进 metadata)。

#### 3.1.1 `locate`: 让客户端自己算出来该点哪儿

`screenshot` 只把图给你 —— 还得**有人看图**才知道往哪点。`locate` 把这一步也做掉:
抓一帧 → 在图上找 → **直接吐出可以喂给 `mouse.click` 的真实屏幕坐标**。算法都在
`remote/vision.py`, 纯 Pillow 实现, **不新增 numpy / OpenCV 依赖**。

```bash
# 网格概览 (默认模式): 切成 8x5 块, 报每块主色与"内容量" (标准差, 留白接近 0)
python -m remote.client ws locate --describe --max-width 1000 --top 40

# 主色调: 先看这一屏由哪些颜色构成, 再挑一种去精确定位
python -m remote.client ws locate --dominant --top 10

# 按颜色定位 —— 例: 浏览器标签栏那条窄带里的 favicon
python -m remote.client ws locate --color "#1a73e8" --region 0,0,1680,90 --top 5

# 找"任何颜色鲜艳的小方块" —— 不知道目标颜色时的第一招 (图标 / 按钮)
python -m remote.client ws locate --saturated --region 0,0,1680,90 --top 20

# 模板匹配: 先裁一块存成模板, 之后按形状找它
python -m remote.client ws locate --box 100,200,40,24 --save-template send.png
python -m remote.client ws locate --template send.png --threshold 0.85

# 帧差: 找出操作前后的变化区 —— 用来确认"这一下点没点出反应"
python -m remote.client ws locate --save-frame before.png
python -m remote.client ws locate --diff-with before.png
```

每个匹配同时给**两套坐标** —— `center` 是图上的位置, `screen` 是乘过缩放系数、
能直接丢给 `mouse.click` 的真实屏幕坐标:

```json
{"mode": "color", "scale": 1.68, "matches": [
  {"x": 100, "y": 50, "w": 40, "h": 20, "center": {"x": 120, "y": 60},
   "screen": {"x": 202, "y": 101}, "score": 1.0}]}
```

> **`scale` 靠帧头里的 `source_width` 算**, 而 `--max-width` 会把图缩小 —— 早先 WS
> 的二进制帧头漏了 `source_width`, `scale` 恒等于 1.0, 于是定位出来的坐标全部偏掉
> 一个缩放比。已修, 详见 `docs/knowledge/env-ws-frame-missing-source-width.md`。
>
> 不过**帧头里只有缩放, 没有平移**: 多显示器时虚拟桌面向左向下扩展, 图的原点未必
> 是鼠标的 (0, 0), 而这个偏移量帧头里根本没有。乘算补不回来, 得实测 —— 见 3.1.2。

> **纯视觉认不出「这是哪个窗口」**: 一排彩色小图标既可能是浏览器标签栏, 也可能是
> 某个桌面应用的会话列表, 光看图像区分不了 —— 2026-09-20 实战翻过车。现在有
> `window.list` / `window.focus` 了, 先用标题与进程名锁定应用、切到前台, 再在窗口
> 内部 locate (详见 `docs/knowledge/gui-window-focus-gap.md`)。

### 3.1.2 `calibrate`: 把"图坐标 → 鼠标坐标"实测出来

`locate` 报的 `screen` 默认是用帧头里的缩放系数**乘**出来的。乘算只能表达缩放,
表达不了平移 —— 多显示器上虚拟桌面允许负坐标, 图的 (0, 0) 可能是鼠标的
(-1920, 0), 每个点都该减去它。帧头里没这一项, 只能测。

`calibrate` 不猜, 走闭环: 鼠标移到已知座标点 → 抓一帧并把光标画上去 → 帧差找出
"这一屏哪儿变了" → 在变化区里认出那个红十字 → 拿一排这样的点对拟合出
`screen = a × frame + b`。全程用的都是仓库里已有的东西 (`mouse.move` /
`draw_cursor` / `vision.diff` / `vision.find_color`), 没有新依赖。

```bash
# 默认 3x3 = 9 个靶点; 会动鼠标, 跑完默认把光标挪回原位
python -m remote.client ws calibrate

# 顺带按 1280 宽缩放做, 并把 fit 存下来
python -m remote.client ws calibrate --max-width 1280 --save /tmp/calib.json

# 之后 locate 就用实测出来的换算 (缩放 + 平移), 不再只看帧头
python -m remote.client ws locate --saturated --calib /tmp/calib.json --max-width 1280
```

报告的重点字段 (1680x1050 按 1280 宽跑的真实输出):

```json
{"ok": true, "requested": 9, "sampled": 9, "missed": [],
 "declared_scale": 1.3125, "fit_scale": 1.3114, "scale_drift": 0.0008,
 "origin": {"x": 0.481, "y": -0.656},
 "residual": {"rmse": 0.3091, "max": 0.4371},
 "verdict": "aligned", "outliers": [], "restored": true,
 "advice": "图左上角对应鼠标坐标 (0.5, -0.7) —— 虚拟桌面有偏移, 只用 vision.scale_of
            换算会整体偏这么多; 残差 0.44px <= 容差 2.0px, 这条换算可用。"}
```

四件事值得先知道:

- **`verdict` 不是"命令跑成功了"**。认不出标记时它是 `too_few_samples`, 而且这时
  **不给** `calibration` —— 宁可让你重跑, 也不给一个假的换算去用。
- **`outliers` 是认错的标记**。屏幕上动的不止光标, 尺寸凑巧的红色 UI 会混进来:
  本机实测一度 9 个点错 4 个, 直接拟合出的系数差了 19%。所以拟合走投票, 剔掉的
  点连同误差列在这里供复查; 互相一致的点凑不够多数票时干脆报错。
- **`restored` 为真才代表光标回去了**。校准必然动鼠标, 这是副作用。
- **`origin` 是平移量**。单屏上接近 (0, 0), 多显示器上它才是校准的主要价值所在。

详见 `docs/knowledge/gui-coordinate-calibration.md`。

### 3.1.3 `monitors`: 先说清楚有几块屏, 再谈坐标

上面那条 `origin` 在多显示器上才是它存在的理由 —— 副屏排在主屏左边时虚拟桌面
原点是**负的**, 只用 `screen.size` 换算会整体偏一整块屏。编排前先看一眼:

```bash
python -m remote.client ws monitors                     # 列全部 + 虚拟桌面边界
python -m remote.client ws monitors --x -100 --y 500    # 只问: 这个点在哪块屏上
```

```json
{"count": 1,
 "virtual_screen": {"left": 0, "top": 0, "width": 1680, "height": 1050},
 "primary_index": 0,
 "monitors": [{"index": 0, "handle": 961028267, "device": "\\\\.\\DISPLAY20",
               "rect": {"left": 0, "top": 0, "width": 1680, "height": 1050},
               "work": {"left": 0, "top": 0, "width": 1680, "height": 1010},
               "primary": true}]}
```

- `rect` 是整块屏, `work` 是去掉任务栏的工作区, 都是**虚拟桌面坐标** (可以是负数)。
- 给了 `x`/`y` 就只回"这个点在哪块屏"; 只给其中一个按"没给"处理 (退化成列全部),
  点不在任何一块屏上时报错 `not_found`, 不猜一个。
- 非 Windows 受控端直接回 `unsupported` —— 不给假屏幕。
- **只做只读枚举**: 不改显示器排列, 也不动进程的 DPI 感知状态 (那是有副作用的
  全局设置)。知道边界之后, "截图该抓哪块屏" 见 3.1.4。

### 3.1.4 多显示器下抓哪一屏: `monitor` / `all_screens` + `origin`

`PIL.ImageGrab.grab()` **无参数调用抓的是主显示器, 不是整个虚拟桌面** (它走的是
`SM_CXSCREEN`)。单屏机器上两者一样, 所以这个区别很容易被忽略; 多屏时它意味着
"图上的 (0,0)" 未必是 "鼠标的 (0,0)" —— 差的那一截就是下面这个 `origin`:

```bash
python -m remote.client ws screenshot a.png                 # 默认: 主显示器
python -m remote.client ws screenshot b.png --monitor 1     # 只抓第 1 块屏
python -m remote.client ws screenshot c.png --all-screens   # 抓整个虚拟桌面
python -m remote.client ws op screen.screenshot --args '{"include_image": false}'
```

```json
{"format": "png", "width": 1680, "height": 1050,
 "source_width": 1680, "source_height": 1050,
 "origin": {"x": 0, "y": 0}, "backend": "pillow", "...": "..."}
```

- `origin` = 这一帧左上角在**虚拟桌面坐标系**里的位置 (就是鼠标坐标那一套)。
  **图坐标 + origin = 鼠标坐标** —— `region` 与画上去的光标都按这个换算,
  少了它会整体偏一整块屏。
- `monitor=<下标>` 与 `all_screens` 只能给一个; 下标越界、两个都给都报
  `bad_request`, 不悄悄退回主屏。下标 `0` 是合法值 (第一块屏), 所以 proto 里
  这两个字段都是 `optional` —— 普通标量分不清"没给"与"给 0"。
- `region` 一律是**帧内坐标** (相对这一帧的左上角), 不是虚拟桌面坐标。

### 3.1.5 `ocr`: 认出屏幕上写着什么

`locate` 能说"这块像按钮", 说不出上面写着什么 —— 于是调用方知道该点哪儿, 却不知道
自己点的是"发送"还是"清空"。`screen.ocr` 补上这一层, 而且**每行每词都给出能直接点的
坐标** (三步换算 service 那层已经做掉: 除缩放 → 加裁剪偏移 → 加帧原点):

```bash
python -m remote.client ws ocr                          # 默认: 认主屏, 逐行简报
python -m remote.client ws ocr --json                   # 要完整结构 (词矩形/语言/scale)
python -m remote.client ws ocr --region 0,0,800,600     # 只认左上角那一块
python -m remote.client ws ocr --monitor 1 --lang zh-Hans-CN
python -m remote.client ws ocr --no-words               # 不要逐词矩形 (省体积)
python -m remote.ctl ocr -a                             # 多机: 每台各自认各自的屏
```

```json
{"ok": true, "text": "发送\n清空", "language": "zh-Hans-CN", "count": 2,
 "width": 1680, "height": 1050, "scale": 1.0, "origin": {"x": 0, "y": 0},
 "lines": [{"text": "发送", "x": 120, "y": 300, "w": 60, "h": 28,
            "screen": {"x": 120, "y": 300, "w": 60, "h": 28},
            "words": [{"text": "发送", "x": 120, "y": 300, "w": 60, "h": 28,
                       "screen": {"x": 120, "y": 300, "w": 60, "h": 28}}]}]}
```

- 走**系统内置的 Windows OCR** (`Windows.Media.Ocr`, 经 PowerShell 调 WinRT),
  所以 **零 pip 依赖、离线可用**, 但也因此**只有 Windows 受控端有** —— 别的平台
  直接回 `unsupported`, 不给假结果。
- 代价是每次调用要起一次外部进程, 约 1 秒; 大图 + 中文的识别本身也要几秒。
  **别拿它当逐帧监控用**, 它是"需要读字的时候读一次"的那种工具。
- `lang` 空着表示按受控端用户的语言偏好挑 (中文界面上就是中文引擎); 给了就**只用**
  那个语言, 没装对应语言包直接报 `bad_request`, 不悄悄换引擎 (换了的表现是中文被
  当别的语言认, 调用方完全看不出来)。
- `--no-words` 之后 `words` 是**空列表**而不是"没有这个键" —— protobuf 的空
  repeated 在转 dict 时照样会出现, 两条传输必须一致。
- 中文常被切成一个字一个"词" (`"中 文 识 别"`): WinRT 的 OcrWord 就是这么切的,
  要整句就直接用 `lines[].text`。

### 3.1.6 `find-text`: 找写着某段文字的那块, 直接给能点的坐标

`ocr` 把整屏的字都给出来, 而调用方多半只要一句"『发送』在哪"。`screen.find_text`
把认、筛、排序、算中心点合成一次调用 —— 返回值里的 `center` 就是**屏幕坐标**上的
中心点, 直接喂给 `mouse.click`:

```bash
python -m remote.client ws find-text 发送                     # 默认按行、只给最靠上的
python -m remote.client ws find-text OK --unit word --all     # 按词匹配, 给全部命中
python -m remote.client ws find-text '[0-9]+ 条' --match regex  # 正则
python -m remote.client ws find-text 发送 --json              # 要完整结构
python -m remote.ctl find-text --query 下一步 -a              # 多机各找各的
```

```json
{"ok": true, "query": "发送", "match": "contains", "unit": "line",
 "case_sensitive": false, "language": "zh-Hans-CN",
 "found": true, "count": 1, "scale": 1.0, "origin": {"x": 0, "y": 0},
 "items": [{"text": "发送", "x": 120, "y": 300, "w": 60, "h": 28,
            "screen": {"x": 120, "y": 300, "w": 60, "h": 28},
            "center": {"x": 150, "y": 314}}]}
```

- `unit` 默认 `line`: 中文会被引擎**逐字**切成很多"词" (见上一节), 词级匹配在这种
  文本上几乎命中不了 —— 要更紧的框 (只框"发送"而不是整行"发送(S)") 再给 `--unit word`。
- `all` 默认关: 只回**最靠上**的那个 (按阅读顺序排过序), 因为"找按钮"要的是那一个。
  要一屏里所有同名的给 `--all`, 再用 `--limit` 截断。
- **没找到不算错**: `found=false` + `items: []`, 不抛异常 —— 屏幕上没有这个字是正常
  结果, 报错留给参数写错 / 平台不支持 (`bad_request` / `unsupported`)。
- `match` / `unit` / `case_sensitive` 都在返回值里**回显**: 参数不回显的话,"某条传输
  把它悄悄丢了"永远测不出来 (服务端按默认处理, 一切看起来正常)。

### 3.2 `python -m remote.ctl` (多机控制级)

先把机器记进 registry (`~/.kuuki/registry.json`, `--registry` 可改), 之后按**别名 / 组 / 全体**
分发同一条命令 —— 并发跑、逐台超时、**结果逐台汇总**, 并把 online/offline 写回 registry:

```bash
# 登记 (本机做受控端时就是 127.0.0.1)
python -m remote.ctl machines add self      --transport ws   --endpoint ws://127.0.0.1:8765 -g local
python -m remote.ctl machines add self-grpc --transport grpc --endpoint 127.0.0.1:50051      -g local
python -m remote.ctl machines list

python -m remote.ctl ping -a                    # 广播
python -m remote.ctl info -g local              # 组播
python -m remote.ctl pos self                   # 单发
python -m remote.ctl shot out.png -a            # 多机自动插别名: out-self.png
python -m remote.ctl watch frames -g local --fps 4 --count 10
python -m remote.ctl move self 400 300 --duration .3
python -m remote.ctl calibrate -g local --max-width 1280   # 每台各标定各的换算
```

`op` / `check` / `shot` / `watch` / `tail` 这几个命令的目标要用 `-t/--to` / `-g` / `-a`
(它们的位置参数是 op 名 / 键名 / 路径, 会被别名列表吞掉)。完整命令表与注释见
[docs/puppet-multi-machine.md](../docs/puppet-multi-machine.md) 第 4 节。

**实操手册**: [docs/ctl-selfhost-runbook.md](../docs/ctl-selfhost-runbook.md) ——
记录 / 注意事项 (含踩过的坑) / 从零复现本机自控的 8 步指南。想上手照抄命令就翻它。

> 2026-09-20 本机自控实测: 起一个 `python -m remote --ws --grpc`, 注册成 WS 与 gRPC
> 两个别名, ping / info / 光标 / 键预检 / 截屏 / 连续抓帧全部通过。

### 3.3 没有真设备时: `python -m remote.dummy`

`remote/dummy.py` 起一批**仿真受控端** —— 真 `RemoteService` + 真传输服务端, 只把抓屏和输入
换成假实现。每台自带屏幕尺寸 / 响应延迟 / 操作日志, 还能单独给某台注入故障, 所以「命令到底去了哪台」
是可验证的 (全都返回同一个答案的话, 广播和单发就分不出来)。用来验证一主多从与多操纵端:

```bash
python -m remote.dummy --count 3 --latency .08 --bootstrap-registry /tmp/swarm.json
python -m remote.ctl ping --all --registry /tmp/swarm.json     # 三台各回各的答案
python -m remote.ctl shot frames -a --registry /tmp/swarm.json # 三张不同尺寸的图
```

选项: `--transport ws|grpc|both` / `--latency` / `--jitter` / `--fail-ops mouse.click` (给最后一台
注入故障) / `--names a,b,c`。详见 [ctl-selfhost-runbook.md](../docs/ctl-selfhost-runbook.md) 第 4.5 节。

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
   python -m remote                        PeerJsClient("kuuki-mouse-<房间码>")
        │                                        │
        └────────► 0.peerjs.com:443 ◄────────────┘
              (只做牵线, 数据走 P2P)
```

**最常见的错误**: 在 A 机器起了服务, 却想让 B 机器"连自己" —— 那没有服务端。
要在哪台机器上动鼠标键盘、截哪台机器的屏, **服务端就必须跑在那台机器上**。

服务端**只能跑在 Windows 上** (见开头的定位说明), 所以"想控制谁"就只剩一个答案:

| 想控制谁 | 服务端跑在哪 | 控制端跑在哪 |
|---|---|---|
| **Windows 桌面** | **必须**是 Windows (`start-win.bat`) | WSL / Linux / 手机 / 任何地方 |

控制端不挑平台 —— 它只发起出站连接, 不碰自己的桌面。

```bash
# 被控端 (Windows): 只开 PeerJS, 打印房间码, 不需要任何端口
start-win.bat --room ABCDE

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
| `Calibrate(CalibrateRequest) → CalibrateReply` | 实测图坐标↔鼠标坐标的换算 (见 3.1.2; reply 形状与 `screen.calibrate` 的 dict 逐字段对齐) |
| `StreamScreenshots(StreamScreenshotsRequest) → stream Image` | **服务端流式推帧** (WS 侧要自己轮询) |
| `Monitors(MonitorsRequest) → MonitorsReply` | 显示器与虚拟桌面边界 (见第 6.x 节; 仅 Windows 受控端) |
| `GetMousePosition` / `MoveMouse` / `MoveMouseRelative` | 光标 |
| `ClickMouse` / `MouseDown` / `MouseUp` / `Scroll` / `Drag` | 鼠标动作 |
| `TypeText` / `PressKey` / `Hotkey` / `Combo` / `HoldKey` / `PasteText` / `CheckKeys` | 键盘 |
| `ListWindows(ListWindowsRequest) → ListWindowsReply` / `GetForegroundWindow` / `FocusWindow` | 窗口 (见第 7 节; 仅 Windows 受控端) |
| `Notify(NotifyRequest) → Ack` | 被控端无焦点角标 |
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
| `screen.calibrate` *`calibrate`/`calib`* | `cols` `rows` `margin` `settle` `tolerance` `max_width` `restore` | **实测**图坐标↔鼠标坐标的换算 (`remote/calibrate.py`), 见 3.1.2。**会动鼠标**, 默认跑完挪回原位 |
| `screen.size` *`size`* | — | 屏幕尺寸 |
| `screen.monitors` *`monitors`/`monitor`/`screens`* | `x` `y` (都给了才是单点查询) | 显示器与虚拟桌面边界 —— 多屏校准的前置信息, 见 3.1.3。仅 Windows 受控端 |
| `screen.screenshot` *`screenshot`/`capture`* | `format` `quality` `region` `max_width` `max_height` `scale` `draw_cursor` `include_image` `binary` `monitor` `all_screens` | 抓一帧。`monitor`=第几块屏 / `all_screens`=整个虚拟桌面 (都不给 = 主屏), 返回值带 `origin` (这一帧左上角在虚拟桌面坐标系里的位置), 见 3.1.4 |
| `screen.ocr` *`ocr`/`read`* | `region` `monitor` `all_screens` `lang` `include_words` | 认出这一屏上的**文字** (系统内置 OCR), 每行/每词都带能直接点的屏幕坐标, 见 3.1.5。**仅 Windows 受控端**, 每次约 1 秒 |
| `screen.find_text` *`find_text`/`find`/`findtext`* | `query` `match` `unit` `case_sensitive` `all` `limit` `region` `monitor` `all_screens` `lang` | 找**写着某段文字的那块**, 回能直接点的中心点坐标 (`center`), 见 3.1.6。仅 Windows 受控端 |
| `screen.grab` | 同上 | 同上, 强制二进制帧 —— **仅 WS** (`remote/ws_server.py` 直接处理) |
| `screen.watch` / `screen.unwatch` | `fps` `count` `watch_id` | 推流 —— **仅 WS**; gRPC 走 `StreamScreenshots` 服务端流式 |
| `mouse.position` *`position`* | — | 当前光标 |
| `mouse.move` *`move`* | `x` `y` `duration` | 绝对定位 (`duration>0` 平滑) |
| `mouse.move_rel` *`move_rel`* | `dx` `dy` `duration` | 相对移动 |
| `mouse.click` *`click`* | `button` `clicks` `interval` `hold` `x` `y` `move_duration` | 点击 (`interval` 默认 50ms, `hold` 默认 60ms)。`x`/`y` 先移动再点 (可只给一个), 返回值带 `positioned_at`; `interval` / `hold` 也回在返回值里 (这样才能跨传输比对 —— 两条传输在这两个字段上曾经不等价) |
| `mouse.down` / `mouse.up` | `button` | 按住 / 松开 |
| `mouse.scroll` *`scroll`* | `dx` `dy` (兼容老协议 `delta`) `steps` `interval` `x` `y` | 滚轮, `dy>0` 向上。`steps>1` 拆成多步平滑滚动 (总增量仍等于 `dx`/`dy`); `x`/`y` 先定位再滚 |
| `mouse.scroll_h` *`scroll_h`* | 同 `mouse.scroll` (`dx` 为主体) | 横向滚动 |
| `mouse.drag` *`drag`/`dragp`* | `x1` `y1` `x2` `y2` **或** `points`/`path`; `button` `duration` | 拖拽。`points=[[x,y],...]` (或 `"x,y;x,y"`) 走路径点, `duration` 是**每段**时长 |
| `keyboard.type` *`type`/`text`* | `text` `interval` | 输入文本 |
| `keyboard.key` *`key`* | `key` `action`(tap/press/release) `modifiers` | 单键 |
| `keyboard.hotkey` *`hotkey`* | `keys` (`"ctrl+shift+s"` 或数组) `hold_ms` | 组合键 |
| `keyboard.combo` *`combo`* | 同 `keyboard.hotkey` | 组合键 + 按住时长 (`hold_ms`) |
| `keyboard.hold` *`hold`* | `key` `ms` | 按住单键 `ms` 毫秒再松开 |
| `keyboard.paste` *`paste`* | `text` | 写剪贴板 + Ctrl/Cmd+V (**中文/emoji 用这个**) |
| `keyboard.check` *`check`/`keys`* | `keys` 或 `key` | **预检**键能不能发 (不按键) |
| `notify` *`popup`* | `message` `detail` `seconds` `corner` | 屏角弹一个**不抢焦点**的角标 (`remote/toast.py`)。`corner` 取 `br/tr/tl/bl` |
| `window.list` *`windows`* | `title` `process` `limit` `include_hidden` | 列出顶层窗口, 按 Z 序 (最靠前在前)。`title`/`process` 是子串过滤 (不分大小写, `process` 也认 pid)。**仅 Windows** (`remote/window.py`) |
| `window.foreground` *`foreground`* | — | 当前前台窗口 (平铺一个窗口 dict; `hwnd=0` 表示没有前台, 如锁屏)。**仅 Windows** |
| `window.focus` *`focus`* | `hwnd` **或** `title`/`process` + `index` `wait` | 把窗口切到前台并**确认**。`focused` 是确认结果不是"调用成功" (Windows 前台锁可能让切换失败); 命中多个时取 Z 序第 `index` 个, 总数在 `matched` 里。**仅 Windows** |
| `kuuki` *`sensor`* | `message` | 老协议透传 |

窗口三个 op 是**视觉定位的补集**: 截图说得出"屏幕上有块像输入框的东西", 说不出
它属于哪个应用 —— 先 `window.list` / `window.focus` 用标题与进程名认窗口、切前台,
再在窗口内部 locate / click (踩过的坑见 `docs/knowledge/gui-window-focus-gap.md`)。

> ⚠️ **三传输并不完全等价**: 上表里 `screen.grab` / `screen.watch` 只在 WS 实现
> (gRPC 的推流走的是 `StreamScreenshots` 服务端流式, 不是 op)。其余 op —— 包括
> 窗口与 `notify` —— 三条传输都有: gRPC 侧对应 `ListWindows` / `GetForegroundWindow` /
> `FocusWindow` / `Notify`。加新 op 时两个翻译层都要跟上
> (见 `docs/knowledge/arch-one-impl-three-transports.md` 与
> `docs/knowledge/todo-open-items.md`)。

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
(写剪贴板 + 发 `Ctrl+V`)。

写剪贴板这条路在 Windows 上**不走命令行工具** —— `_set_clipboard_windows()` 直接调
`SetClipboardData(CF_UNICODETEXT)` 写 UTF-16LE。`clip.exe` 会按控制台代码页 (GBK)
解释 stdin, 中文必然变成 "浣犲ソ" 这类乱码。`info.clipboard_tool` 报的 `clip`
是**兜底**路径的探测结果, 不代表 `paste` 实际用了它; 真正用过的工具名在
`paste` 的返回值里 (`"win32"` 表示走了 Win32 API, 省事又无损)。

兜底时的探测顺序 (非 Windows, 或 Win32 写入失败):
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
- **"只绑回环"挡不住 PeerJS 这条进来的路。** WS / gRPC 是本机*监听*, 绑回环就只有
  本机进程能连; PeerJS 反过来 —— 是本机*主动连出去*注册到公开 broker, 别人拿到房间码
  `kuuki-mouse-XXXXX` 就能从任何地方连进来, 与 `--host` 无关。房间码 31⁵ ≈ 2860 万种,
  够挡误撞, 挡不住有意枚举。没设 token 就等于**任何知道房间码的人有完全控制权**,
  只在可信网络里这么用 (手机自配对通常是可信的), 不确定就加 `--token`。

## 9.1 打包产物 (受控端 exe)

CI 每次推 `master` / `feat/remote-control` 都会在 windows-latest 上打一次, 但**只打
受控端** —— 控制端 (`remote.ctl` / `remote.client`) 一直在开发环境跑源码, 不进包。

- 普通推送: 仓库 → **Actions** → 点最新的 `Build kuuki-agent` → 底部 **Artifacts**
  里下 `kuuki-agent-windows-x64.zip` (保留 14 天)
- 发版本: `git tag v0.1.0 && git push origin v0.1.0` —— 自动建 Release 并把 zip 挂上去
- 自己打: `pip install -r requirements.txt -r requirements-remote.txt -r requirements-build.txt`
  然后 `pyinstaller packaging/kuuki-agent.spec --noconfirm`, 产物在 `dist/kuuki-agent/`

产物是 onedir (不是单文件): aiortc / av 有一堆 DLL, 单文件每次启动都要解上百 MB。
zip 里带一份 `README.txt` 说明怎么起。坑与实测见 `docs/pyinstaller-ci.md`。

## 10. 测试与验证状态

```bash
python -m pytest test_remote.py -v   # 156 passed / 1 skipped
                                     # 其中 29 项专测控制端, 16 项专测 PeerJS, 7 项专测 vision,
                                     # 7 项专测窗口, 12 项专测坐标校准
python -m remote --selftest --selftest-input
```

**2026-09-20 Windows 实测** (`.venv-win`, py3.10.7, pytest 9.1.1, 1680x1050):

- 测试套 **32 passed / 1 skipped** (跳过的是 Linux/X11 专用的键预检用例)。
  P3 控制端落地后是 **57 passed / 1 skipped** (新增 registry、目标解析、分发汇总、
  命令翻译与端到端 15 项)。
- 受控端正常启动 (`python -m remote --ws --ws-port 8766`),
  客户端 `python -m remote.client ws --url ws://127.0.0.1:8766 ping` 与同一命令换 `info`
  端到端都通, `info` 报 `os=Windows` / `clipboard_tool=clip`。
- 平台门禁: 伪装成非 Windows 时 `main()` 返回 2 并在 stderr 给出提示 (`--selftest` 同样被拦);
  Windows 上 `--help` / `--version` 不受影响。
- 真实抓屏: `PIL.ImageGrab` 1680x1050, PNG 魔数正确; region 裁剪 + 缩放正常。
- 光标位置读取正常 (`mouse.position`); `keyboard.check` 在 Windows 上恒为 supported。
- **动作增强**: 多步滚动的总量守恒 (3 格 / 5 步 → 每步 1 格, 不丢余数)、
  路径点拖动整条只按一次松一次、组合键 `hold_ms`、`keyboard.hold` 长按 —— 全部用
  假鼠标/假键盘断言, 不碰真实光标与按键。
- **三传输等价**: 26 个动作用例分别经 WS 与 gRPC 下发, 返回值与副作用逐条比对一致。
  边界上也一致 —— `interval=0` / `duration=0` 这类"显式给 0"与"没给"能区分开
  (proto3 普通标量做不到, 相关字段已改成 `optional`)。窗口 op 另有 7 个用例
  (打桩后端跨传输比对, 不动真桌面), `screen.calibrate` / `screen.monitors` /
  `screen.ocr` / `screen.find_text` 也在这 26 项里 —— 它们的 proto reply 形状刻意
  做得与 service 返回的 dict 一致, 就是为了能逐字段比对 (OCR 那七条的引擎是打桩
  的: 真起一次外部进程要 1 秒, 而这里要比的是两条传输的翻译层)。
  `find_text` 那四条里有一条是**没命中**的 —— 空 repeated 在 protobuf 转 dict 时
  照样会出现, 一边给 `[]` 一边不给键就会红。
- **窗口 op + 端到端**: `window.list` 列出真实桌面窗口 (标题/进程/pid/Z 序),
  `window.focus` 在 Code 与 msedge 之间来回切换且 `focused` / `window.foreground`
  逐一吻合。用"focus 认窗口 → 点进提问框 → paste → 帧差否证 → 回车"的**纯 repo**
  流程向 Gemini 发出一条消息并收到回复 (`evidence/verify_window_fix.py`) ——
  此前同样流程因"不知道前台是谁"点进过错误的窗口。
- **坐标校准**: `screen.calibrate` 在 1680x1050 单屏上按 `--max-width 1280` 跑,
  9/9 靶点全部认对 —— `declared_scale` (帧头声明) 1.3125 与 `fit_scale` (实测)
  1.3114 吻合, 残差 max 0.44px, `verdict=aligned`, `origin≈(0.5, -0.7)` (单屏本就
  没有平移, 多显示器上这一项才是它存在的理由)。三个坑已在模块里堵掉: 前后帧都画
  光标会让帧差出现两个合法候选、动态 UI 里凑巧的红色方块会被当成标记 (本机一度
  9 错 4, 拟合出的系数差 19%)、贴边的标记被裁一半导致重心内偏。
  **端到端回测** (`evidence/calibration/verify_mspaint.py`): 视觉找到画图色板上的
  红色方块 → 标定换算成鼠标坐标 → 移过去后受控端画出的光标与预测位置差 **0.71px**
  → 点击, 色块被选中、"颜色 1" 指示器同步变红 —— 帧差的两处变化都落在预期位置。
  这条链路曾在 `vision.find_color` 上断过: 面积换算错杀小目标, 只好去匹配大块
  笔迹 (见 `docs/knowledge/gui-find-color-area-unit.md`), 已修并有专测钉住。

> `.venv-win` 里现在装了 pytest (9.1.1)。装的时候若 pip 报连不上
> `127.0.0.1:10809`, 那是系统代理变量指到了一个没在跑的代理, 加
> `env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy` 走直连即可。

**2026-09-19 本机 (WSL2/WSLg, conda py3.12, `DISPLAY=:0`) 实测通过** —— 该路径自受控端
限定 Windows 起不再支持, 结论保留在第 8 节:

- `test_remote.py` 当时全过 (键名解析 / 区域 / 编码 / 裁剪缩放 / 光标叠加 /
  服务调度 / 请求信封与 batch / kuuki 透传 / 键预检 / WS 帧编解码 / WS 端到端 /
  gRPC 端到端含流式与鉴权失败 / PeerJS 分块协议)。
- 真实抓屏走 ffmpeg 后端; 真实键盘用 `pynput.keyboard.Listener` (XRecord) 抓 XTEST
  注入事件验证 (`kuuki-remote` 逐字符到达, `enter` 与 `ctrl+shift+k` 到达)。
- 未实测: 多显示器 (本机只有一块虚拟屏)、跨机网络调用。

测试套件**不动鼠标键盘**; 会按键/移光标的验证都在 `--selftest-input` 与冒烟脚本里,
需要显式开启。
