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
| 局域网 | WS | `python -m remote --no-grpc --no-peerjs --allow-remote --token T` | `ws://<ip>:8765/?token=T` | agent 机防火墙放行 8765 入站 |
| 局域网 | gRPC | 同上（默认 50051） | `<ip>:50051` + token | 同上 50051 |
| 公网/异地（非对称 NAT） | PeerJS | `python -m remote --no-ws --no-grpc --room XXXX --token T`（**无端口**） | `--peer kuuki-mouse-XXXX` | 双端能连 0.peerjs.com 公网 broker |
| 对称 NAT / 求稳 | 隧道 | tailscale/zerotier 组网后，WS/gRPC 走隧道虚拟 IP | `ws://<tunnel-ip>:8765` | 两端装隧道软件，无防火墙烦恼 |

- PeerJS 是异地最省事的路（免端口/域名/证书），但**双端都在对称 NAT 后**需要 TURN 兜底（见风险节）。
- 隧道是**最稳**的路：把「公网可达」这件事外包给 tailscale，传输层仍用现成 WS/gRPC。

## 3. 动作集（op 表，三传输共用）

### 已有（2026-09-19 实测可用）

| op | 说明 | 关键参数 |
|---|---|---|
| `mouse.position` | 读光标 | — |
| `mouse.move` | 绝对移动（可平滑） | x, y, duration |
| `mouse.move_rel` | 相对移动 | dx, dy, duration |
| `mouse.click` | 单击/双击/右键，hold 默认 60ms | button, clicks, interval, hold |
| `mouse.down` / `mouse.up` | 按住/松开 | button |
| `mouse.scroll` | 滚轮（单步） | dx, dy |
| `mouse.drag` | 两点拖动：按下→平滑→松开 | x1,y1,x2,y2, button, duration |
| `keyboard.type` | 文本输入（逐字符 interval；中文/emoji 必须改用 `keyboard.paste`） | text, interval |
| `keyboard.key` | 单键 tap/press/release + 修饰键 | key, action, modifiers |
| `keyboard.hotkey` | 组合键：依序按下→逆序松开 | keys=["ctrl","shift","s"] 或 "ctrl+shift+s" |
| `keyboard.paste` | 剪贴板 + Ctrl+V（中文/emoji 可靠输入） | text |
| `keyboard.check` | 键支持性预检（不实际按键） | keys |

### 方案新增 / 增强

| op | 说明 | 参数 |
|---|---|---|
| `mouse.scroll` 增强 | **平滑多步滚动**；可先定位再滚 | dx, dy, steps=1, interval=0.05, x?, y? |
| `mouse.drag` 增强 | **路径点拖动**：首点按下→逐段平滑→末点松开 | points=[[x,y],...], button, duration（每段） |
| `keyboard.combo` | 组合键 + 可按住时长（长按场景） | keys / "ctrl+shift+s", hold_ms=0 |
| `keyboard.hold` | 按住 N 毫秒再松开（F2 重命名这类长按） | key, ms |
| `mouse.scroll_h` | 横向滚动别名（=scroll dx） | dx, dy=0, steps |

**向后兼容**：`scroll` 不传 steps 即单步（现行为）；`drag` 不传 points 仍认 x1/y1/x2/y2（两点 = points 两元素）。

## 4. 控制器 `kuuki_ctl`（新）

```
kuuki_ctl machines list | add <alias> --transport ws|grpc|peerjs --endpoint <url|ip|peer> [--token T] [--group g]
kuuki_ctl <alias> ping | info | shot [out.png] | watch [--fps 2] [--count 10]
kuuki_ctl <alias> move X Y [--duration .3] | click [--button right] [--clicks 2] [--hold .06]
kuuki_ctl <alias> drag x1,y1,x2,y2 [--button left] [--duration .3] | dragp x1,y1;x2,y2;x3,y3
kuuki_ctl <alias> scroll [--dx 0] [--dy 3] [--steps 5] [--x X] [--y Y]
kuuki_ctl <alias> type "文本" | paste "中文" | combo ctrl+shift+s [--hold 200] | hold f2 500 | key f5
kuuki_ctl all ping | all shot  | group <g> shot     # 广播 / 组播
kuuki_ctl tail <alias>           # 连续 watch 存帧目录（回显窗口）
```

- 实现：复用 `remote/client.py` 的 `WsClient / GrpcClient / PeerJsClient`，新增多机分发层（registry 读写、并发分发、结果汇总）。
- 截图回显：`shot` 存文件；`watch`/`tail` 用已有 `_write_frame` 连续落盘。
- 统一超时与重试：每台 agent 动作默认 10s 超时，断线标记 offline。

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

1. **P1 `remote/input.py`**：scroll 平滑多步、drag 路径点、combo/hold（带单测）
2. **P2 `remote/service.py` + proto**：注册新 op/别名；gRPC 补 RPC，保持三传输一致
3. **P3 `kuuki_ctl.py`**：registry + 单发/广播/组播 + watch 回显
4. **P4 部署包**：pack/agent + PyInstaller + 各 OS 启动脚本
5. **P5 验收靶**：target_events.html + verify_events.py + 三传输用例
6. **P6 冒烟**：本机起两个不同房间码的 agent 实例，controller 分别连（双机占位）；真多机等有第二台机器再跑

## 8. 风险与兜底

| 风险 | 兜底 |
|---|---|
| 双端对称 NAT，PeerJS 连不通 | 0.peerjs.com 的 turn0/turn1（若 fork 支持）或自建 coturn；或直接 tailscale 隧道 |
| agent 机防火墙挡入站 | 局域网模式加放行规则；公网模式走 PeerJS（无入站）或隧道 |
| 组合键/拖动误操作真实窗口 | 演示类操作先无焦点 toast 通知；验收只对自建靶页 |
| 中文输入被 IME 吃掉 | 一律 `keyboard.paste`，不逐字符 type |
| 多显示器坐标错位 | `screen.size` 取主屏；region 参数显式指定；后续加 monitor 枚举 op |

## 9. 本机已有可复用资产

- `remote/` 三传输 + op 注册表（2026-09-19 跨机 WS 实测：`ping` 通、1680×1050 截图 132ms；agent 侧现在只允许 Windows）
- `click.html` + `p2p_clicktarget.py`：点击自验证靶（`_clicks.jsonl`）
- `annotate.py`：截图 overlay 网格标注（排障用）
- `remote/toast.py`：无焦点通知（操作前提示不抢焦点）
- 已提交（`1f2f354`）：`ws_server.py` 两处 `capture.backend` → `"pillow"`（screen 重构后遗留的 AttributeError）
