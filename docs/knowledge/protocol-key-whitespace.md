# 空格 / 制表符是合法键名，不能先 strip 再判空

**现象（2026-09-20 修掉）**：控制端发 `{"op":"keyboard.key","args":{"key":" "}}`
想敲一个空格，服务端回 `bad_request: 键名不能为空`。

**为什么**：`resolve_key()` 的第一行是 `raw = str(name).strip()` —— 空格被 strip
成空串，然后被当成「没给键名」误拒。空格、制表符、换行**本身就是有意义的键**
（`Key.space` / `Key.tab` / `Key.enter`），不能套用"去空白"的文本处理直觉。

**修法**：在 strip **之前**识别纯空白字符串：

```python
if raw.strip():
    key = raw.strip().lower()
else:
    key = {" ": "space", "\t": "tab", "\n": "enter", "\r": "enter"}.get(raw, "")
if not key:
    raise ValueError("键名不能为空")
```

**怎么避开**：写"把字符串归一成键名"这类函数时，先问一句「这段输入里有没有
本身就是合法值的空白？」—— `strip()` 是文本直觉，不是按键语义。相关坑：
「0 是合法值」不能用 `x or default`（见 `protocol-proto3-optional.md`），
同一类错误。
