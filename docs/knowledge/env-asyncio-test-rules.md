# 测试里的 asyncio 规则

## WS 服务端要在同一个协程内跑完

```python
asyncio.run(server.start())     # 返回后事件循环关闭、socket 也被收掉
server._server.sockets[0].getsockname()   # WinError 10038 非套接字
```

端到端测试要**在 `asyncio.run()` 内部起服务、连、断言、关**，别跨 `asyncio.run`。

## 不能嵌套 `asyncio.run`

`ctl.main()` 内部自己调 `asyncio.run()`。所以端到端测试**直接调 `dispatch` /
`call_machine` 这些内部函数，不要调 `main()`** —— 嵌套 `asyncio.run` 会
`RuntimeError: asyncio.run() cannot be called from a running event loop`。

这条也决定了 `DummySwarm` 必须跑在独立线程（`serve_in_thread()`）。
