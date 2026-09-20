# kuuki-mouse 多机 Puppet 方案

> 2026-09-19 组稿。目标：**一个控制端管 N 台被控机**，动作集覆盖**完整拖动 / 平滑滚动 / 键盘组合键**，局域网、公网、隧道三种连接方式都能用，且每步动作都可自验证。

## 0. 目标

- **多机**：controller（控制端）⇄ N × agent（被控端），支持单发 / 广播 / 组播。
- **动作完整**：
  - 鼠标：绝对/平滑移动、单击/双击/右键/中键、按住/松开、**路径拖动**（多点轨迹）、**平滑滚动**（多步/横竖方向/先定位再滚）。
  - 键盘：文本输入（含中文/emoji，走剪贴板粘贴）、单键 tap/press/release、**组合键**（ctrl+shift+s 之类，含可按住时长）、键支持性预检。
  - 屏幕：单帧截屏（png/jpeg/webp、region、光标）、连续帧 watch。
- **传输等价**：同一套 op 在 WS / gRPC / PeerJS 下行为一致（三个传输都只是 `RemoteService.handle(op, args)` 的翻译层）。
- **自验证**：动作结果由被控端测试靶回传事件文件判定，不靠肉眼。
- **平台**：**agent（被控端）只支持 Windows** —— 它必须原生跑在 Windows 上，非 Windows 会在
  启动前直接拒绝（退出码 2，见 `remote/README.md` 开头与 `remote/__main__.py` 的
  `platform_refusal()`）。controller 不限平台，WSL / Linux / 手机都能当控制端。

## 1. 架构

```
controller (本机)
  kuuki_ctl CLI ── registry.json ──┬─> agent A: 传输 ws/grpc/peerjs ──> Windows (agent 只能跑在 Windows)
                                    └─> agent B: ...                        (各跑 remote 服务)
```

- **agent** = 现有 `remote/` 包（service 层 + 三传输）。每台一个唯一房间码/别名。
- **controller** = 新 `kuuki_ctl` CLI：机器注册表 + 动作分发 + 批处理 + 截图循环。
- 寻址：`registry.json` 把 **别名** 映射到 `{transport, endpoint/peer, token, os}`；PeerJS 下 agent 固定注册为 `kuuki-mouse-<房间码>`，房间码即地址。其中 `os` 恒为 `win`（agent 只支持 Windows），留着是为了将来真出现多平台时不用改数据结构。

## 2. 传输矩阵（按场景选）

| 场景 | 传输 | agent 启动（被控机） | controller 连接 | 前提 |
|---|---|---|---|---|
| 局域网 | WS | `python -m remote --ws --allow-remote --token T` | `ws://<ip>:8765/?token=T` | agent 机防火墙放行 8765 入站 |
| 局域网 | gRPC | 同上（默认 50051） | `<ip>:50051` + token | 同上 50051 |
| 公网/异地（非对称 NAT） | PeerJS | `python -m remote --room XXXX --token T`（**无端口**；PeerJS 是默认传输） | `--peer kuuki-mouse-XXXX` | 双端能连 0.peerjs.com 公网 broker |
| 对称 NAT / 求稳 | 隧道 | tailscale/zerotier 组网后，WS/gRPC 走隧道虚拟 IP | `ws://<tunnel-ip>:8765` | 两端装隧道软件，无防火墙烦恼 |

- PeerJS 是异地最省事的路（免端口/域名/证书），但**双端都在对称 NAT 后**需要 TURN 兜底（见风险节）。
- 隧道是**最稳**的路：把「公网可达」这件事外包给 tailscale，传输层仍用现成 WS/gRPC。

## 3. 动作集（op 表，三传输共用）

### 已实现（P1/P2 于 2026-09-20 落地）

