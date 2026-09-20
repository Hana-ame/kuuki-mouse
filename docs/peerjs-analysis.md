# PeerJS 传输：逻辑验证报告

> 2026-09-20。只有一台机器、一条网络，PeerJS 那条链路（公开 broker + WebRTC 打洞）
> 没法端到端实跑。这篇记录的是**不联网能做到的最强验证**：把整条链拆成两层，
> 下面一层用内存回环完整跑通，上面一层逐环节做静态契约核对，最后列出剩下必须
> 真机验证的三件事与验法。

## 1. 结论

写这篇的当天，0.peerjs.com 是可达的（`TCP 443` 通），所以**网络那一层也真的跑过了**
—— 本机起服务端与客户端，两端都连公开 broker，同机 WebRTC 互连。实测：

```
[已验证 · 回环]  信封协议 / 鉴权 / op 路由 / batch / 大响应分块重组 / 控制端的 peerjs 分支
[已验证 · 真机]  broker 注册 ~2s、ICE 握手 ~13s、ping/info/pos/batch 全通、
                 大截图 192457B 经真实 DataChannel 分块传输后**逐字节相同**
[部分验证]       fork 的 API 契约（符号、协程性、缓冲阈值）—— 静态核对
[未验证]         跨 NAT（本机只有一台）、弱网/移动网下的重连、远端真实屏幕与输入
```

"回环"那一行不是靠读代码下的结论：`test_remote.py` 里有 16 项 PeerJS 相关测试，用一个内存回环把
`PeerJsServer` 与 `PeerJsClient` 背靠背接起来，跑的是**和真机完全相同的代码路径**，
只是把 `DataConnection.send()` 换成了一次函数调用。连 `remote.ctl` 的 peerjs 分支都是
这么验的 —— ctl 自己不知道，它照常走 registry → 目标解析 → 分发 → 汇总 → 状态回填。

> **握手 13s 这个数字要记住**：它正是控制端给 peerjs 默认 30s 超时（而不是 ws 的 10s）
> 的原因 —— 按 10s 给的话，几乎每次都会在握手阶段把预算吃光。

## 2. 逐环节分析

| # | 环节 | 谁的代码 | 逻辑依据 | 证据 |
|---|---|---|---|---|
| 1 | 客户端构造 `Peer(id, PeerOptions(secure=True))` | fork | `secure` 存在；cloud broker 只收 wss | `test_peerjs_fork_api_contract` |
| 2 | `peer.start()` / `peer.connect()` / `conn.send()` 是协程 | fork | `iscoroutinefunction` 全为 True；我们全都 `await` | 同上 |
| 3 | 事件名 `PeerEventType.Open/Error/Connection`、`ConnectionEventType.Data/Open/Close` | fork | 枚举成员都在 | 同上 |
| 4 | 只能 JSON 序列化 → 图片走 base64 | fork | `send()` 里 Binary 分支整段被注释（dataconnection.py:269-283） | 静态核对；`_op_screen_screenshot` 返回 `image_b64` |
| 5 | 库内分块被注释 → 必须应用层分块 | fork | `_handleDataMessage` 里 `__peerData` 那段被注释（:194-196） | 我们实现了 `split_message` + `PeerJsChunker` |
| 6 | 单块 8KB 不会触发 fork 背压 | fork | `MAX_BUFFERED_AMOUNT = 8MB`，8KB×4 仍远小于 | 同上测试断言 |
| 7 | 请求信封 → `service.handle_request` | 我们 | 与 WS 服务端**同一个入口**（ws_server.py:276） | `test_peerjs_loopback_roundtrip_matches_direct_call` |
| 8 | 响应信封 → 客户端按 `id` 匹配 | 我们 | 分块重组后 `header` 保留 `id`/`ok` | `test_peerjs_split_message_threshold_and_unique_ids` |
| 9 | 大截图分块 → 重组 → 字节一致 | 我们 | 240×160 噪点 PNG（>60KB）真走分块 | `test_peerjs_screenshot_survives_chunking` |
| 10 | 分块不全 → 超时，不给半截答案 | 我们 | 吞掉 `end` 块后必须 `TimeoutError` | `test_peerjs_incomplete_chunks_time_out_instead_of_lying` |
| 11 | 鉴权四种组合 | 我们 | 无 token 直通 / 一致可用 / 没给或错 → unauthorized 且断连 | `test_peerjs_auth_matrix` |
| 12 | 错误带 code 回传 | 我们 | 未知 op → `unknown_op`，坏参数 → `bad_request` | `test_peerjs_reports_remote_error_not_silence` |
| 13 | batch 与 kuuki 老协议路由 | 我们 | batch 必须在**顶层**；老消息靠 `t/mouse/text/key` 识别 | `test_peerjs_batch_and_legacy_kuuki_route` |
| 14 | 控制端 peerjs 分支 | 我们 | ctl 走完整流程，`shot`/`watch` 真落盘 | `test_ctl_peerjs_branch_end_to_end` |
| 15 | ICE 本机候选过滤 | 我们 | 断开的虚拟网卡留 169.254，aioice 去 bind 会失败 | `remote/ice.py`（2026-09-19 现场） |

