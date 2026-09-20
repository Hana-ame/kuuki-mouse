# 受控端打包: PyInstaller + GitHub Actions

**只打一个东西: `kuuki-agent.exe` —— 受控端。** 控制端 (`-m remote.ctl` /
`-m remote.client`) 保持源码运行, 不打进这个包。

理由很直白: 受控端是"被操作的那台机器", 它要分发到别人电脑上, 那边往往没装 Python;
控制端一直在你自己的开发环境里跑, 打包只是给自己找麻烦 (改一行就要重新构建 + 重传)。

---

## 1. 产物与触发

| 触发 | 产物 |
|---|---|
| push 到 `feat/remote-control` / `master`、PR、`workflow_dispatch` | Actions artifact `kuuki-agent-windows-x64.zip` (保留 14 天) |
| push `v*` tag | 上面那个 artifact **+** GitHub Release, 挂同名 zip |

只在 `windows-latest` 上构建: 受控端本来就只支持 Windows (`remote/__main__.py` 的
`platform_refusal()` 门禁), 在别的平台上打出来也是个一启动就退出的 exe。

发一次 release::

    git tag v0.1.0 && git push origin v0.1.0

---

## 2. 本地复现

```bash
pip install -r requirements.txt -r requirements-remote.txt -r requirements-build.txt
pyinstaller packaging/kuuki-agent.spec --noconfirm --distpath dist --workpath build/agent

# 检查惰性导入的依赖有没有打进去 (不依赖屏幕和网络)
python packaging/check_bundle.py build/agent/kuuki-agent

# 冒烟
dist/kuuki-agent/kuuki-agent.exe --version
dist/kuuki-agent/kuuki-agent.exe --selftest
```

产物在 `dist/kuuki-agent/` (**onedir**, 不是 onefile)。

---

## 3. 为什么是 onedir 而不是 onefile

`aiortc` / `av` 带一堆 DLL, 整个目录约 **124 MB**。onefile 每次启动都要把这 124 MB
解到临时目录再跑, 冷启动慢到没法用; onedir 直接躺在磁盘上, 启动就是启动。
分发时压成 zip, 使用者解压即可 —— 这一步由 CI 的 `Compress-Archive` 完成。

---

## 4. 踩到的坑 (改 spec 前先看这里)

### 4.1 三处运行期 import, 静态分析看不见

下面这些 import 都在**函数体里**, PyInstaller 扫不到, 打出来的 exe `--help` 一切正常,
一用到就 `ModuleNotFoundError`:

| 位置 | 运行期 import |
|---|---|
| `remote/peerjs_server.py` / `remote/peerjs_client.py` | `from peerjs.peer import Peer, PeerOptions` |
| `remote/service.py` (kuuki 透传) | `from app import handle_message` |
| `remote/input.py` | `from controller import PynputMouseController` |

所以 spec 里 `hiddenimports` 那一长串不是凑数。改完 spec 记得跑
`packaging/check_bundle.py` —— 它就是为这三处存在的。

### 4.2 `peerjs/` 是没有 `__init__.py` 的隐式命名空间包

`collect_submodules("peerjs")` 对命名空间包不保证奏效, 所以 spec 里把 11 个模块
**显式列全** (`peerjs.api` / `peerjs.peer` / `peerjs.dataconnection` …)。
以后往 `peerjs/` 加新模块, 记得同步 `packaging/kuuki-agent.spec` 的 `peerjs_hidden`。

### 4.3 别加 pywin32

`win32api` / `win32con` / `win32gui` 项目根本没用 —— pynput 的 Win32 后端走 ctypes。
加进 hiddenimports 只会在没装 pywin32 的机器上刷一排 `Hidden import not found`,
看着像构建失败其实不影响产物。

### 4.4 spec 里的相对路径是相对 spec 所在目录

`Analysis(["packaging/agent_entry.py"])` 会被解析成 `packaging/packaging/agent_entry.py`
(基准是 `SPECPATH`, 不是 CWD)。spec 里统一用 `os.path.join(SPECPATH, ...)`。

### 4.6 本地 venv 会掩盖 requirements 的缺项 (最重要的一条)

`peerjs/api.py` 顶层就 `import aiohttp`, 但 `requirements.txt` 里**没有** aiohttp ——
aiortc 1.11+ 已经不再间接带它。本地 venv 是多年前手动攒的, 里面躺着 aiohttp,
于是本地怎么跑都正常, 谁也没发现清单少东西。

