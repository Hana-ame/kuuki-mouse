# 控制端 `remote.ctl` —— 记录 / 注意事项 / 复现指南

> **版本锚点**：本文件对应提交 `c5c6a24`（feat: 多机控制端 P3）+ `2c2a6fb`（docs），分支 `feat-remote-control`。
> 后文所有「实测输出」均来自本机（`DESKTOP-LLULJ2Q` / Windows-10-10.0.19041 / Python 3.10.7 / 1680×1050）于 2026-09-20 的真实执行，耗时与时间每次会变，结构应当一致。

---

## 0. 一页速览

| 问题 | 答案 |
|---|---|
| 控制端是什么 | `remote/ctl.py`，多机**编排层**：registry 起别名 → 按别名/组/全体分发 → 并发 + 逐台超时 → 结果汇总 → online/offline 回填 |
| 和 `remote.client` 的区别 | `remote.client` 是**单机调试级**（地址写在命令行上，一条命令打一个 op）；`remote.ctl` 是**多机控制级**。两者不重叠，别搞混 |
| registry 在哪 | `~/.kuuki/registry.json`（环境变量 `KUUKI_REGISTRY` 或 `--registry` 可改） |
| 退出码 | `0` 全成功 / `1` 有机器执行失败 / `2` 用法错误（未知别名、未知组、坏 JSON）/ `130` Ctrl-C |
| 测试基线 | `python -m pytest test_remote.py -q` → **84 passed / 1 skipped**（跳过的是 Linux-X11 专用键预检） |
| 已实测 | WS + gRPC 本机自控：ping / info / 光标 / 键预检 / 截屏 / 连续抓帧 / 零位移输入 |
| 已实测 | PeerJS：回环 13 项 + 真机自检 `python -m remote.peerjs_selftest`（连公开 broker，192KB 截图分块传送逐字节相同）→ 见 `docs/peerjs-analysis.md` |
| 未实测 | 跨 NAT 的 PeerJS（本机只有一台）、真多机（本机只有一台） |

---

## 1. 记录：这次做了什么

### 1.1 代码改动

| 文件 | 改动 |
|---|---|
| `remote/ctl.py` | **新增**（868 行）。registry 模型 `Machine` / `Registry`；目标解析（位置别名 / `-t` / `-g` / `-a` / `all`，重叠去重）；`dispatch()` 并发分发 + 逐台超时；`call_machine()` / `capture_machine()` 三传输适配；`update_states()` 状态回填；`expand_path()` 多机落盘策略；命令 → op 翻译集中在 `_machine_op()` |
| `remote/client.py` | 修跨传输返回形状：gRPC 无返回值 RPC 用 `Ack{ok, message}` 兜底、`message` 里是 JSON 字符串，WS/PeerJS 直接给对象 → 加 `_unwrap_ack()` 拆回对象 |
| `remote/__init__.py` | 包注释补 ctl |
| `test_remote.py` | 26 → 57 项，新增 15 项专测 ctl（含一个起真 WS 服务端的端到端用例） |

### 1.2 registry 记录结构

```json
{
  "version": 1,
  "machines": {
    "self": {
      "alias": "self",
      "transport": "ws",              // ws | grpc | peerjs
      "endpoint": "ws://127.0.0.1:8765",
      "token": null,
      "os": "win",
      "groups": ["local"],
      "timeout": 10.0,
      "note": "本机回环 WS",
      "state": "online",              // unknown | online | offline，由每次执行回填
      "last_seen": "2026-09-20T01:56:53+08:00",
      "last_error": null
    }
  }
}
```

### 1.3 实测结果（本机自控）

受控端 `python -m remote --ws --grpc`（WS 8765 + gRPC 50051），把 `127.0.0.1` 注册成 `self`(WS) 与 `self-grpc`(gRPC)，同组 `local`：

