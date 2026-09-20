# 后台进程与端口：三个反复踩的坑

## 1. Bash 工具里 `nohup ... &` 活不过这一次工具调用

起服务端的那次调用结束后进程就没了，下一条命令去连就是 ConnectionRefused。
要**跨调用常驻必须用 Bash 的 `run_in_background=true`**（变成一个后台 task）。

## 2. `timeout -s INT` 杀不掉受控端

`timeout -s INT 8 python -m remote` 之后进程仍在监听（实测活了 3 分钟）。
停受控端用：

```bash
netstat -ano | grep :8765        # 拿 PID
taskkill /F /PID <pid>           # 单斜杠
```

**`taskkill //F //PID`（双斜杠）在这个 Git Bash 里不做路径转换**，会报
「无效参数/选项 - '//F'」。

## 3. 残留进程会让冒烟「假通过」

曾经 8765 被一个 8 小时前遗留的 `python -m remote --no-peerjs` 占着，
于是「起服务 + ping」的冒烟测试其实是连到了那个旧服务 —— 新代码根本没被验到。

**起服务前先 `netstat` 确认端口是空的**；python 的 stdout 走管道时是块缓冲，
启动横幅迟迟不出现**不代表没起来**，用 `netstat` 确认更可靠。