| op | 说明 | 关键参数 |
|---|---|---|
| `mouse.position` | 读光标 | — |
| `mouse.move` | 绝对移动（可平滑） | x, y, duration |
| `mouse.move_rel` | 相对移动 | dx, dy, duration |
| `mouse.click` | 单击/双击/右键，hold 默认 60ms；x/y 先移动再点 | button, clicks, interval, hold, x?, y? |
| `mouse.down` / `mouse.up` | 按住/松开 | button |
| `mouse.scroll` | 滚轮；**steps>1 平滑多步**，**x/y 先定位再滚** | dx, dy, steps=1, interval=0.05, x?, y? |
| `mouse.scroll_h` | 横向滚动（`dx` 为主体） | dx, dy=0, steps, interval |
| `mouse.drag` | 拖拽；**points 走路径点**：首点按下→逐段平滑→末点松开 | x1,y1,x2,y2 **或** points=[[x,y],...]（也认 `"x,y;x,y"`）, button, duration（每段） |
| `keyboard.type` | 文本输入（逐字符 interval；中文/emoji 必须改用 `keyboard.paste`） | text, interval |
| `keyboard.key` | 单键 tap/press/release + 修饰键 | key, action, modifiers |
| `keyboard.hotkey` | 组合键：依序按下→逆序松开 | keys=["ctrl","shift","s"] 或 "ctrl+shift+s", hold_ms=0 |
| `keyboard.combo` | 组合键 + **按住时长**（与 hotkey 同一实现） | keys, hold_ms |
| `keyboard.hold` | **按住单键 N 毫秒**再松开（F2 重命名这类长按） | key, ms |
| `keyboard.paste` | 剪贴板 + Ctrl+V（中文/emoji 可靠输入） | text |
| `keyboard.check` | 键支持性预检（不实际按键） | keys |
| `window.list` | 列出顶层窗口（标题/进程名过滤，按 Z 序）。**仅 Windows 受控端** | title, process, limit, include_hidden |
| `window.foreground` | 当前前台是谁（跑点击流程前先问一句） | — |
| `window.focus` | 把窗口切到前台**并确认**（focused 是确认结果，不是"调用成功"） | hwnd 或 title/process + index, wait |
| `screen.calibrate` | **实测**图坐标↔鼠标坐标的换算（会动鼠标，跑完默认挪回原位） | cols, rows, margin, settle, tolerance, max_width, restore |

**向后兼容**：`scroll` 不传 steps 即单步（原行为）；`drag` 不传 points 仍认 x1/y1/x2/y2（两点 = points 两元素）；老协议 `{"t":"scroll","delta":N}` 仍被识别。

**多步滚动的总量守恒**：拆分时用"累计目标值取整后取差值"，不是每步 `dx // steps` ——
后者会把整除余数丢掉（3 格 / 5 步会每步 0 格，最后一格都不滚）。

**三传输一致性已验证**：15 个新动作用例逐条比对 WS 与 gRPC 的返回值与副作用
（`test_remote.py::test_new_ops_agree_across_transports`）。过程中修掉两处真实偏差 ——
gRPC 把 `keys: "ctrl+shift+s"` 当成一个键名没拆分、`duration: 0` 被 `or 0.3` 吞掉变成插值轨迹。
另外 proto3 的普通标量分不清"显式给 0"与"缺省"，所以 `ScrollRequest.interval`、
`DragRequest.duration`、`ScrollRequest.at_x/at_y` 都改成了 `optional`。

### 仍未实现

| op | 说明 | 状态 |
|---|---|---|
| ~~`screen.monitors`~~ | 屏幕枚举（多显示器坐标对齐要用） | **2026-09-21 已实现**（`remote/monitor.py`，ctypes 只读枚举，见 remote/README 3.1.3） |
| ~~`screen.ocr`~~ | 认屏幕上的字（多机文案可能不同） | **2026-09-21 已实现**（`remote/ocr.py`，系统内置 OCR，仅 Windows 受控端，见 remote/README 3.1.5） |
| ~~`screen.find_text`~~ | 找写着某段文字的那块并给可点坐标 | **2026-09-21 已实现**（多机编排里比硬编码坐标稳，见 remote/README 3.1.6） |

## 4. 控制器 `remote.ctl`（P3，2026-09-20 落地）