CI 的干净环境一跑就露馅: `check_bundle.py` 报 `MISS aiohttp`, 模块总数 752
(本机 856, 差的就是 aiohttp 一族)。补进 `requirements.txt` 才真正修好。

**这条不只是打包问题** —— 任何人在新环境里 `pip install -r requirements.txt` 然后
`python -m remote` 开 PeerJS, 都会 `ModuleNotFoundError`。CI 顺手把它挖出来了。

教训: 本地 venv 越"全", 越容易掩盖依赖清单的洞。别信"我这儿能跑"。

### 4.7 别用"目录是否存在"判断模块有没有打进去

纯 Python 包 (`peerjs` / `pynput` / `aiortc`) 会被塞进 PYZ 归档, 在
`_internal/` 下**没有对应目录**。第一次写 CI 时按目录检查, 4 个模块全报 MISS,
但 exe 实际跑得好好的 —— 假警报。判断要看 PYZ 的模块清单, 即 `check_bundle.py` 做的事。

---

## 5. CI 的四层冒烟

| 层 | 命令 | 失败是否阻塞 | 说明 |
|---|---|---|---|
| 1 | `--version` / `--help` | 阻塞 | 证明 exe 能启动、argparse 与 remote 包本体正常 |
| 2 | `check_bundle.py` | 阻塞 | 静态查 PYZ 清单, 覆盖 4.1 的惰性导入 |
| 3 | `--selftest` | **不阻塞** | 要桌面会话; runner 是服务会话, 抓不到屏会失败, 但能过就是强证据 |
| 4 | 真起 PeerJS | **不阻塞** | 要连 `0.peerjs.com`, 外网抖动会红 |

第 3、4 层设成 `continue-on-error` 是因为它们依赖 runner 环境, 不是构建质量问题。
看构建结果时重点看第 1、2 层。

---

## 6. 实测记录

### 本机 (2026-09-20, Windows 10 + Python 3.10)

- 构建 41 秒, 856 个模块, 产物 124 MB
- `--version` → `kuuki remote 0.1.0`
- `--selftest` → `PASS`, 截屏 1680x1050 / 241 144 字节 / 274 ms, PNG 魔数正确
- 默认启动 (只开 PeerJS) → 3 秒注册到公开 broker, 房间码 `kuuki-mouse-7DSGJ`
  (这条最关键: 证明 peerjs + aiortc + av + aioice 一整套 WebRTC 栈都真的进去了)
- `check_bundle.py` → 4 组 26/26 全命中

### CI (2026-09-20, windows-latest + Python 3.11, run 35477438541)

- 全流程 1 分 21 秒, **866 个模块**, zip 产物 **57.2 MB** (解压后 124 MB)
- 四层冒烟全部通过, 包括两层"看环境脸色"的:
  - `--selftest` → `PASS`, 截屏 **1024x768** / 215 128 字节 / 198 ms
    (runner 有桌面会话, 能真抓屏 —— 比预期强)
  - 默认启动 (只开 PeerJS) → **1.2 秒**注册到公开 broker, 房间码 `kuuki-mouse-R93SG`

注: 本机 856 / CI 866 的差值来自 Python 版本 (3.10 vs 3.11) 与 stdin 的模块集合差异,
不是缺件 —— `check_bundle.py` 的 26 项必需模块两边都全命中。

---

## 7. 已知未做

- 没做代码签名 —— 分发出去会被 SmartScreen 拦, 使用者得点"仍要运行"
- 没做 macOS / Linux 产物 (受控端本就不支持, 见第 1 节)
- 没精简体积: `pytest` / `tkinter` 已排除, 但 `av` 的编解码器没挑过, 还有压缩空间

### 7.1 排除 tkinter 的代价: exe 里的 `notify` 用不了

`tkinter` 被排除是为了体积 (它会拖进整套 Tcl/Tk 运行时), 代价是 `remote/toast.py`
的角标通知在 exe 里走不通 —— `notify_supported()` 探不到 tkinter。

这不是静默失败: `_op_notify` 会先检查并抛 `unsupported`, 控制端拿到的是
`"角标通知需要被控端有 tkinter 图形环境"`, 而不是一个假的 `shown: false`。
源码运行 (`python -m remote`) 时桌面 Python 自带 tkinter, 功能正常。

要让分发版也支持, 把 spec 的 `excludes` 里那行 `tkinter` 删掉即可, 代价是体积。
