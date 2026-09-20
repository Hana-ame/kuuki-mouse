# `GrpcClient` 是同步的，并发分发要 `asyncio.to_thread`

多机分发是并发的。如果同步的 gRPC 调用直接堵在事件循环里，会连累同批次的 WS /
PeerJS 请求 —— 它们本该并行，却被迫排队。

```python
result = await asyncio.to_thread(grpc_client.call, op, args)
```

**这条只在并发场景需要**：单发一条无所谓，问题是「广播给 50 台」时第 1 台慢会拖住
其余 49 台，而且看不出来是被拖住的（超时看起来像网络问题）。
