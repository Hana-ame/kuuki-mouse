# 组合键要在客户端拆，不能在服务端拆

`keys: "ctrl+shift+s"` 到 gRPC 时被包成**单元素列表**，服务端按单个键名查表，
报「未知键名 'ctrl+shift+s'」。WS 侧一直是拆好的 `["ctrl","shift","s"]`，所以同一个
op 在两条传输上一个能用一个不能。

根因：proto 里该字段是 `repeated string`，但客户端塞进去的是一整个 `"+"` 连接串。
**拆分只能在客户端做**（proto 形状自己不会拆），`remote/client.py` 里负责。

> 这是既有 bug，`keyboard.hotkey` 一直中招，不是某次改动引入的 —— 说明
> 「挑一两条命令手动试过」不等于「这条 op 真的全链路可用」。
