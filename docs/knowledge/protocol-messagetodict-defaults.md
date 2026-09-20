# `MessageToDict` 会省略取默认值的字段

proto3 标量没有存在性，序列化时**取默认值的字段会被直接省略**。于是

```python
{"x": 1396, "y": 0}   # y 恰好取默认值 →
{"x": 1396}           # 到了对端只剩 x
```

控制端写 `pos["y"]` 当场 `KeyError`。而且它只在光标**恰好在某条边缘**时才复现，
是最难查的那种 bug —— 平时一次不出。

解法：`MessageToDict(..., always_print_fields_with_no_presence=True)`。