| 命令 | 实测结果 |
|---|---|
| `ping -a` | 两台 OK，约 93ms / 181ms，返回值字段一致（`pong/ts/uptime_s/version`） |
| `info -a` | 返回 `DESKTOP-LLULJ2Q` / Windows / 1680×1050 / `clipboard_tool: clip` / 43 条 capabilities |
| `pos -g local` | 两台都读到同一根真实光标 `(1046, 551)` |
| `check -g local ctrl shift f5` | 三条全 `supported: true` |
| `shot shot.png -a` | 两张 ~149 KB PNG，自动改名 `shot-self.png` / `shot-self-grpc.png` |
| `watch frames -g local --fps 4 --count 3` | 落到 `frames/<别名>/0001..0003.png`，双机互不覆盖 |
| `move-rel -g local --dx 0 --dy 0` | 输入链路通（返回真实坐标），**光标未移动** |

---

## 2. 命令速查

```bash
PY=.venv-win/Scripts/python.exe          # Windows 用这个 venv

# ---- registry ----
python -m remote.ctl machines add <alias> --transport {ws,grpc,peerjs} \
       --endpoint <ws://host:8765|host:50051|kuuki-mouse-XXXX> \
       [--token T] [-g 组] [--timeout 10] [--note "备注"] [--force]
python -m remote.ctl machines list | show <alias> | rm <alias>

# ---- 只读类（最安全）----
python -m remote.ctl ping  all              # 广播；也可写 -a
python -m remote.ctl info  -g local         # 组播
python -m remote.ctl pos   self self-grpc   # 单播多台（位置别名可用）
python -m remote.ctl check -a ctrl shift f5 # 键预检，不真按
python -m remote.ctl op screen.size -t self # 任意 op

# ---- 画面 ----
python -m remote.ctl shot  out.png  -a                 # 多机自动插别名
python -m remote.ctl watch frames  -g local --fps 4 --count 10
python -m remote.ctl tail  frames  -t self             # 一直抓到 Ctrl-C

# ---- 会真的动鼠标键盘（慎用）----
python -m remote.ctl move     self 800 400 --duration .3
python -m remote.ctl move-rel self --dx 5 --dy -5
python -m remote.ctl click    self --button right --clicks 2
python -m remote.ctl drag     self 10 10 200 200 --duration .5
python -m remote.ctl dragp    self 10,10;100,100;200,200
python -m remote.ctl scroll   self --dy 3 --steps 5
python -m remote.ctl type     self "hello" --interval .01
python -m remote.ctl paste    self "中文"            # 中文/emoji 走 paste，别用 type
python -m remote.ctl key      self f5  |  combo self ctrl+shift+s --hold 200  |  hold self f2 500
```

**全局选项**（写在命令前或后都行）：`--registry PATH`、`--timeout SEC`、`--json`、`--serial`、`--no-update`。

---

## 3. 注意事项

### 3.1 会咬人的坑（按被咬概率排序）

1. **`op` / `check` / `shot` / `watch` / `tail` 的目标只能写 `-t/-g/-a`，不能写位置别名。**
   这 5 个命令的位置参数是 op 名 / 键名 / 路径，`positional_targets=False`。写 `python -m remote.ctl shot out.png self` → `self` 会被当成多余位置参数报 argparse 错误。
   能写位置别名的是：`ping` `info` `pos` `move` `move-rel` `click` `down` `up` `scroll` `scroll-h` `drag` `dragp` `type` `paste` `key` `combo` `hold`。
   *记不住就永远用 `-t/-g/-a`，一定不会错。* ← 这条已被测试 `test_ctl_op_and_check_take_targets_by_flag` 锁住。

2. **每次动作默认会写 registry**（online/offline + `last_seen` + `last_error`）。
   只想探活、不想改文件 → 加 `--no-update`。

3. **退出码 1 ≠ 2**。`ping ghost`（未知别名）是**用法错误**，`exit=2` 且在 stderr 打印 `未知别名 'ghost'; 已注册: ...`；而机器连不上是**执行失败**，`exit=1` 且在最后汇总 `1/1 台失败: dead`。写脚本判断时别混：

   ```text
   FAIL dead          1503.9ms  TimeoutError: 超过 1.5s 未响应
   [exit=1]
   ```

4. **别在这台正在被使用的机器上跑 `move/click/type/scroll`。** 它们会真的操作光标键盘。
   要验证输入链路通不通，用**零位移**：`move-rel -g local --dx 0 --dy 0`（有返回值、光标不动）。