```
# 先登记机器 (~/.kuuki/registry.json, 可用 --registry 改路径)
python -m remote.ctl machines add pc1 --transport ws    --endpoint ws://192.168.1.20:8765 --token T
python -m remote.ctl machines add pc2 --transport grpc  --endpoint 192.168.1.21:50051 --token T -g office
python -m remote.ctl machines add pc3 --transport peerjs --endpoint kuuki-mouse-XXXX -g office
python -m remote.ctl machines list            # 别名 / 传输 / 地址 / 组 / online-offline
python -m remote.ctl machines show pc1        # (token 默认打码, --show-token 才显示)
python -m remote.ctl machines rm pc3

# 单发 / 广播 / 组播: 多数命令目标写在命令后, 也可 -t <别名> / -g <组> / -a
#                    (例外见下方注意: op / check / shot / watch / tail 的目标只能走 -t/-g/-a)
python -m remote.ctl ping pc1 pc2 pc3         # 单发多台
python -m remote.ctl ping all                 # 广播
python -m remote.ctl info -g office           # 组播
python -m remote.ctl pos -a                   # 读光标

# 动作 (同一套 op, 三传输共用)
python -m remote.ctl move  pc1 400 300 --duration .3
python -m remote.ctl move-rel pc1 --dx 5 --dy -5
python -m remote.ctl click pc1 --button right --clicks 2
python -m remote.ctl down pc1 | up pc1 --button left
python -m remote.ctl scroll pc1 --dy 3 --steps 5 --x 800 --y 400
python -m remote.ctl scroll-h pc1 --dx 4 --steps 2
python -m remote.ctl drag  pc1 100 100 500 400 --duration .3
python -m remote.ctl dragp pc1 "100,100;300,200;500,150" --duration .2
python -m remote.ctl type  pc1 "hello" --interval 0.01
python -m remote.ctl paste pc1 "中文走剪贴板"
python -m remote.ctl key   pc1 f5 | key pc1 ctrl --action press
python -m remote.ctl combo pc1 ctrl+shift+s --hold 200
python -m remote.ctl hold  pc1 f2 500
python -m remote.ctl check -a ctrl shift f5    # 键支持性预检, 不真按

# 任意 op / 回显
python -m remote.ctl op screen.size -a
python -m remote.ctl shot  out.png -a                     # 多机自动插别名: out-pc1.png
python -m remote.ctl watch frames -g office --fps 4 --count 10
python -m remote.ctl tail  frames -t pc1                  # 一直抓到 Ctrl-C
```

**注意 `op` 与 `check` 的目标只能用 `-t/-g/-a`**：它们的 op 名 / 键名本身是位置参数，再放一组位置参数当别名会被 argparse 吞掉（`shot` / `watch` / `tail` 同理，`路径` 在命令后、目标在 `-t/-g/-a`）。

- 实现：`remote/ctl.py`，复用 `remote/client.py` 的 `WsClient / GrpcClient / PeerJsClient`，上面加一层多机分发：registry 读写、目标解析、**并发**分发（`--serial` 可串行）、按机器超时、**结果逐台汇总**、状态回填。
- `GrpcClient` 是同步的（gRPC 库同步），并发时用 `asyncio.to_thread` 包住，不会堵住另两台的 asyncio 连接。
- **超时与失败是逐台算的**：默认 10s（`machines add --timeout` 定义这台机器的默认值，单次可用全局 `--timeout` 覆盖）；超时或抛异常只标记**那一台**失败，不拖垮整批。结束时把 online/offline 与 `last_seen` / `last_error` 写回 registry（`machines list` 能看到，`--no-update` 可关）。
- 截图回显：`shot` 存文件；`watch` / `tail` 按 `<目录>/<别名>/0001.png` 连续落盘（多机不会互相覆盖）。
- registry 里的 token 是**明文**：新建时会收权限（POSIX 0600，Windows 用 icacls 断继承），不想落盘就让它读环境变量 `KUUKI_REMOTE_TOKEN`。
- 退出码：全部成功 0；有机器失败 1；用法错误（未知别名 / 未知组 / `--args` 不是 JSON）2。
- **多个操纵端并存**时 registry 的写走 `Registry.transaction()`（文件锁 + 重新读盘 + 原子写）。
  朴素的 load→改→save 会让后写的把先写的整份冲掉（实测丢一半）；另外 Windows 上还要额外处理
  「文件锁不跨线程」「`os.replace` 目标被占用会 WinError 5」「tmp 名撞 pid」三个坑，见
  [ctl-selfhost-runbook.md](ctl-selfhost-runbook.md) 第 4.5 节。
