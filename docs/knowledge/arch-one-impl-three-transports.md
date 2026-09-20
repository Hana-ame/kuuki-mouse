# 一份实现、三种传输

`ws_server` / `grpc_server` / `peerjs_server` **都只是翻译层**，真正的动作只有
`RemoteService.handle(op, args)` 一份。这是整个扩展的地基。

## 加新 op 时必须两侧都跟

- WS 走 args dict 直接透传
- gRPC 走「op → RPC 名」映射表 + proto 里的字段
- PeerJS 复用同一份 `handle()`

漏了某一边不会报错，只会「这条 op 在这条传输上不存在」。

## 三传输必须等价

同一条 op 经三条路下发，返回值与副作用必须一致。靠
`test_remote.py::test_new_ops_agree_across_transports` 逐条比对 —— 它是刻意写成
「跨传输比对」而不是「各自单测」的，历史上抓出来的偏差全是这种比对才暴露的：
gRPC 组合键不拆、`duration: 0` 被吞、`MessageToDict` 丢字段。详见对应知识点。
