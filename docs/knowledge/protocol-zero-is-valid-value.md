# 「0 是合法值」的字段不能用 `x or default`

这是本项目里踩过两次的错误写法：

```python
duration = args.get("duration") or 0.3   # 错：duration=0（瞬移）被换成 0.3
interval = args.get("interval") or 0.05  # 错：interval=0（全速）被换成 0.05
```

后果属于最坏的一类：**两条传输行为不一致**。当时 `duration: 0` 被服务端吞掉，
于是 gRPC 走插值轨迹、WS 走瞬移 —— 参数完全相同，鼠标动的路径不同，且毫无报错。

统一用带默认值的取值助手（`remote/service.py` 里的 `_as_int` / `_as_float` /
`_as_str`），它们区分「没给」和「给了 0」。

同理的另一处：`args.get("x") or <当前坐标>` 这类写法会把 `x=0`（屏幕左边缘）
当成没给。
