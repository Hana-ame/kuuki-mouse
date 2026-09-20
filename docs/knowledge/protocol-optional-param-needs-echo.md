# 跨传输等价性测不出来, 先看看参数有没有回进返回值

## 现象

补 `mouse.click` 的 `hold` 字段时 (proto 里原先没有, WS/PeerJS 支持、gRPC 不支持),
在 `test_new_ops_agree_across_transports` 的用例表里加了
`("mouse.click", {"hold": 0})`, 验证用例有效性的方式是**把 gRPC 侧的传参删掉看用例
红不红**:

- 删掉 `hold` → 用例立刻红: `{'hold': 0.0} != {'hold': 0.06}` ✅
- 把 `interval` 的 `HasField` 判断改成 `or 0.05` (即"显式 0 被当成没给") →
  **用例照样绿** ❌

同一个测试、同一套比对逻辑, `hold` 抓得住、`interval` 抓不住。

## 为什么

这条测试的判据是「返回值 + 副作用」。`mouse.click` 的返回值当时是
`{"button", "clicks", "hold", "positioned_at?"}` —— **`interval` 根本不在里面**。
假鼠标也不记录它。于是 interval 被悄悄换成默认值这件事, 在测试的可见范围内
不产生任何差异:

```python
result = {"button": button, "clicks": done, "hold": hold}   # 没有 interval
```

**返回值里没有的参数等于没有判据** —— 参数是"怎么执行的"的一部分, 不回显出来,
两条传输的行为差异就没有观测点。

## 修法

```python
result = {"button": button, "clicks": done, "hold": hold, "interval": interval}
```

改完再跑一次变异测试 (把 `HasField` 换成 `or 默认`), 用例红了 —— 判据成立。

## 教训

- **加参数时顺手把它回进返回值**: 成本一行, 收益是"这条参数从此可被跨传输比对"。
  尤其是那些 `0` 是合法值的参数 (`interval` / `hold` / `duration`), 它们最容易被
  `x or default` 静默改写。
- **用例写完要做变异测试**: 故意改坏实现, 看用例红不红。**不红的判据等于没有
  判据**, 而且比没有用例更危险 —— 它给人一种"这里测过了"的错觉。
  本轮要不是顺手验一下, `interval` 这条会一直绿着。
- 这条与 `protocol-zero-is-valid-value.md` 是一体两面: 那边说"`0` 是合法值别用
  `or default`", 这边说"用了也未必测得出来"。
