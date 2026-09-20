# 同一份数据的 JSON 通道与二进制帧头必须字段一致

**现象（2026-09-20 修掉）**：同一帧截屏，走 `screen.screenshot`（JSON op）拿不到
`backend` 字段，走 WS 二进制帧头却能拿到 —— 因为 `Capture.to_dict()` 漏了它，
而 `ws_server` 的帧头是单独读 `capture.backend` 拼的。

**为什么危险**：两个出口各拼各的字典，字段集会悄悄分叉。控制端如果按「帧头有
的 JSON 也有」写代码，走到另一条通道就 KeyError / None；测试抓不到，因为每条
通道各有各的测试。

**修法**：`to_dict()` 补上 `"backend": self.backend`。

**怎么避开**：一份数据有多个序列化出口时，字段清单只能有一份真值 —— 要么都从
同一个 `to_dict()` 出，要么写一条「两通道逐字段相等」的对比测试
（参考 `test_remote.py::test_new_ops_agree_across_transports` 的做法）。
