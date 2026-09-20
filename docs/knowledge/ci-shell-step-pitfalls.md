# CI shell 步骤：`set -uo pipefail` 缺了 `-e` 等于没有断言

```bash
set -uo pipefail      # 缺 -e
```

缺 `-e` 时，中间某条命令失败（比如 grep 没匹配到）**不会中断**步骤，
最终结果只看最后一条命令的退出码 —— 于是**整层断言形同虚设**。端到端步骤曾经就是这样：
三步检查全不成立，步骤照样是绿的。

一律写 `set -euo pipefail`。

## Release 不幂等：重跑会失败

重跑同一个 run 时 release 已存在，`gh release create` 直接失败。改成：

```bash
if gh release view "$TAG" >/dev/null 2>&1; then
  gh release upload "$TAG" <files> --clobber
else
  gh release create "$TAG" <files>
fi
```

能被重复执行的步骤，就要让它敢被重复执行。

## 已知无害的噪音

Actions 日志里的 "Node.js 20 is deprecated"（checkout@v4 / setup-python@v5 /
upload-artifact@v4 被强制跑在 Node 24 上）—— 纯告警，不影响结果。**别为了消掉一行
警告去追新版本**，容易把绿灯折腾红。
