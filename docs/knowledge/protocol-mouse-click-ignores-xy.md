# 【未修】`mouse.click` 静默忽略 `x` / `y`

**状态：待修。** `remote/service.py:313` 的 `_op_mouse_click` 只读取
`button` / `clicks` / `interval` / `hold`，**根本不读 `x` / `y`** —— 它永远在
「当前光标位置」点击。

```python
def _op_mouse_click(self, args: dict) -> dict:
    button = _as_str(args.get("button"), "button", "left")
    ...
    done = self.controller.click(button, clicks, interval, hold)   # 坐标去哪了？
```

## 为什么这条最坏

`mouse.scroll(x, y)` 是**支持**坐标的，所以客户端自然会把 click 也写成
`{"op":"mouse.click","args":{"x":840,"y":546}}` —— 服务端回 200 OK，什么都没动。

**静默忽略是最坏的行为**：它让成功与否取决于「光标恰好在哪」，于是同一个脚本
间歇性生效，且从不报错。真机调试时被它坑过整整一轮。

## 修法二选一

1. 收到 `x`/`y` 就先 move 再 click（与 scroll 的语义一致，最符合直觉）
2. 不打算支持就对多余坐标报 `bad_request`（让它当场炸，而不是静默）