- **没有真设备也能验多机**：`python -m remote.dummy --count 3` 起一批仿真受控端（各自的屏幕/延迟/
  操作日志，可单独注入故障），registry 直接由它生成。145 项测试里有 9 项在跑这套。
- **PeerJS 不必"等有外网再说"**：16 项回环测试跑的是与真机相同的代码路径，另有
  `python -m remote.peerjs_selftest` 连公开 broker 做真机自检（注册 2s / 握手 13s /
  192KB 截图分块传送逐字节相同）。逐环节分析与修掉的 9 个问题见
  [peerjs-analysis.md](peerjs-analysis.md)；目前只剩跨 NAT 没验过。

> **想照着跑一遍？** 见 [docs/ctl-selfhost-runbook.md](ctl-selfhost-runbook.md)：
> 实测记录、**14 条注意事项**（目标参数冲突 / registry 回填 / 端口占用 / 跨传输返回形状差异 / token 明文 / 只在 Windows 受控 / 本机 venv 与 git push 的坑）、以及**从零复现本机自控的 8 步**（起受控端 → 注册 self 与 self-grpc → 只读四连 → 截屏抓帧 → 零位移输入 → 故障演练 → 测试 → 收尾），每步附实测输出。

## 5. 目标端部署包

- `pack/agent/`：`remote/` 包 + `requirements.txt` + `config.json`（token/传输/房间码）+ 启动脚本：
  - Windows：`start-agent.bat`（venv 检测、代理变量清理，同 start-win.bat 做法）
  - ~~Linux/macOS：`start-agent.sh` + systemd/launchd 单元~~ —— 已删：agent 只支持 Windows
- **PyInstaller 单文件** `kuuki-agent.exe`（可选）：目标机免装 Python，双击即跑。
- 启动打印：房间码 / peer id / 端口 / 鉴权要求；日志写文件。
- 安全：默认要求 token；`--allow-remote` 必须同时带 token；PeerJS 房间码 + token 双因子。

## 6. 验收（自验证，不靠肉眼）

扩展测试靶 `target_events.html` + 本地服务收 `POST /event` 写 `_events.jsonl`：

| 事件 | 页面记录 | 控制器断言 |
|---|---|---|
| 拖动 | mousedown/mousemove/mouseup 轨迹点序列 | 起点/终点在容差内，路径点数 ≥ 预期 |
| 滚动 | wheel 的 deltaY/deltaX 累计 | 总步数、方向、位置符合 |
| 组合键 | keydown 带 ctrlKey/shiftKey/altKey 标志 | 标志与键序正确 |

- `verify_events.py <alias> --case drag|scroll|combo` 输出 PASS/FAIL。
- **多机**：两台以上 agent 跑同一用例套，结果对照。
- **三传输等价**：同一动作集分别经 WS / gRPC / PeerJS 各验一遍（PeerJS 走公网 broker，测真实跨网）。

## 7. 实施顺序（可增量落地）

1. ✅ **P1 `remote/input.py`**（2026-09-20 完成）：scroll 平滑多步（总量守恒）、drag 路径点、combo 的 `hold_ms`、`keyboard.hold` 长按 —— 带单测，测试用假鼠标键盘，不碰真实光标。
2. ✅ **P2 `remote/service.py` + proto**（2026-09-20 完成）：新增 `mouse.scroll_h` / `keyboard.combo` / `keyboard.hold` 与别名；proto 补 `Combo` / `HoldKey` / `ScrollHorizontal` 三个 RPC 与 `optional` 字段；gRPC 客户端修掉组合键不拆分与 `duration: 0` 被吞两处偏差。
3. ✅ **P3 `remote/ctl.py`**（2026-09-20 完成）：registry + 目标解析 + 并发分发 + 结果汇总 + online/offline 回填 + `shot/watch/tail`。**本机自控已实测**（起一个本机 agent，注册成 `self`(WS) 与 `self-grpc`(gRPC) 两台，ping / info / pos / 键预检 / 截屏 / 连续抓帧全部 OK）。
4. **P4 部署包**：pack/agent + PyInstaller + 启动脚本（agent 只支持 Windows，只需 `start-agent.bat`）
5. **P5 验收靶**：target_events.html + verify_events.py + 三传输用例
6. ✅ **P6 冒烟**（2026-09-20 完成了一半）：本机一个 agent 进程注册成**两个别名**（WS 与 gRPC 两条链路）已跑通；真多机、以及 PeerJS 走公网 broker 的跨网验证，等有第二台机器再补。