5. **端口 8765 / 50051 容易被残留进程占死。** 起受控端前先 `netstat -ano | grep -E "8765|50051"`，有就 `taskkill /F /PID <pid>`（**单斜杠 `/F`**，写 `//F` 会报错）。昨天就撞过一次：起服务看不到端口。 受控端起不来时先看这里，别急着改代码。

6. **跨传输返回值形状不完全相同，按 key 取值，别做字符串或顺序敏感的比较。**
   已修了最扎眼的一处（`Ack{ok, message}`），但仍有残留差异，例如：

   ```text
   WS:    {"ok": true, "keys": [{"key": "ctrl", "supported": true, "reason": null}, ...]}
   gRPC:  {"keys": [{"key": "ctrl", "supported": true, "reason": ""}, ...]}
   ```

   `"ok"` 字段位置、`reason` 的 `null` vs `""` 都不同。要机器可读就用 `--json`。

7. **`GrpcClient` 是同步的，`WsClient`/`PeerJsClient` 是 asyncio。** ctl 内部用 `asyncio.to_thread` 包住 gRPC 那路，所以并发时 gRPC 机器不会拖住 asyncio 连接。别在外部直接混用这三个 Client 而不处理这件事。

### 3.2 安全

8. **token 明文存盘。** ctl 新建 registry 时会尽力收紧权限（Windows 走 `icacls` 先 `/grant:r user:(F)` 再 `/inheritance:r`——顺序反了会把 ACL 清成空的；POSIX 走 `chmod 0o600`），但这是尽力而为，失败了只静默跳过。
   不想落盘就让 registry 的 `token` 留空，改由受控端读环境变量 `KUUKI_REMOTE_TOKEN`。

9. **受控端默认只绑 `127.0.0.1`。** 要跨机必须 `python -m remote --host 0.0.0.0 --allow-remote --token <强口令>`——`--allow-remote` 必须同时带 `--token`，否则启动会被拒。**别在不可信网络里裸奔**，这是完全控制权限的服务。

10. **受控端只支持 Windows。** `remote/__main__.py` 有 `SUPPORTED_PLATFORMS = ("win32",)` 门禁，非 Windows 平台在 `--help`/`--version` 之后、起服务前以 **退出码 2** 拒绝（不静默跑半截）。

### 3.3 本机环境

11. **用 `.venv-win`（Python 3.10.7），别用仓库里的 `.venv`**——那个 venv 的基解释器 `C:\Python312` 已被卸载，`python.exe` 直接报找不到解释器。所有命令统一走 `.venv-win/Scripts/python.exe`。

12. **Bash 里 PATH 缺 coreutils**，用到 `sed/awk/grep -E` 前先
    `export PATH="/c/Program Files/PortableGit/usr/bin:/c/Program Files/PortableGit/mingw64/bin:$PATH"`。

13. **`timeout -s KILL Ns cmd | tail` 是假的超时**——子进程继承管道写端，父进程被杀后 `tail` 一直等 EOF。长时间任务请「后台重定向到文件 + 轮询文件」。

14. **git push 走 HTTPS 会永久卡在 `Pushing to https://...`**（本机 credential helper 无 GitHub 凭据，助手在非终端环境里死等）。已把 `remote.origin.pushurl` 改成 `git@github.com:Hana-ame/kuuki-mouse.git`，以后直接 `git push` 即可。

---

## 4. 复现指南（从零到通，约 5 分钟）

> 全程在一台 Windows 上，受控端与受控目标都是本机；**不移动光标、不按键**，除了最后一步可选。

### Step 0 · 前置检查

```bash
cd /d/WorkPlace/kuuki-mouse
export PATH="/c/Program Files/PortableGit/usr/bin:/c/Program Files/PortableGit/mingw64/bin:$PATH"
PY=.venv-win/Scripts/python.exe

$PY --version                                   # 期望: Python 3.10.7
$PY -m remote --selftest                        # 期望: 自检通过 (不启动服务, 不动光标)
netstat -ano | grep -E "8765|50051"             # 期望: 空; 有占用则 taskkill /F /PID <pid>
```

### Step 1 · 起受控端（WS + gRPC）

