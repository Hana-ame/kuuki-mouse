# WS 二进制帧头漏了 source_width, 缩放系数算出来永远是 1.0

## 现象

`locate --describe` 输出 `scale = 1.0`。1680 宽的屏幕按 `--max-width 1000`
截回来的帧, 系数应该是 1.68 —— 拿 1.0 去点鼠标, 全部点偏左上 (1000/1680 = 60%
的位置被当成 100%)。

## 为什么

同一个 `Capture` 有两条出口, 字段却不对齐:

- JSON 通道 (`Capture.to_dict()`): 有 `source_width` / `source_height` / `scale`;
- WS 二进制帧 (`ws_server.py` 的 screenshot 分支): 只放了 `width` / `height`,
  没有 source_*。

而 gRPC 的 Image message 和 PeerJS 的 JSON op 返回都带 source_width。于是只有
WS 二进制帧这条最常用的路算不出缩放系数 —— `vision.scale_of()` 拿不到
source_width 只能返回 1.0, 错误被静默放大成一整屏的坐标偏移。

这和上一轮修的 `Capture.to_dict() 漏 backend` 是同一类病: **两条通道一个字段
改了另一个忘了** (见 `protocol-json-binary-field-parity.md`)。

## 规避

1. 给 Capture 加字段时, 三个出口都要跟: `to_dict()` (JSON/PeerJS)、
   ws_server 的二进制帧头、proto 的 Image message (要动 proto 就得重新生成存根)。
2. 帧头里直接放 `scale` 冗余一份, 调用方不用自己除一遍。
3. 消费侧 (vision.scale_of) 拿不到 source_width 时返回 1.0 并让上层拒绝换算
   (`_run_peerjs` 对没有 source_width 的帧直接报错), 宁可不给也不给错坐标。

## 顺带

`--region` 截图时 source_width 是**整屏**宽度 (1680), 而图片宽度是裁剪后的
宽度 —— 这时 width/source_width 不是缩放关系, scale_of 会返回 1.0。这是对的:
region 模式下图片坐标本来就是真实屏幕坐标, 不需要再乘系数。
