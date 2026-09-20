# proto3 普通标量分不清「显式给 0」与「缺省」

proto3 的普通标量字段**没有存在性**：publisher 显式写的 `0` 和根本没写，到达服务端
长得一模一样。于是服务端写成

```python
if request.interval:   # 显式 0 会走到 else，等价于「用户没给」
    ...
```

结果是 WS 传 `interval=0`、gRPC 用默认值 `0.05` —— 又一处跨传输不等价。

**需要区分「给了」和「没给」的字段必须声明 `optional`**，判断用 `HasField()`：

```protobuf
optional double interval = 3;
optional double duration = 4;
optional int32 at_x = 5;
optional int32 at_y = 6;
```

已经这么处理的：`ScrollRequest.interval`、`DragRequest.duration`、
`ScrollRequest.at_x/at_y`。

> **仍未修**：`ClickMouseRequest.interval` 是普通标量，同类问题还在。
