# 文档里的静态数字，是「文档有没有跟着代码走」的指示剂

四处文档一直写着「84 项」，实测是 90，另一处还写着 "70 passed / 1 skipped"。

**数字本身不重要，但它是最好的指示剂** —— 没人会主动去核对这个数字，如果它是对的，
说明有人认真同步过；如果它是错的，整套文档大概率都在自说自话。

2026-09-20 又验证了一次：加了 `remote/vision.py` 与 `locate` 之后，实测已是
**126 passed / 1 skipped**，而四处文档还停在 113；同一轮里还发现 `info` 的
capabilities 从 43 涨到了 46、`docs/knowledge/` 从 54 条涨到了 59 条 —— 三类数字
一起漂。**只要有一类数字在动，就说明其它几类大概率也过期了。**

## 加/减测试之后要动的四处

| 位置 | 写的是什么 |
|---|---|
| `README.md` 文件清单 | `test_remote.py` 的项数 + 覆盖范围 |
| `remote/README.md` 第 10 节 | `pytest -v` 的期望输出 + 分类项数 |
| `docs/puppet-multi-machine.md` | 第 8/9 节里的总数与「其中 N 项测它」 |
| `docs/ctl-selfhost-runbook.md` | 测试基线 / 具体情况表 / Step 7 期望输出 |

另外三处顺手也要看一眼：`docs/peerjs-analysis.md` 的回环项数、`docs/ctl-selfhost-runbook.md`
里 `info` 的 capabilities 条数、`README.md` 里 `docs/knowledge/` 的条数。

改完跑 `python check_ctl_docs.py` —— 它管的是**命令**，数字它管不到。

## 别动的两类

- **历史演进记录**：`remote/README.md` 的 "32 → 57 项"、`ctl-selfhost-runbook` 的
  "26 -> 57 项" 是当时发生了什么的记载。「当前值」和「曾经的值」要看得出来区别。
- **故意留着的乱码**：`浣犲ソkuuki` 是 GBK 乱码的实拍样本，看起来像文件坏了，其实是
  反面例子。见 `gui-chinese-input-via-clipboard.md`。

## 怎么一次性核出来

```bash
python -m pytest test_remote.py -q                     # 总数
python -m pytest test_remote.py --collect-only -q | grep "::" | grep -c ctl
python -m pytest test_remote.py --collect-only -q | grep "::" | grep -ci peerjs
```

注意 **项数有两种口径**：95 个测试函数，**参数化展开后是 127 条用例**
（`test_ctl_translates_cli_to_ops` 一项就展开成 16 条）。文档里写的是 pytest 报告的
条数（126 passed / 1 skipped），别混着写，否则同一份文档里会出现两个对不上的数字。