## 3. 这一轮改掉的问题

按"会不会真出事"排序。每条都有对应测试锁住。

| # | 问题 | 为什么会出事 | 修法 |
|---|---|---|---|
| 1 | 分块 id 用 `时间戳 + 3 位随机` | 同一毫秒内两组分块有 1/1000 撞号 → 两组的块混进一组 → "分块不完整，丢弃" → 那条请求干等到超时 | 模块级自增序号 + uuid 后缀（`_CHUNK_SEQ`）；测试连开 200 组验无重复 |
| 2 | `CHUNK_THRESHOLD = 60000` 定义了却从没用，实际拿 `chunk_size`(8192) 当阈值 | 10KB 的响应也要拆成两条，白白多两次往返；PeerJS 每次往返都很贵 | `split_message(payload, chunk_size, threshold)` 两个参数各司其职 |
| 3 | 一个响应的分块可能与另一个响应的交错 | 虽然按 `_id` 分组能重组，但一组的块横跨另一组，更容易撞上 `max_pending` 淘汰 | 每连接一把 `asyncio.Lock`，整组连着发（`_reply`） |
| 4 | `call()` 返回 `msg.get("result") or {}` | 0 / False / "" / [] 这类**合法**结果被吞成空 dict，而 WS 照实返回 → 三传输形状不一致 | 改成 `msg["result"] if "result" in msg else {}`（WS 端同步改） |
| 5 | `screenshot()` 只返回 bytes，元数据全丢 | 控制端的 peerjs meta 只有 `transport/bytes`，而 WS/gRPC 有 `format/width/height/duration_ms` | 客户端存 `last_capture`，ctl 拼出同形状 meta |
| 6 | `conn._kuuki_authed = True` 直接挂属性 | 对端对象若不允许（`__slots__`）会 AttributeError → 每次请求都 500 | `_mark_authed` / `_is_authed`，失败退化到按 `id()` 记账 |
| 7 | ctl 给 peerjs 的默认超时也是 10s | peerjs 一次调用含 broker 注册 + ICE 交换 + 打洞，10s 经常在握手阶段就吃光预算，表现为"每次都刚好超时" | `DEFAULT_TIMEOUT = {ws:10, grpc:10, peerjs:30}` |
| 8 | `machines add` 不校验 endpoint | 三种传输地址长得很不一样，拼错了当场不报，等到下发才抛看不懂的连接错误，在多机汇总里会被误判成"那台挂了" | `validate_endpoint()`，peerjs 必须是合法 peer id（不是 URL） |
| 9 | 客户端没有 batch 入口 | 协议里写了 `{"id":2,"batch":[...]}`，但 `call("batch", {...})` 会把 batch 塞进 args → `unknown_op` | 加 `batch()` 与 `top=`（batch 必须在信封顶层）；WS 客户端同步加 |

## 4. 回环验证是怎么搭的

```
PeerJsClient ──send──> [server_chunker] ──> PeerJsServer._handle
     ^                                              │
     └────── _to_client ← [client._chunker] ←─ _reply┘
```

