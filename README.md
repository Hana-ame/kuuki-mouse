# kuuki-mouse

手机传感器 → 空气鼠标 (完全重构版, 不再自己 host 任何东西)。

继承原版思路 (手机传感器 → 姿态解算 → 控制鼠标, 算法移植自 `~/my-node-app`),
但把整条链路改成**全公开服务**架构:

```
┌─────────────┐   PeerJS WebRTC 数据通道 (主)   ┌──────────────────┐
│  手机 (web/) │◄──────────────────────────────►│  PC (main.py)    │
│ GitHub Pages │   MQTT 公告 + 兜底通道 (备)      │ 姿态解算→鼠标     │
└─────────────┘                                 └──────────────────┘
      │                                                │
      └── 扫码 → 打开 GitHub Pages 页面 #/房间码 ────────┘
```

- **PC 端只做两件事**: ① 姿态解算 → 控制鼠标; ② 打印二维码给手机扫。
- **配对/传输全部走公开服务**, 不需要域名、证书、端口转发、自己 host HTML:
  - **PeerJS 公开 cloud broker** (`0.peerjs.com:443`) — 主数据通道 (WebRTC DataChannel)
  - **MQTT 公开 broker** (`broker.hivemq.com`, 与 my-node-app 同款) — 房间公告 + 兜底数据通道
  - **GitHub Pages** — 手机页面托管
- 二维码内容 = `https://<你的用户名>.github.io/<仓库名>/#/<房间码>`, 手机扫码即配对。

## 扩展: 本机远程控制 (remote/) — 鼠标键盘 + 截屏, WS / gRPC / PeerJS 三传输 (受控端仅 Windows)

除空气鼠标之外, 本仓库还提供一个**本机电脑操作服务**: 用鼠标键盘操作这台机器、
把屏幕截图取回来, 供 agent / 脚本 / 别的机器调用。**三种传输默认全开**:

> **受控端只支持 Windows**。"受控端"指被操作的那台机器, 也就是下面这个服务本身 ——
> 它必须**原生跑在 Windows 上**, 非 Windows 会在启动前被拒绝 (退出码 2, 提示里给可照做
> 的命令)。WSL / Linux / 手机一侧只跑**客户端** (`python -m remote.client …`) 连过来就行,
> 客户端不挑平台。

| 传输 | 默认端点 | 适用场景 |
|---|---|---|
| WebSocket | `ws://127.0.0.1:8765` | 最常用, JSON + 二进制截屏帧 + 推流 |
| gRPC | `127.0.0.1:50051` | 强类型接口, 服务端流式推帧 |
| PeerJS | 无本地端口 (走 `0.peerjs.com` 公开 broker) | 跨网络免端口转发/免域名, 靠房间码配对 |

```bash
pip install -r requirements.txt -r requirements-remote.txt
python -m remote                     # 默认三个一起起: WS 8765 + gRPC 50051 + PeerJS 房间码
python -m remote --no-peerjs         # 只要本机两个端口, 不连公开 broker
python -m remote --no-ws --no-grpc   # 只开 PeerJS (不需要任何开放端口)
python -m remote --room ABCD123      # 指定 PeerJS 房间码 (默认随机生成)
python -m remote --selftest          # 自检: 报告截屏后端 + 抓一帧 (不动鼠标)
python -m remote.client ws ping      # 命令行客户端
```

- 操作: 鼠标绝对/相对移动、点击/按住/滚轮/拖拽, 键盘输入/单键/组合键/剪贴板粘贴, 截屏 (区域/缩放/多格式/推流)。
- 兼容原空气鼠标协议: 老协议 JSON (`t`/`mouse`/`text`/`key`) 会被直接路由到
  `app.handle_message`, 所以 `web/` 页面可以不走 PeerJS, 直接把传感器数据发到本机 WS。
- 默认只绑 `127.0.0.1`; 暴露到网络必须 `--allow-remote --token <随机值>`。
  PeerJS 走的是出站连接、不需要开放入站端口, 但同样建议带 token。

完整协议、op 一览、PeerJS 分块传输、跨机部署位置与已知限制见
[remote/README.md](remote/README.md) (里面另有一批 WSLg / X11 的历史实测结论 ——
那条路径自受控端限定 Windows 起已不再作为支持目标, 保留仅作参考)。

## 快速开始

### 1. PC 端

```bash
pip install -r requirements.txt
python main.py            # 生成房间码 + 二维码, 开始运行
# 可选: python main.py ABCDE 指定房间码
```

> `requirements.txt` 里**没有** `peerjs` — peerjs-python 已被 fork 到项目 `peerjs/` 目录
> (含 Python 3.12 全部兼容补丁), 直接 `import peerjs` 就会走本地这份, 无需安装/修补。
> 它的依赖 (`aiortc` + `pyee` + `av`) 已在 requirements 里, 且已避开版本冲突,
> 直接 `pip install -r requirements.txt` 即可, 不用 `--no-deps`。

