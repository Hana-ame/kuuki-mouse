# 截屏只有一个后端 PIL.ImageGrab

**没有降级链。** 曾经的 `mss → Pillow → ffmpeg x11grab` 三级降级、失败拉黑、
屏幕尺寸四套探测**都已删除**。写文档或改代码时不要再提「多重后端 / 降级」。

- 屏幕尺寸直接取截图的 `.size`，不做额外探测
- `info` 里**没有** `capture_backends` 字段
- WS / gRPC 的截图响应里带 `"backend"` 字段

## `backend` 的真值只能有一份

曾经 `backend` 只在 `ws_server.py` 里硬编码成 `"pillow"`，而 `Capture` 对象上反而
没有这个字段 —— 于是 `wincheck.py` 读 `capture.backend` 直接
`AttributeError: 'Capture' object has no attribute 'backend'`，一跑就崩。

修法：字段挪到 `Capture`（默认 `"pillow"`），协议头改成读 `capture.backend`。

> 这个崩一直没被发现，因为**没有任何测试会执行 `wincheck.py`**。仓库里不能被
> 测试覆盖到的脚本，出错就只有人肉踩到才知道。
