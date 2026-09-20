# 多步滚动要保持总量守恒

`mouse.scroll` 支持 `steps` / `interval` 把一次滚动拆成多格发送（某些应用只认
小步长）。

**错的做法**：每步滚 `dx // steps`。丢掉整除余数 —— 滚 3 格分 5 步，每步 0 格，
**一格都不滚**。

**对的做法**：累计目标值取整后取差值。

```python
total = dx
sent = 0
for i in range(1, steps + 1):
    target = round(total * i / steps)   # 累计目标
    step   = target - sent              # 与已发量的差
    if step:
        scroll(step)
        sent += step
```

总滚量与 `steps` 无关，"拆细"纯属视觉手感。