```bash
$PY -u -m remote --ws --grpc
```

另开一个终端确认端口：

```bash
netstat -ano | grep -E "8765|50051"
#  TCP    127.0.0.1:8765    0.0.0.0:0    LISTENING   <pid>
#  TCP    127.0.0.1:50051   0.0.0.0:0    LISTENING   <pid>
```

> 若用后台方式起，务必用「重定向到文件再轮询」，不要用 `| tail`（见注意 13）。

### Step 2 · 注册本机的两条链路

```bash
$PY -m remote.ctl machines add self       --transport ws   --endpoint ws://127.0.0.1:8765 \
       -g local --note "本机回环 WS"   --force
$PY -m remote.ctl machines add self-grpc  --transport grpc --endpoint 127.0.0.1:50051 \
       -g local --note "本机回环 gRPC" --force
$PY -m remote.ctl machines list
```

期望：

```text
registry: C:\Users\lumin\.kuuki\registry.json
  self          ws      ws://127.0.0.1:8765 (ws)         组=local      unknown
  self-grpc     grpc    127.0.0.1:50051 (grpc)           组=local      unknown
```

### Step 3 · 只读四连（验证链路）

```bash
$PY -m remote.ctl ping all
$PY -m remote.ctl info -g local
$PY -m remote.ctl pos  -g local
$PY -m remote.ctl check -a ctrl shift f5
```

期望 `ping`（结构一致即可，耗时/uptime 每次不同）：

```text
-> ping {}   目标: self, self-grpc
OK   self            93.2ms  {"ok": true, "pong": true, "ts": 1789840596.4453678, "uptime_s": 1291.472, "version": "0.1.0"}
OK   self-grpc      181.5ms  {"pong": true, "ts": 1789840596.534595, "uptime_s": 1291.561, "version": "0.1.0", "ok": true}
[exit=0]
```

`pos` 应返回**同一个**真实光标坐标（证明两条链路打的是同一台机器）：

```text
-> mouse.position {}   目标: self, self-grpc
OK   self            94.9ms  {"ok": true, "x": 1046, "y": 551}
OK   self-grpc      181.4ms  {"x": 1046, "y": 551}
```

### Step 4 · 画面回显

```bash
$PY -m remote.ctl shot  .workbuddy/tmp/shot.png   -a
$PY -m remote.ctl watch .workbuddy/tmp/frames -g local --fps 4 --count 3
find .workbuddy/tmp/frames -type f | sort
```

期望：

```text
OK   self      370.6ms  {"path": ".../shot-self.png",      "bytes": 149026, "width": 1680, "height": 1050, "transport": "ws"}
OK   self-grpc 448.3ms  {"path": ".../shot-self-grpc.png", "bytes": 149057, "width": 1680, "height": 1050, "transport": "grpc"}

OK   self      1315.6ms  {"frames": 3, "bytes": 447408, "dir": ".../frames1\\self",      "last": "...\\0003.png"}
OK   self-grpc 1370.9ms  {"frames": 3, "bytes": 447343, "dir": ".../frames1\\self-grpc", "last": "...\\0003.png"}

.workbuddy/tmp/frames/self-grpc/0001.png
.workbuddy/tmp/frames/self-grpc/0002.png
.workbuddy/tmp/frames/self-grpc/0003.png
.workbuddy/tmp/frames/self/0001.png
.workbuddy/tmp/frames/self/0002.png
.workbuddy/tmp/frames/self/0003.png
```

落盘规则：多机 + 带扩展名 → `<名>-<别名>.<ext>`；多机 + 无扩展名 → 当目录，按 `<dir>/<别名>/NNNN.png`；路径里写 `{alias}` 占位符可自定义。

---

## 4.5 多机 / 多操纵端：没有真设备时用 dummy 练

只有一台机器也能验证「一主多从」—— `remote/dummy.py` 起一批**仿真受控端**：真 `RemoteService`
+ 真 WS/gRPC 服务端，只把最底层的抓屏和输入换成假的。每台有自己的屏幕尺寸、响应延迟、操作日志，
可以对指定那台单独注入故障。**关键是每台能分辨**：全都返回同一个答案的话，广播和单发就分不出来。

