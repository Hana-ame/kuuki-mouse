# `result or {}` 会吞掉合法的 falsy 返回值

```python
return {"result": payload or {}}   # payload 是 0 / False / "" / [] 时全变成 {}
```

`False` 是最痛的一个：op 明确返回「操作失败 / 不支持」，到调用方手里变成一个空 dict，
看上去跟「没有返回值」一模一样。

凡是「返回值可能合法为假值」的地方，都用显式判断：

```python
result = payload if payload is not None else {}
```

同理，`None` 的语义要和「未提供」区分开 —— 服务端的 op 返回值不要用 `None`
表示成功。
