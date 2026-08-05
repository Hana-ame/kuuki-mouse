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

首次运行会在终端打印 ASCII 二维码, 并保存 `pair_<房间码>.png` (同时尝试自动打开)。

> 注意: `main.py` 顶部 `PAGE_URL` 需要改成你的 GitHub Pages 地址, 二维码才指向正确的页面。

### 2. 发布手机页面到 GitHub Pages

把 `web/` 目录内容作为 GitHub Pages 站点发布:

- 方式 A (推荐): 把 `web/` 推到仓库根目录或 `gh-pages` 分支, 在仓库 Settings → Pages 里选择分支。
- 方式 B: 任何静态托管 (Vercel/Netlify/自己的服务器) 都行, 只要 HTTPS + 能访问 unpkg CDN。

### 3. 配对

手机扫 PC 端二维码 → 打开页面 → 自动连接 (或手动输入房间码点「开始配对」)。
连接成功后手机会把传感器数据发到 PC, 手机即变身空气鼠标:

- **校准**: 点「校准姿态」把当前朝向设为零点 (此时鼠标不动)
- **左键**: 点「左键」大按钮
- **文字输入**: 文本框输入回车发送; 空文本框按 Backspace = 键盘退格

## 消息协议 (JSON)

手机 → PC 的 DataChannel / MQTT 消息, 统一走 `app.py::handle_message` 路由:

| `t` | 字段 | 含义 |
|---|---|---|
| `sensor` | `x,y,z` (m/s², 含重力) + `alpha,beta,gamma` (°) + `gx,gy,gz` (rad/s) | 一帧传感器数据 |
| `mouse` | `button` (`left`/`right`/`middle`) | 鼠标按键 |
| `text` | `text` | 键盘输入文本 |
| `key` | `key` | 单键 (如 `Backspace`) |
| `calibrate` | — | 重新校准姿态基准 |

PeerJS DataConnection 必须用 `serialization: "json"` (Python 移植版的二进制/msgpack 未实现)。

## 项目结构

```
main.py          桌面端入口: 房间码 → 二维码 → PeerJS 主机 + MQTT 公告 → 鼠标控制
app.py           姿态→鼠标逻辑 (Mahony 融合 → 前向向量 → 平滑/死区/跳变保护 → 鼠标)
attitude.py      纯 Python 姿态解算 (my-node-app lib/attitude.js + motion/forward.js 的移植)
controller.py    鼠标控制 (pynput 封装)
peerjs/          peerjs-python 的 fork (含 py3.12 兼容补丁, 见下)
web/             手机页面 (GitHub Pages 发布内容, 即旧 www/ 的替代)
test_attitude.py 姿态解算单元测试 (port of verify-attitude.mjs)
```

旧的自托管文件 (`server.py` HTTPS 服务器、`cert.py`/`pull_cert.sh` 证书、`www/` 旧页面)
已删除 — 本版本不需要任何自建服务器。

## 关于 peerjs-python 的 fork

pip 上的 peerjs-python (1.5.1) 是把 JS 版 PeerJS 直接翻译的移植版, 维护停滞,
在 **Python 3.12 + 新版依赖库 (websockets 17 / aiortc / pyee 13)** 下有一串兼容问题。
与其每次重装都跑脚本改 site-packages, 不如直接把源码 fork 进仓库 (`peerjs/`),
补丁已固化在源码里, 换机器/重装环境都不会丢。共修了 7 处:

1. `PeerOptions.config` 可变默认值 — py3.12 的 dataclass 直接报错 → `default_factory`
2. peerjs 用顶层 `from pyee import AsyncIOEventEmitter`, 但 pyee≥12 移除了顶层导出, 且
   aiortc 要求 pyee>=13 → fork 改成 `from pyee.asyncio import AsyncIOEventEmitter`,
   同时兼容 aiortc 的 pyee>=13 (不再需要固定 pyee 11, 也不再和 aiortc 冲突)
3. `peer.connect()` 把 options 当位置参数传给 `DataConnection` (库自身 bug) → 改关键字参数
4. `negotiator` 把 options dict 当对象用 (`options.constraints`) + aiortc `createOffer()` 无参 → `.get()` + 去参
5. `socket._wsOpen()` 用了 websockets≥13 已删除的 `.open` → 改用 `state == State.OPEN`
6. offer payload 直接塞 `RTCSessionDescription` 对象 (不能 JSON 序列化) → `object_to_dict()`
7. `dataconnection.handleMessage` 对 dict 用 `payload.sdp` 属性访问 → `payload.get('sdp')`

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
- **后续同步上游**: 上游若有新版本, 把新源码拷进 `peerjs/` 后重新确认这 7 处补丁仍在即可
  (可 `grep` 上面的关键词核对)。

fork 版本同 JS 版 PeerJS 服务器协议互通 (手机浏览器用官方 JS 版), 无兼容问题。

## 环境小坑 (本机)

shell 里有裸的 `socks_proxy=172.29.80.1:10808` (无 scheme) 环境变量, 会被
websockets/urllib 误当 HTTP 代理, 而 0.peerjs.com / broker.hivemq.com 都能直连,
所以 `main.py` 启动时会自动清掉这类无 `://` 的代理变量。若你的环境不同, 自行删除该段即可。
