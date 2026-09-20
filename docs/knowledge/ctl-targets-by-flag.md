# ctl 的目标只能走 `-t/-g/-a`，不能用位置参数

`remote.ctl` 里 `targets`（`nargs="*"`）与「op 名 / check 键名 / shot 路径 /
watch 目录」同时存在时，**argparse 的贪心匹配会把后面的全吃掉**：

```bash
# 意图：给 pc1 发 op=mouse.position；实际：op 名被当成第 2 个目标
python -m remote.ctl op mouse.position pc1

# 正确
python -m remote.ctl op mouse.position -t pc1
```

所以 `op` / `check` / `shot` / `watch` / `tail` 这几个命令**不收位置目标**，只认
`-t/--to` / `-g/--group` / `-a/--all`。有测试盯着这条：
`test_ctl_op_and_check_take_targets_by_flag`。

这是 `remote.ctl`（多机编排级）与 `remote.client`（单机调试级）的分水岭之一 ——
client 是「一条命令打一台一个 op，地址临时写在命令行上，不落盘」；ctl 是
「registry 起别名 → 按别名/组/全体分发 → 并发 + 逐台超时 → 结果汇总 → 回填状态」。