首次运行会在终端打印 ASCII 二维码, 并把二维码 PNG 保存到 `pair_<房间码>.png`
(终端会打印完整路径, 自行打开该文件用手机扫码)。

> 注意: `main.py` 顶部 `PAGE_URL` 需要改成你的 GitHub Pages 地址, 二维码才指向正确的页面。

### 2. 发布手机页面到 GitHub Pages

本仓库已配置 **GitHub Actions 自动部署** (`.github/workflows/pages.yml`):
每次 push 到 `master` 都会把 `web/` 目录自动部署到
`https://<用户名>.github.io/<仓库名>/`。只需在仓库
Settings → Pages → Build and deployment → Source 选 **GitHub Actions** 一次。

手动/其他托管 (Vercel/Netlify/任意静态站) 也行, 只要 HTTPS + 能访问 unpkg CDN。

### 3. 配对

手机扫 PC 端二维码 → 打开页面 → 自动连接 (或手动输入房间码点「开始配对」)。
连接成功后手机会把传感器数据发到 PC, 手机即变身空气鼠标。

**用法**:

1. **校准**: 手机对准屏幕中央方向, 点「校准姿态」——当前指向设为基准, 光标回屏幕中心。
2. **移动**: 手机转动, 光标跟随移动 (绝对映射, δ角度=δ位置)。
3. **静止**: 手机不动 → 光标**冻结在原地**, 绝不漂移。点「🖱 鼠标」回鼠标模式。
4. **鼠标键**: 「左键 / 中键 / 右键」+「滚轮 ↑ / 滚轮 ↓」。
5. **输入**: 点「✍ 输入」或点文本框 → 进入**输入模式** (此时**禁用鼠标移动**):
   - 文本框打字 (可用**手机拼音输入法**), **每个字符立即发送**到电脑直接输入;
   - 空文本框退格 = 电脑退格;
   - 下方**屏幕全键盘**点键即发送 (Q/W/E…数字、空格、回车、退格、Shift);
   - 点任一鼠标键 / 校准 / 点「🖱 鼠标」退出输入模式。

> 手势不稳/换手后, 重新点一次「校准姿态」即可。

## 算法: 长轴指向驱动 (抗漂移)

鼠标定位采用你指定的坐标系定义 (`app.py`):

```
Y (上下) = 手机长轴指向方向 与 地面 的夹角          (俯仰)
X (左右) = 指向方向 相对 校准基准铅垂面 的左右偏角   (偏航)

驱动: 光标 = 屏幕中心 + (当前角度 − 基准角度) × px/度,  绝对映射
      δ(夹角) = δ(绝对位置),  位置是角度的直接函数, 无速度/无惯性/无低通
```

- **信号源**: 两个角都从 **Mahony 陀螺积分四元数** 解出 (姿态解算保留, `attitude.py`),
  长轴方向 = 四元数旋转设备 `+y`(竖屏长边) 在世界系的方向。
  **不用** `deviceorientation` 的绝对 alpha (磁罗盘) 直接驱动 ——
  罗盘在室内受磁场干扰会自己漂移/跳变, 手机静止 alpha 也在变,
  "静止还在飘"就是它造成的。陀螺短时间积分平滑、无罗盘跳变。
- **无速度感**: 位置是角度的直接函数, 没有任何低通/惯性/累积,
  手机转 δ 角度, 光标就立刻移对应的 δ 绝对像素, 停即停。
- **静止冻结 (抗漂移核心)**: 用陀螺转速 `|gx,gy,gz|` (静止时≈0, 不受磁场影响)
  判断手机是否真在动。持续静止 100ms → 光标冻结 + 角度基准重锚定。
  设备没提供 rotationRate 时自动禁用冻结 (Mahony 无陀螺也不会漂移)。
- **其他**: 单帧跳变保护 (毛刺 → 重锚定, 光标不跳)、微小死区 (
  吸收微噪)。

### 调参 (`app.py` 顶部常量)

| 常量 | 默认 | 作用 |
|---|---|---|
| `FULL_RANGE_X_DEG` | 90 | 左右 ±45° 对应整个屏宽, 越小越灵敏 |
| `FULL_RANGE_Y_DEG` | 60 | 上下 ±30° 对应整个屏高, 越小越灵敏 |
| `DEADZONE_DEG` | 0.05 | 角度差死区(°), 吸收传感器微噪 (≈1°/s 慢移仍可用) |
| `JUMP_GUARD_DEG` | 90 | 单帧角度跳变阈值, 超此值重锚定 |
| `STILL_GYRO_RAD_S` | 0.05 | 静止判定: 陀螺转速低于此 (≈2.9°/s) |
| `STILL_TIME_S` | 0.10 | 持续静止多久才冻结 (秒) |
| `GYRO_LIVE_THRESH` | 0.02 | 判定陀螺是否可用 |
| `kp` / `ki` | 0.5 / 0.1 | Mahony 比例/积分增益 |