### 起一批

```bash
python -m remote.dummy --count 3 --latency 0.08 --bootstrap-registry .workbuddy/tmp/swarm.json
```

```text
起了 3 台仿真受控端 (Ctrl-C 停止):
  dummy1       ws=ws://127.0.0.1:63579  size=320x200
  dummy2       ws=ws://127.0.0.1:63580  size=400x250
  dummy3       ws=ws://127.0.0.1:63581  size=256x160

registry 已写好: .workbuddy/tmp/swarm.json
试: python -m remote.ctl ping --all --registry .workbuddy/tmp/swarm.json
```

常用参数：`--transport ws|grpc|both`（每台同时开两种传输）、`--latency S` 固定延迟、
`--jitter S` 随机抖动、`--fail-ops mouse.click` 给**最后一台**注入指定 op 的故障、
`--names a,b,c` 自定义名字。

### 一主多从：验证各答各的

```bash
R=.workbuddy/tmp/swarm.json
python -m remote.ctl ping --all --registry $R
python -m remote.ctl shot frames -a --registry $R     # 三张不同尺寸的图
```

```text
OK  dummy1  190.0ms  {"ok": true, "pong": true, ..., "device": "dummy1"}
OK  dummy2  190.0ms  {"ok": true, "pong": true, ..., "device": "dummy2"}
OK  dummy3  189.9ms  {"ok": true, "pong": true, ..., "device": "dummy3"}

OK  dummy1  {"path": "frames\dummy1.png", "bytes": 515, "width": 320, "height": 200, ...}
OK  dummy2  {"path": "frames\dummy2.png", "bytes": 707, "width": 400, "height": 250, ...}
OK  dummy3  {"path": "frames\dummy3.png", "bytes": 399, "width": 256, "height": 160, ...}
```

`info` / `ping` 的返回里多点一个 `device` 字段就是为这个——`ping` 看起来都一样，但能确认
命令确实分头到了三台。尺寸/字节数不同则是更强的证据：拿到的是各自的画面，不是同一张图复制三份。

### 多个操纵端同时下发

真正的多个操纵端是**多个 ctl 进程**同时对同一批机器下手：

```bash
python -m remote.ctl ping --all --registry $R &     # 操纵端 A
python -m remote.ctl ping --all --registry $R &     # 操纵端 B
wait
```

两个进程都拿到完整的三条结果，registry 里三台都落成 `online`，文件没被写坏。

> **这里踩过三个 Windows 特有的坑**（都已在 `remote/ctl.py` 里修掉，别再退回去）：
>
> 1. **registry 是 load→改→save，不是原子的。** 两个操纵端各自 load 到同一份快照，后写的把先写的
>    整份冲掉 —— 实测双线程各 add 25 台，**最后只剩 26 台，丢了 24 条**。现在是
>    `Registry.transaction()`：在文件锁里**重新读盘**再改再写。
> 2. **锁要两层。** POSIX 的 `flock` 按 open file description 互斥，同进程多线程也管得着；
>    但 Windows 的 `msvcrt.locking` **只跨进程生效** —— 实测同进程 4 个线程能同时进临界区
>    （峰值 4/4）。所以 `FileLock` 里额外叠了一把 `threading.Lock`。
> 3. **判断锁文件是否为空，别用 `read(1)`。** Windows 上别人锁住的字节范围对其它进程**不可读**，
>    `read` 直接 `PermissionError`，被当成「锁不上」静默退化 —— 结果是锁从来没生效过。改用
>    `os.path.getsize`。同理：对面正在 `os.replace` 的瞬间，另一进程的 `open(path,'r')` 也会
>    `PermissionError`（Errno 13），读侧 `_read_json` 要退避重试。
>
> 附带一条：`os.replace` 的目标被别的线程打开时会 WinError 5「拒绝访问」（POSIX 没这回事），
> `Registry.save` 里加了退避重试。临时文件名还必须带随机串 —— 只带 pid 的话，同一进程的多个操纵端
> 会 truncate 同一个 tmp（实测 50 台 → 只剩 1 台）。

### 对应的测试

