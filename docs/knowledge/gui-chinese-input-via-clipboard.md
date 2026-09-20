# 中文输入走剪贴板 + Ctrl+V，不要用逐字注入

`keyboard.type` 注入中文不可靠：返回 `chars: 61` 看着成功，但文字根本没进输入框
（pynput 的 Unicode 注入方式与某些页面的输入处理冲突）。

**可靠做法**：写入剪贴板 → 粘贴。

```
点输入框 → ctrl+a → delete（清掉上一次的残留）→ ctrl+v → enter
```

## 剪贴板写入的坑：`clip.exe` 按 GBK 解释 stdin

```bash
printf '你好' | clip        # 错：UTF-8 字节被 clip.exe 按 GBK 解释 → 粘出来全是乱码
```

**服务端 `keyboard.paste` 同样踩过这条（2026-09-20 已修）**：`paste_text()` 原来
把 UTF-8 字节喂给 `clip`，中文 Windows 的控制台代码页是 936（GBK），"你好kuuki"
粘出来是 "浣犲ソkuuki"。修法：Windows 上不再起子进程，直接 ctypes 调
`SetClipboardData(CF_UNICODETEXT, ...)` 写 UTF-16LE（`remote/input.py` 的
`_set_clipboard_windows`），剪贴板原生就是 Unicode 格式，无编码转换路径。
有单元测试往返验证（中文 + emoji 无损），真机记事本截图复核过。

**控制端自己写剪贴板时**（不走服务端）也要注意同一条：

```powershell
Set-Clipboard -Value (Get-Content -Raw -Encoding UTF8 msg.txt)   # PowerShell 工具里
```

或者先把文本以 UTF-8 写盘，再从文件喂给 `Set-Clipboard`。

> 从 Bash 里调 PowerShell 会被本机安全策略拦。用 PowerShell 工具。

## 沙箱实测的教训：GUI 开沙箱窗口不可靠

在用户真机上验证键盘类 op，**不要**用 `Win+R → 输 notepad → Enter` 这种 GUI
方式开沙箱 —— 实测 Enter 没生效，运行框一直开着，后面所有键入全部落进了用户
正开着的 Chrome / WorkBuddy。正确姿势：

1. **进程方式**启动：`subprocess.Popen(["notepad.exe"])`，不经过 GUI；
2. 每次按键前用 Win32 `GetForegroundWindow` 查**前台窗口标题**，不是沙箱就中止；
3. 关键结果**截图留证**，人（多模态）复核 —— 光看 op 返回 ok 不算数。