> 方向反了 (如上抬光标反而向下): 把 `update_data` 里 `ty = self._sy - d_el * self._gain_y`
> 的 `-` 改成 `+` (或 `tx` 的 `+` 改 `-`) 即可。

## 消息协议 (JSON)

手机 → PC 的 DataChannel / MQTT 消息, 统一走 `app.py::handle_message` 路由:

| `t` | 字段 | 含义 |
|---|---|---|
| `sensor` | `x,y,z` (m/s², 含重力) + `alpha,beta,gamma` (°) + `gx,gy,gz` (rad/s) | 一帧传感器数据 |
| `mouse` | `button` (`left`/`right`/`middle`) | 鼠标按键 (左/中/右) |
| `scroll` | `delta` (>0 上, <0 下) | 滚轮上下滚 |
| `text` | `text` | 键盘输入文本 (逐字符自动发送) |
| `key` | `key` | 单键 (`Backspace`/`Enter`/`Space`/`Tab`…) |
| `calibrate` | — | 重新校准姿态基准 |

PeerJS DataConnection 必须用 `serialization: "json"` (Python 移植版的二进制/msgpack 未实现)。

## 项目结构

```
main.py          桌面端入口: 房间码 → 二维码 → PeerJS 主机 + MQTT 公告 → 鼠标控制
app.py           长轴指向驱动 (Mahony 陀螺积分 → 长轴俯仰/偏航 → 静止冻结/死区/跳变保护 → 鼠标)
attitude.py      纯 Python 姿态解算 (my-node-app lib/attitude.js + motion/forward.js 的移植)
controller.py    鼠标控制 (pynput 封装)
peerjs/          peerjs-python 的 fork (含 py3.12 兼容补丁, 见下)
web/             手机页面 (GitHub Pages 发布内容, 即旧 www/ 的替代)
test_attitude.py 姿态解算单元测试 (port of verify-attitude.mjs)
remote/          本机远程控制扩展 (**受控端仅 Windows**, 鼠标键盘 + 截屏, WS / gRPC / PeerJS 三传输)
  ├─ __main__.py   服务端入口 + 平台门禁 (非 Windows 拒绝启动, 退出码 2)
  ├─ screen.py     截屏 (单一后端 PIL.ImageGrab) + 裁剪/缩放/编码
  ├─ input.py      输入控制 (扩展 controller.PynputMouseController)
  ├─ service.py    操作注册表 (op 调度, 三个传输共用)
  ├─ ws_server.py  WebSocket 服务端 (JSON + 二进制截屏帧 + 推流 + 鉴权)
  ├─ grpc_server.py gRPC 服务端 (含服务端流式截屏)
  ├─ peerjs_server.py / peerjs_client.py  PeerJS 传输 (被控端 / 控制端)
  ├─ ice.py        ICE 候选地址过滤 (自动排掉虚拟网卡)
  ├─ toast.py      被控端无焦点角标通知 (notify op)
  ├─ win/          WSL→Windows 实验桥 (host.py + winhost.ps1, 未接入服务端)
  ├─ client.py     命令行客户端 (单机调试级: 一条命令打一台一个 op)
  ├─ ctl.py        多机控制端 (别名/组/广播 + 结果汇总 + online-offline 回填)
  ├─ proto/        kuuki_remote.proto 与生成的 gRPC 存根
  └─ README.md     扩展的完整文档 (协议 / op 表 / 本机实测与坑)
test_remote.py   remote/ 扩展的测试 (57 项, 不动鼠标键盘; 含平台门禁与跨传输等价用例)
wincheck.py      Windows 侧自检 (三种传输, 只读状态 + 截屏)
start-win.bat    Windows 侧启动脚本 (venv.ps1 建虚拟环境)
docs/puppet-multi-machine.md  多机 Puppet 方案 (一个控制端管 N 台被控机, P1-P3/P6 已落地)

# ---- 调试 / 演示小工具 ----
annotate.py      截图上叠坐标网格 + 标记目标点/色块, 核对"算出的坐标偏没偏"
click.html       点击靶页面 (纯品红大按钮, 点击 POST /click)
play_target.py   点击靶 HTTP 服务 (仅靶, 不走 PeerJS, 供 WS 通道演示)
p2p_clicktarget.py  点击靶 + PeerJS 控制点击, 结果写 _clicks.jsonl 由控制端判定
p2p_toast.py     经 PeerJS 控制 Windows 发消息, 每步在被控端弹无焦点角标
requirements.txt / requirements-remote.txt  依赖 (后者为 remote/ 扩展所需)
.github/workflows/pages.yml  push 到 master 自动部署 web/ → GitHub Pages
```