`test_remote.py` 里 13 项（合计 111 passed / 1 skipped），全部不碰真实光标：

| 测试 | 验的是什么 |
|---|---|
| `test_dummy_swarm_gives_each_device_its_own_identity` | 每台端口/尺寸都不同 —— 一切「证明分头执行」断言的前提 |
| `test_multi_machine_broadcast_reaches_every_device` | 一条广播，三台各收到一次（查 `device.received`） |
| `test_multi_machine_each_device_answers_for_itself` | 返回值 + 屏幕尺寸各不相同（证明不是把同一台打三遍） |
| `test_multi_machine_group_casts_only_to_members` | 组播只动组内，外的不被波及 |
| `test_multi_machine_input_lands_on_every_device` | 输入类 op 落在每台自己的假鼠标/假键盘上 |
| `test_multi_machine_one_faulty_device_does_not_sink_the_batch` | 一台注定失败，其余照常且只有它标 offline |
| `test_multi_machine_concurrent_really_is_concurrent` | 并发墙钟明显小于串行累加 |
| `test_registry_concurrent_writes_keep_every_entry` | 多线程并发改 registry 不丢条目（上面坑 1 的守门） |
| `test_registry_transaction_does_nothing_when_block_raises` | `transaction` 里抛异常不许落盘 |
| `test_registry_save_leaves_no_temporary_files` | 原子写不留垃圾 |
| `test_two_controllers_dispatch_at_the_same_time` | 两个操纵端并行下发，registry 不坏 |
| `test_second_controller_can_use_its_own_aliases` | 同一设备在两个操纵端里可以叫不同别名 |
| `test_dummy_cli_smoke` | dummy CLI 参数至少能被自己的 parser 认下 |

> 注意：写多机测试时，**集群必须跑在自己的事件循环线程里**（`swarm.serve_in_thread()`）。
> 如果把 dummy 与控制端塞进同一个线程，控制端一阻塞服务端的 loop 就转不动，现象是
> **所有机器一起超时**，很容易误判成「并发分发坏了」。

---

### Step 5 · 输入链路（零位移，安全）

```bash
$PY -m remote.ctl move-rel -g local --dx 0 --dy 0
```

```text
-> mouse.move_rel {"dx": 0, "dy": 0, "duration": 0.0}   目标: self, self-grpc
OK   self        89.0ms  {"ok": true, "x": 1046, "y": 551, "dx": 0, "dy": 0}
OK   self-grpc  177.4ms  {"x": 1046, "y": 551, "dx": 0, "dy": 0, "ok": true}
```

**确实要动**的时候再跑真的，例如 `$PY -m remote.ctl move self 800 400 --duration .3`。

### Step 6 · 故障与用法错误（验证汇总逻辑不串味）

用临时 registry，别污染自己的：

```bash
R=.workbuddy/tmp/reg_fail.json; rm -f "$R"
$PY -m remote.ctl machines add dead --transport ws --endpoint ws://127.0.0.1:9 -g demo --registry "$R"
$PY -m remote.ctl ping -a --timeout 1.5 --registry "$R"
$PY -m remote.ctl ping ghost           --registry "$R"
$PY -m remote.ctl machines list        --registry "$R"
```

期望：

```text
-> ping {}   目标: dead
FAIL dead          1503.9ms  TimeoutError: 超过 1.5s 未响应
                                    (stderr) 1/1 台失败: dead        → exit 1

                                    (stderr) "未知别名 'ghost'; 已注册: dead"  → exit 2

  dead         ws      ws://127.0.0.1:9 (ws)  组=demo   offline     ← 状态已回填
```

### Step 7 · 跑测试 + 校验文档命令

```bash
$PY -m pytest test_remote.py -q        # 期望: 57 passed, 1 skipped
$PY check_ctl_docs.py                  # 期望: 51/51 条文档命令通过 parser
```

`check_ctl_docs.py` 把 README / docs 里**代码块中**的每条 `remote.ctl` 命令喂给真正的 parser —— 命令一改而文档没跟上，这里会直接失败。它只扫代码块，因为正文里有「这样写是错的」这类反例。

