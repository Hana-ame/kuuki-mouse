# 本机特有的两个输入坑 (逐字输入被吞 / alt+space 被抢)

2026-09-20 在画图上做坐标校准验收时连撞两次, 都表现为"命令发了、看起来也成功了,
但目标应用毫无反应"。记下来是因为排查路径很长 —— 症状看着像传输或 op 坏了。

## 逐字 `keyboard.type` 会被吞, `keyboard.paste` 不会

现象: `win+r` → `keyboard.type "mspaint"` → 运行对话框里常常一个字都没有, 或者
只到一半。开始菜单的输入框同理。**对照实验**: 同一套代码打 `notepad` 也时灵时不灵,
所以不是画图的问题。

猜测是逐字符注入 (`pynput` 的 `type` 逐个 press/release) 与输入框本身的 IME /
自动补全抢焦点: 单个字符之间隔的那点时间里, 输入框可能还没准备好接收。

**绕过去**: 用 `keyboard.paste` (写剪贴板 + Ctrl+V) —— 一次性送入, 实测多次都稳定。
这条适用于所有"在某处输入一串东西"的场景, 不只是打开应用:

```bash
python -m remote.client ws op keyboard.paste --args '{"text":"mspaint"}'
```

注意这条不是 `remote` 的 bug, 是本机环境 + 目标输入框的组合效果。仓库里已经有
`gui-chinese-input-via-clipboard.md` 讲"中文必须用 paste", 这里是同一招在非 ASCII
场景之外也成立。

## `alt+space` 已经被 PowerToys 的 PowerLauncher 占了

现象: 想用系统菜单最大化窗口 (`alt+space` → `x`), 结果弹出来的是 PowerToys 的
搜索框, 后续按键全进了搜索框。窗口状态纹丝不动。

**绕过去**: 用 `win+up` 最大化。而且**任何窗口操作之后都要确认**, 不能假设生效:

```bash
python -m remote.client ws focus --process mspaint       # 看返回的 focused
python -m remote.client ws op window.foreground --args '{}'   # 前台到底是谁
```

必要时先按一次 `esc` 把可能存在的弹出层关掉。这条与
`gui-window-focus-gap.md` 是同一个道理: **操作之后确认, 而不是相信调用成功**。

## 排查这类问题的顺序

1. `window.foreground` 看前台是谁 —— 一半的"没反应"是前台不是你以为的那个;
2. `window.list --process xxx` 看目标进程到底起没起来 —— 起了但没到前台 vs 压根没起,
   是完全不同的两回事;
3. 抓帧对比 —— 别猜, 看屏幕。
