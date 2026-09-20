# 三种传输「点名即选择」，默认只开 PeerJS

以前默认 WS + gRPC + PeerJS 全开，用 `--no-xxx` 关；现在反过来：

```bash
python -m remote                      # 默认只开 PeerJS
python -m remote --ws                 # 只开 WebSocket 8765
python -m remote --ws --grpc --peerjs # 三个全开
```

**语义 = 点名即选择**：给了任何 `--ws/--grpc/--peerjs` 就以给的那几个为准；一个都没给
才用默认。`--no-xxx` 保留，是在这个结果上再减。

## 为什么不用 additive

additive 下 `--ws` 会等于「PeerJS + WS」。想只要本地端口的人会**白白注册一条到
公开 broker 的出站通道**，而这条通道跟本机绑 `127.0.0.1` 还是 `0.0.0.0` 完全无关
—— 多暴露一条通道不该是默认的。

## 改这块时连带要改的地方

CI 两处启动命令、`start-win.bat` 用法、`packaging/agent-README.txt`（打进产物 zip，
是给用户看的）、`packaging/agent_entry.py` 注释、`remote/__init__.py` 包文档、
四处 md。**以及 `wincheck.py` 这类「连别人的工具脚本」** —— 它们假设了旧默认，
改完语义要单独复查。