一个 `_FakeConn` 实现 `send()` / `close()` / `peerId`，`on_data` 回调把消息交给对端。
于是下面这些和真机跑的是同一份代码：信封解析、鉴权、`handle_request`、分块与重组、
按 id 匹配响应、控制端的 `call_machine` / `capture_machine`。

`test_ctl_peerjs_branch_end_to_end` 更近一步：monkeypatch 掉 `PeerJsClient`，让 ctl
以为自己在连真 broker，实际连的是回环 —— `ping` / `info` / `shot` / `watch` 全走通，
registry 状态回填成 online。

## 5. 真机自检：`python -m remote.peerjs_selftest`

网络那一层靠逻辑推不出来，所以留了一个随时能复跑的入口。它在本机起服务端和客户端，
两端都连公开 broker，然后用**假屏幕 + 假输入**跑一遍（不会动你正在用的光标）：

```bash
python -m remote.peerjs_selftest              # 随机房间码
python -m remote.peerjs_selftest --room ABCDE # 指定房间码
python -m remote.peerjs_selftest --timeout 60 # 慢网络放宽握手预算
```

2026-09-20 的实际输出（本机同机两端，家庭宽带）：

```
[   0.1s] 服务端 peer_id=kuuki-mouse-ZZXYW
[   2.1s] start() 返回, 等 broker 确认注册 …
[   2.1s] 客户端连接中 (broker 注册 + ICE 打洞) …
[  15.3s] 连接建立
[  15.3s] ping   -> pong=True
[  15.3s] info   -> hostname=DESKTOP-LLULJ2Q python=3.10.7
[  15.3s] pos    -> {'ok': True, 'x': 400, 'y': 300}
[  15.4s] shot   -> 192457B 是PNG=True
[  15.4s] 大图   -> 服务端 192457B (超过阈值, 会走分块)
[  15.5s] 大图   -> 客户端 192457B 逐字节相同=True
[  15.5s] batch  -> [True, True]
[  16.2s] 关闭完成
EXIT=0
```

三个要点：**注册 2s**、**握手 13s**、**192KB 的图分块传回来逐字节相同**。最后一条同时
压住了"应用层分块"和"真实 DataChannel 的顺序/完整性"两件事 —— 分块要是少一块，客户端
会超时而不是拿到半截图（见回环里那条 `test_peerjs_incomplete_chunks_...`）。

想用**真**屏幕 + 真受控端走这条路，就按 runbook 那套来：

```bash
python -m remote                                  # 默认就开 PeerJS, 会打印房间码
python -m remote.ctl machines add me --transport peerjs --endpoint kuuki-mouse-XXXXX
python -m remote.ctl ping me                      # 握手是否在 30s 内完成
python -m remote.ctl shot shot.png -t me          # 大响应分块全到了吗
```

跨 NAT 要换两台机器再跑一遍 —— 同机 WebRTC 走 host 候选，跨网才真的需要 STUN 打洞。

## 6. fork 的已知缺陷（不是我们的，但影响判断）

- **`_tryBuffer()` 里 `self._trySend(msg)` 是协程却没 await**（dataconnection.py:328）。
  后果：一旦触发背压（或连接未 open），buffer 里的消息会被**当作发送成功丢掉**，还递归。
  缓解：单块 8KB 相对 8MB 的阈值极小，正常不会进这条路径；但别依赖 fork 的内部缓冲 ——
  send 之前必须确认 `conn.open`（我们两端都等了 `Open` 事件才发）。
- **二进制序列化整段被注释**：图片只能 base64，体积 +33%。
- **ICE 候选不做可用性过滤**：已由 `remote/ice.py` 打补丁（见那篇的现场记录）。

## 7. 相关文件

| 文件 | 作用 |
|---|---|
| `remote/peerjs_server.py` | 服务端：协议、鉴权、分块发送 |
| `remote/peerjs_client.py` | 客户端：连接、调用、分块重组 |
| `remote/ice.py` | ICE 本机候选过滤补丁 |
| `test_remote.py` 的 `PeerJS (回环验证)` 段 | 13 项回环 + 契约测试 |
| `docs/ctl-selfhost-runbook.md` | 控制端实战手册（WS/gRPC 本机自控实测） |
