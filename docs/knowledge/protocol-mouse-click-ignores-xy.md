# 【已修】`mouse.click` 静默忽略 `x` / `y`

**状态：已修复（2026-09-20）。** 修复前 `remote/service.py` 的 `_op_mouse_click`
只读 `button` / `clicks` / `interval` / `hold`，**根本不读 `x` / `y`** —— 永远在
「当前光标位置」点击，服务端还回 200 OK。

```python
# 修复前: 坐标去哪了？
done = self.controller.click(button, clicks, interval, hold)
```

## 为什么这条最坏

`mouse.scroll(x, y)` 是**支持**坐标的，所以客户端自然会把 click 也写成
`{"op":"mouse.click","args":{"x":840,"y":546}}` —— 服务端回 200 OK，什么都没动。

**静默忽略是最坏的行为**：它让成功与否取决于「光标恰好在哪」，于是同一个脚本
间歇性生效，且从不报错。真机调试时被它坑过整整一轮。

## 修复后的行为

- 给了 `x` / `y`：**先移动再点**（与 `mouse.scroll` 的坐标语义一致）；只给一个时
  另一个保持当前坐标。
- 返回值多一个 `positioned_at: {"x":..,"y":..}`，控制端能核对"到底点没点到位"。
- 没给坐标：保持原行为（点当前位置），返回值里**没有** `positioned_at` —— 不伪造。

三处同步改动（改协议记得三传输等价）：

| 层 | 改动 |
|---|---|
| `remote/input.py` `click()` | 新增 `x` / `y` / `move_duration` 参数，先 `move_smooth` 再按 |
| proto `ClickMouseRequest` | 新增 `optional int32 at_x / at_y`（必须 optional） |
| `grpc_server.py` / `client.py` | `HasField("at_x")` 后透传，`_optional_at()` 只在显式给坐标时写入 |

**为什么字段必须 optional**（详见 `protocol-proto3-optional.md`）：直接
`at_x=args.get("x", 0)` 会把「没给坐标」变成「定位到 x=0」—— 点在屏幕左上角。
显式 0 又是合法坐标，所以只能用 optional + `HasField`。

## 教训

以后新增 op 参数，要么实现、要么当场 `bad_request`，不要收下不理。