旧的自托管文件 (`server.py` HTTPS 服务器、`cert.py`/`pull_cert.sh` 证书、`www/` 旧页面)
已删除 — 本版本不需要任何自建服务器。

## 关于 peerjs-python 的 fork

pip 上的 peerjs-python (1.5.1) 是把 JS 版 PeerJS 直接翻译的移植版, 维护停滞,
在 **Python 3.12 + 新版依赖库 (websockets 17 / aiortc / pyee 13)** 下有一串兼容问题。
与其每次重装都跑脚本改 site-packages, 不如直接把源码 fork 进仓库 (`peerjs/`),
补丁已固化在源码里, 换机器/重装环境都不会丢。共修了 8 处:

1. `PeerOptions.config` 可变默认值 — py3.12 的 dataclass 直接报错 → `default_factory`
2. peerjs 用顶层 `from pyee import AsyncIOEventEmitter`, 但 pyee≥12 移除了顶层导出, 且
   aiortc 要求 pyee>=13 → fork 改成 `from pyee.asyncio import AsyncIOEventEmitter`,
   同时兼容 aiortc 的 pyee>=13 (不再需要固定 pyee 11, 也不再和 aiortc 冲突)
3. `peer.connect()` 把 options 当位置参数传给 `DataConnection` (库自身 bug) → 改关键字参数
4. `negotiator` 把 options dict 当对象用 (`options.constraints`) + aiortc `createOffer()` 无参 → `.get()` + 去参
5. `socket._wsOpen()` 用了 websockets≥13 已删除的 `.open` → 改用 `state == State.OPEN`
6. offer payload 直接塞 `RTCSessionDescription` 对象 (不能 JSON 序列化) → `object_to_dict()`
7. `dataconnection.handleMessage` 对 dict 用 `payload.sdp` 属性访问 → `payload.get('sdp')`
8. `Peer.connect(peer, options)` 的 `options` 默认值是 dict `{}`, 实现里却对它调
   `dataclasses.asdict(options)` —— 照抄 JS 文档的 `{serialization: "json"}` 会直接
   `TypeError: asdict() should be called on dataclass instances` → 默认改 `None` 并兼容 dict 入参

### fork 来源与版本差别

- **上游**: [peerjs-python](https://github.com/ambianic/peerjs-python) `1.5.1` (PyPI 包 `peerjs`),
  即 JS 版 [PeerJS](https://peerjs.com) 的 Python 移植。
  来源经 PyPI 元数据核实: `home_page = github.com/ambianic/peerjs-python`, 作者 Ivelin Ivanov。
  注意: 网上流传的 `Phantom-Flash/peerjs-python` 写法是错的, GitHub 上不存在; 请以 PyPI 元数据为准。
- **本仓库**: [Hana-ame/kuuki-mouse](https://github.com/Hana-ame/kuuki-mouse) (origin) —
  `peerjs/` 目录即维护在项目内的 fork 副本, 随项目走。
- **项目本身继承自**: [meromeromeiro/kuuki-mouse](https://github.com/meromeromeiro/kuuki-mouse) (upstream)。
- **获取方式**: 从 `pip install peerjs` 后的 `site-packages/peerjs/` 整体拷贝进仓库 (无 `__init__.py`, 是 namespace 包),
  共 11 个源文件 + `ext/http_proxy.py`, 未做删减。
- **本地版本**: 在上游 1.5.1 基础上打了上面 7 处补丁 (全部与 Python 3.12 / 新版依赖库兼容性相关,
  不改动任何 PeerJS 服务器协议逻辑)。
- **与 pip 版 peerjs 的区别**: pip 版无法在 py3.12 + 新依赖下直接运行 (需 `--no-deps` + 手工改源码);
  本 fork 开箱即用, 且不需要 `pip install peerjs`。
- **后续同步上游**: 上游若有新版本, 把新源码拷进 `peerjs/` 后重新确认这 8 处补丁仍在即可
  (可 `grep` 上面的关键词核对)。

fork 版本同 JS 版 PeerJS 服务器协议互通 (手机浏览器用官方 JS 版), 无兼容问题。

## 环境小坑 (本机)

shell 里有裸的 `socks_proxy=172.29.80.1:10808` (无 scheme) 环境变量, 会被
websockets/urllib 误当 HTTP 代理, 而 0.peerjs.com / broker.hivemq.com 都能直连,
所以 `main.py` 启动时会自动清掉这类无 `://` 的代理变量。若你的环境不同, 自行删除该段即可。
