# 中文输入走剪贴板 + Ctrl+V，不要用逐字注入

`keyboard.type` 注入中文不可靠：返回 `chars: 61` 看着成功，但文字根本没进输入框
（pynput 的 Unicode 注入方式与某些页面的输入处理冲突）。

**可靠做法**：写入剪贴板 → 粘贴。

```
点输入框 → ctrl+a → delete（清掉上一次的残留）→ ctrl+v → enter
```

## 剪贴板写入的坑：Git Bash 的 `printf | clip` 会乱码

```bash
printf '你好' | clip        # 错：UTF-8 字节被 clip.exe 按 GBK 解释 → 粘出来全是乱码
```

正确方式（任选）：

```powershell
Set-Clipboard -Value (Get-Content -Raw -Encoding UTF8 msg.txt)   # PowerShell 工具里
```

或者先把文本以 UTF-8 写盘，再从文件喂给 `Set-Clipboard`。

> 从 Bash 里调 PowerShell 会被本机安全策略拦。用 PowerShell 工具。
