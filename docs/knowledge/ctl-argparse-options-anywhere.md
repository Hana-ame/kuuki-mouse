# argparse：让全局选项前后都能写

子 parser 与外层 parser 共享 namespace，**子 parser 的默认值会覆盖命令行已经给的值**。
于是 `python -m remote.ctl -t pc1 ping` 里 `-t` 在子命令之前时会被吞掉。

解法是各处都用 `default=argparse.SUPPRESS`（即「不出现在 namespace 里」），最后统一
`_fill_defaults()` 补齐。这样 argparse 不会用默认值去覆盖任何东西。

**撞名要单独处理**：`machines add` 有自己的 `--timeout`（这台机器的默认超时），
与全局 `--timeout`（本次调用超时）同名。那里只接 `--registry` / `--json`，
避免两个语义混成一个。

另一个历史遗留：`machines add` 的分组参数只有 `--group`，而动作命令有 `-g` ——
文档里顺手写成了 `-g`，于是两边都支持了（兼容性考虑，不是设计）。
