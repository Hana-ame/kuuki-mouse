# 文档里的测试数量，是「文档有没有跟着代码走」的指示剂

四处文档一直写着「84 项」，实测是 90，另一处还写着 "70 passed / 1 skipped"。

**数字本身不重要，但它是最好的指示剂** —— 没人会主动去核对这个数字，如果它是对的，
说明有人认真同步过；如果它是错的，整套文档大概率都在自说自话。

加/减测试之后，顺手更新这四处（README / remote/README / docs/puppet-multi-machine.md /
docs/ctl-selfhost-runbook.md），改完跑 `check_ctl_docs.py`。

> `ctl-selfhost-runbook` 里 "26 -> 57 项" 那处是**历史演进记录**，不要动 ——
> 「当前值」和「曾经的值」要看得出来区别。
