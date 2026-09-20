# gRPC 无返回值的 RPC 用 `Ack{ok, message}` 兜底

proto 里没有返回值可给时，统一回 `Ack{ok, message}`，**`message` 是 JSON 字符串**；
而 WS / PeerJS 直接返回对象。

两条传输形状不一致，控制端（要横向比较多台机器的结果）没法统一处理。解法在
`remote/client.py::_unwrap_ack`：把它拆回 dict。

**拆包条件要保守**：只在 dict 恰好只有 `{ok, message}` 两个键、且 `message` 能解析
成 dict 时才拆；拆不动就原样返回。不要无条件 `json.loads(message)` —— 普通消息的
`message` 可能本来就是一句纯文本。