## 8. 风险与兜底

| 风险 | 兜底 |
|---|---|
| 双端对称 NAT，PeerJS 连不通 | 0.peerjs.com 的 turn0/turn1（若 fork 支持）或自建 coturn；或直接 tailscale 隧道 |
| agent 机防火墙挡入站 | 局域网模式加放行规则；公网模式走 PeerJS（无入站）或隧道 |
| 组合键/拖动误操作真实窗口 | 演示类操作先无焦点 toast 通知；验收只对自建靶页 |
| 中文输入被 IME 吃掉 | 一律 `keyboard.paste`，不逐字符 type |
| 多显示器坐标错位 | `screen.size` 取主屏；region 参数显式指定；**协同的第一步先跑 `screen.calibrate`** —— 多显示器上虚拟桌面原点可能是 (-1920, 0)，这个平移量帧头里没有，只有实测补得回来 |

## 9. 本机已有可复用资产

- `remote/` 三传输 + op 注册表（2026-09-19 跨机 WS 实测：`ping` 通、1680×1050 截图 132ms；agent 侧现在只允许 Windows）
- `remote/input.py` 完整动作集（2026-09-20：多步滚动 / 路径点拖动 / combo 的 `hold_ms` / `hold` 长按），
  `test_remote.py` 145 项测试覆盖（含跨传输等价用例、控制端用例、PeerJS 回环用例、vision 定位、窗口与坐标校准用例），全部用假鼠标键盘断言
- `remote/client.py` 的 `WsClient` / `GrpcClient` / `PeerJsClient`：P3 的分发层直接复用它，不用新写传输
- `remote/ctl.py`（2026-09-20）：多机控制端 —— registry + 目标解析 + 并发分发 + 结果汇总，测试 145 项里的 29 项专测它
- `remote/vision.py`（2026-09-20）：纯 Pillow 视觉定位 —— 颜色找块 / 模板匹配 /
  帧差 / 网格概览 / 主色，给 `remote.client` 的 `locate` 子命令用（见
  `remote/README.md` 3.1.1），测试 145 项里的 7 项
- `remote/calibrate.py`（2026-09-20）：纯 Pillow 坐标校准 —— 闭环实测"图坐标↔鼠标坐标"
  的缩放**与平移**（帧头里只有缩放），给 `remote.client` 的 `calibrate` 子命令用
  （见 `remote/README.md` 3.1.2），测试 145 项里的 12 项
- `remote/peerjs_selftest.py`（2026-09-20）：PeerJS 真机自检入口（连公开 broker，假屏幕假输入）
- 三条踩过的坑已固化：`op` / `check` / `shot` 等命令的目标必须走 `-t/-g/-a`（位置参数互相吞）；测试里起 WS 服务端必须在**同一个协程**里跑完（`asyncio.run` 一结束就关 socket）；gRPC 的 `Ack{ok, message}` 里塞的是 JSON 字符串，客户端要拆回对象才能与 WS 的返回值比（`remote/client.py::_unwrap_ack`）
- `click.html` + `p2p_clicktarget.py`：点击自验证靶（`_clicks.jsonl`）
- `annotate.py`：截图 overlay 网格标注（排障用）
- `remote/toast.py`：无焦点通知（操作前提示不抢焦点）
- 已提交（`1f2f354`）：`ws_server.py` 两处 `capture.backend` → `"pillow"`（screen 重构后遗留的 AttributeError）