ctl 的 15 项测试全部不联网；其中 `test_ctl_end_to_end_over_ws` 会起一个**随机端口**的真 WS 服务端跑完整链路（假屏幕 + 假输入），端口必须在**协程内**取——`asyncio.run` 结束后取值会得到 WinError 10038「非套接字」。

### Step 8 · 收尾

```bash
# 关受控端: 找到 Step 1 的 pid
netstat -ano | grep -E "8765|50051"
taskkill /F /PID <pid>

# 可选: 清掉本次演示产物
rm -rf .workbuddy/tmp/shot*.png .workbuddy/tmp/frames* .workbuddy/tmp/reg_fail.json
```

registry（`C:\Users\lumin\.kuuki\registry.json`）里留着 `self` / `self-grpc` 是安全的——它们只是地址记录，下次起服务端就能直接用。

---

## 5. 排错表

| 症状 | 原因 | 处置 |
|---|---|---|
| `Pushing to https://...` 卡死不动 | HTTPS 凭据助手在非终端环境死等 | 走 SSH：`git push`（pushurl 已改好） |
| 起受控端后 `netstat` 看不到端口 | 端口被昨天的残留进程占着 | `netstat -ano` 找 PID，`taskkill /F /PID`（单斜杠） |
| `argparse` 报「无法识别的参数」 | 给 `op/check/shot/watch/tail` 传了位置别名 | 改成 `-t/-g/-a`（注意 1） |
| `python: 找不到指定的程序` / 解释器不存在 | 用了坏掉的 `.venv`（基解释器 `C:\Python312` 已删） | 改用 `.venv-win` |
| 非 Windows 上启动即退出码 2 | 受控端平台门禁 | 预期行为，受控端只支持 Windows |
| gRPC 那台总比 WS 慢一截 | 首次 RPC 要建 channel + 握手 | 正常；真要压测就预热连接 |
| 批量命令里一台超时，其余也慢 | 默认并发，但 `--serial` 会串行 | 去掉 `--serial`；压低单台 `--timeout` |
| `--allow-remote` 被拒 | 必须同时给 `--token` | 加 `--token` |
| `tail` 一直抓停不下来 | 设计如此 | Ctrl-C（`exit=130`） |

---

## 6. 还没验证的部分

- **PeerJS 跨 NAT**：本机同机两端已经真跑通了（见 `docs/peerjs-analysis.md`），但同机 WebRTC 走 host 候选，跨网才真的需要 STUN 打洞 —— 得有第二台机器或另一条网络才能验。
- **PeerJS 弱网**：broker 断连、ICE 失败后的重连没验过；现在的行为是抛超时、标 offline，不重试。
- **真多机**：本机只有一台，所谓「两台」是同一台的两个别名。多机的并发分发、连接池、offline 标记都在单元测试里用假 worker 覆盖过，但没有真实第二台机器背书。
- **连接池**：现在每条命令各建一次连接。机器少无所谓，机器多/命令密会明显慢，属于后续优化项。

后续阶段：`P4 部署包`（pack/agent + PyInstaller + `start-agent.bat`）、`P5 验收靶`（target_events.html + verify_events.py + 三传输用例）。详见 `docs/puppet-multi-machine.md` 第 7 节。

---

## 7. 相关文档 / 脚本

| 文件 | 内容 |
|---|---|
| `docs/puppet-multi-machine.md` | 多机方案总纲（第 4 节控制端命令表，第 9 节踩过的坑） |
| `remote/README.md` | remote 扩展完整文档（3.1 单机调试级 `remote.client`，3.2 多机控制级 `remote.ctl`） |
| `remote/ctl.py` | 控制端实现，命令 → op 的翻译集中在 `_machine_op()` |
| `test_remote.py` | 111 项测试，含控制端与 PeerJS 的端到端用例 |
| `remote/peerjs_selftest.py` | PeerJS 真机自检（连公开 broker，假屏幕假输入） |
| `check_ctl_docs.py` | 校验文档代码块中的命令是否还被 parser 认得 |

> 建议把 `python check_ctl_docs.py` 和 `python -m pytest test_remote.py -q` 一起当作改完 ctl 的收尾动作 —— 这条复现指南就是这么维持不烂的。
