"""PeerJS 传输: 把 ``RemoteService`` 挂到 PeerJS 数据通道上。

和 ``ws_server`` / ``grpc_server`` 并列的第三种传输, 语义完全一致 —— 都调
``RemoteService.handle(op, args)``, 所以客户端换传输不用换脑子。

为什么需要它
------------
kuuki-mouse 本来就用 PeerJS(公开 cloud broker + 房间码 + 手机扫码), 所以这条路
不需要域名、证书、端口转发, 跨网络也能用 —— 对"从手机/异地控制这台电脑"最省事。

与 WS 版的三个关键差别 (都是 Python fork 的限制, 见 README)
----------------------------------------------------------
1. **只能 JSON 序列化**: fork 里 ``SerializationType.Binary`` 的收发代码被注释掉了,
   所以图片必须走 base64 (会比 WS 的二进制帧大约 +33%)。
2. **库内分块也是注释掉的**: ``DataConnection.handleMessage`` 里处理 ``__peerData``
   的那段被注释, 所以**大消息要自己在协议层分块**。这里用 appl 级分块:
   发送侧把 base64 切片成 ~8KB 的块, 接收侧按 ``_id`` 重组。
3. **PeerJS 的 id 由 broker 分配/占用**: 主机端用固定的
   ``kuuki-mouse-<房间码>`` 注册, 客户端用随机 id 连过来。

协议 (与 WS 版同构, 只多一个分块层)
----------------------------------
请求::

    {"id": 1, "op": "mouse.move", "args": {"x": 100, "y": 200}}
    {"id": 2, "batch": [{"op": "ping"}, {"op": "mouse.position"}]}

响应::

    {"id": 1, "ok": true,  "result": {...}}
    {"id": 1, "ok": false, "error": {"code": "bad_request", "message": "..."}}

大响应 (含 base64 图片) 会先发一条头, 再发若干数据块, 最后一条尾::

    {"_chunk": "head", "_id": "c1", "total": 3, "size": 21430, "header": {原始响应骨架}}
    {"_chunk": "data", "_id": "c1", "n": 0, "d": "<base64 片段>"}
    {"_chunk": "end",  "_id": "c1"}

带 ``_chunk`` 的消息不和普通消息撞车 (普通消息只有 id/op/args/ok/result/error)。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import random
import uuid
from typing import Any, Dict, List, Optional

from .service import RemoteError, RemoteService

log = logging.getLogger("kuuki.remote.peerjs")

__all__ = ["PeerJsServer", "PeerJsChunker", "gen_room_code"]

PEER_PREFIX = "kuuki-mouse"

#: 单个分块的大小 (base64 字符数)。取 8KB 远小于 DataChannel 的 ~16KB 上限,
#: 给 JSON 包装和 SCTP 头留足余量; 太大容易被 SCTP 分片或直接丢弃。
CHUNK_SIZE = 8192

#: 超过这个长度的响应才分块, 避免小响应被无谓地拆成三条消息。
CHUNK_THRESHOLD = 60000

ROOM_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # 去掉易混淆字符, 与 main.py 同款
ROOM_LEN = 5


def gen_room_code() -> str:
    return "".join(random.choice(ROOM_ALPHABET) for _ in range(ROOM_LEN))


class PeerJsChunker:
    """接收侧的分块重组器 (每个连接一个实例)。

    发送侧是纯函数式的 (见 ``split_message``), 接收侧必须有状态 —— 这就是它。
    """

    def __init__(self, max_pending: int = 4):
        self._pending: Dict[str, dict] = {}
        self._max_pending = max_pending

    def feed(self, msg: dict) -> Optional[dict]:
        """喂一条消息; 完整了返回原始响应, 否则返回 None。"""
        kind = msg.get("_chunk")
        if kind is None:
            return msg

        cid = str(msg.get("_id") or "")
        if not cid:
            return None

        if kind == "head":
            # 防御: 别让对端用无限多的 chunk id 撑爆内存
            if len(self._pending) >= self._max_pending:
                oldest = next(iter(self._pending))
                self._pending.pop(oldest, None)
                log.warning("分块缓存已满, 丢弃最旧的一组 %s", oldest)
            self._pending[cid] = {
                "total": int(msg.get("total") or 0),
                "parts": {},
                "header": msg.get("header") or {},
            }
            return None

        if kind == "data":
            info = self._pending.get(cid)
            if info is None:
                return None  # head 没到或已被挤掉
            info["parts"][int(msg.get("n") or 0)] = msg.get("d") or ""
            return None

        if kind == "end":
            info = self._pending.pop(cid, None)
            if info is None:
                return None
            total = info["total"]
            if len(info["parts"]) != total:
                log.warning(
                    "分块不完整 (%s/%s), 丢弃", len(info["parts"]), total
                )
                return None
            body = "".join(info["parts"].get(i, "") for i in range(total))
            out = dict(info["header"])
            try:
                out["result"] = json.loads(body)
            except json.JSONDecodeError as exc:
                out["ok"] = False
                out["error"] = {
                    "code": "internal",
                    "message": f"分块重组后 JSON 解析失败: {exc}",
                }
            return out

        return None


#: 分块 id 的自增序号。为什么不用时间戳+随机数: 同一毫秒内连开两组分块时,
#: 3 位随机数只有 1/1000 的区分度, 撞了接收端会把两组块混进同一组 —— 表现为
#: "分块不完整, 丢弃", 然后那条请求一直等到超时。自增序号 + uuid 后缀彻底消除。
_CHUNK_SEQ = itertools.count(1)


def split_message(
    payload: dict,
    chunk_size: int = CHUNK_SIZE,
    threshold: int = CHUNK_THRESHOLD,
) -> List[dict]:
    """把一条大响应切成 head/data.../end 三条以上。

    只对 ``result`` 部分分块 —— 头里保留 id/ok, 这样重组端不用猜结构。

    ``threshold`` 与 ``chunk_size`` 是两件事: 前者决定"要不要拆", 后者决定"拆多大"。
    只拿 chunk_size 当阈值的话, 10KB 的响应也要拆成两条, 白白多两次往返。
    """
    body = json.dumps(payload.get("result"), ensure_ascii=False, separators=(",", ":"))
    # 整体不超阈值就整条发; 单块大小仍按 chunk_size 切 (受 DataChannel 单消息上限约束)
    if len(body) <= threshold:
        return [payload]

    cid = f"c{next(_CHUNK_SEQ)}-{uuid.uuid4().hex[:8]}"
    header = {k: v for k, v in payload.items() if k != "result"}
    pieces = [body[i : i + chunk_size] for i in range(0, len(body), chunk_size)]
    out: List[dict] = [
        {"_chunk": "head", "_id": cid, "total": len(pieces), "size": len(body), "header": header}
    ]
    for index, piece in enumerate(pieces):
        out.append({"_chunk": "data", "_id": cid, "n": index, "d": piece})
    out.append({"_chunk": "end", "_id": cid})
    return out


#: 退化路径: 连接对象不允许挂属性时的已鉴权集合 (见 _mark_authed)
_AUTHORIZED_FALLBACK: set = set()


def _mark_authed(conn: Any) -> None:
    """给连接打上"已鉴权"标记。"""
    try:
        conn._kuuki_authed = True
        return
    except AttributeError:
        pass
    # 退化: 对端对象有 __slots__ 之类限制时, 用 id 记账。进程内 id 可能复用,
    # 但这条路只在异常对象上才走, 且最坏结果是"重新鉴权一次", 不会放过未授权连接。
    _AUTHORIZED_FALLBACK.add(id(conn))


def _is_authed(conn: Any) -> bool:
    try:
        if getattr(conn, "_kuuki_authed", False):
            return True
    except Exception:  # noqa: BLE001
        pass
    return id(conn) in _AUTHORIZED_FALLBACK


class PeerJsServer:
    """把 ``RemoteService`` 挂到 PeerJS 主机 id 上, 等客户端连进来。"""

    def __init__(
        self,
        service: RemoteService,
        room: Optional[str] = None,
        token: Optional[str] = None,
        peer_prefix: str = PEER_PREFIX,
        secure: bool = True,
        chunk_size: int = CHUNK_SIZE,
    ):
        self.service = service
        self.room = room or gen_room_code()
        self.token = token
        self.peer_prefix = peer_prefix
        self.secure = secure
        self.chunk_size = chunk_size
        self.peer_id = f"{peer_prefix}-{self.room}"

        self._peer = None
        self._connections: List[Any] = []
        #: 每条连接的接收重组器与发送锁 (见 _reply: 分块响应必须整组连着发)
        self._conn_state: Dict[Any, dict] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready = asyncio.Event()
        self._started = False

    # ---------------- 鉴权 ----------------
    def _check_token(self, provided: Optional[str]) -> bool:
        if not self.token:
            return True
        import hmac

        return hmac.compare_digest(str(provided or ""), str(self.token))

    # ---------------- 生命周期 ----------------
    async def start(self) -> None:
        """注册到公开 broker 并挂上连接回调 (非阻塞)。"""
        from peerjs.enums import ConnectionEventType, PeerEventType
        from peerjs.peer import Peer, PeerOptions

        # 过滤掉不可用的本机 ICE 候选 (断开的虚拟网卡常留 169.254.x.x,
        # aioice 会去 bind 它们并失败, 导致 P2P 通道建不起来) —— 见 remote/ice.py
        from .ice import patch_aioice_addresses

        patch_aioice_addresses()

        # fork 的 PeerOptions.secure 默认 False, 但 cloud broker 只收 wss(443)
        self._peer = Peer(self.peer_id, PeerOptions(secure=self.secure))
        self._loop = asyncio.get_running_loop()

        def on_connection(conn):
            self._connections.append(conn)
            log.info("PeerJS 客户端接入: %s", getattr(conn, "peerId", "?"))

            state = {"chunker": PeerJsChunker(), "send_lock": asyncio.Lock()}
            self._conn_state[conn] = state
            chunker = state["chunker"]

            def on_data(data):
                if isinstance(data, (bytes, bytearray)):  # fork 不支持二进制
                    log.warning("收到二进制帧, 本端只支持 JSON, 已忽略")
                    return
                if not isinstance(data, dict):
                    return
                msg = chunker.feed(data)
                if msg is None:
                    return  # 分块未收齐
                # 回调在事件循环里, 用 create_task 跑异步处理
                if self._loop is not None:
                    self._loop.create_task(self._handle(conn, msg))

            def on_close(*_):
                log.info("PeerJS 连接断开: %s", getattr(conn, "peerId", "?"))
                self._conn_state.pop(conn, None)
                try:
                    self._connections.remove(conn)
                except ValueError:
                    pass

            conn.on(ConnectionEventType.Data, on_data)
            conn.on(ConnectionEventType.Close, on_close)

        def on_open(peer_id):
            log.info("PeerJS 主机已注册: %s (公开 broker)", peer_id)
            self._ready.set()

        self._peer.on(PeerEventType.Connection, on_connection)
        self._peer.on(PeerEventType.Open, on_open)

        await self._peer.start()
        self._started = True

    async def wait_ready(self, timeout: float = 20.0) -> bool:
        """等 broker 确认注册 (二维码应该在这之后再给)。"""
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def close(self) -> None:
        for conn in list(self._connections):
            try:
                await conn.close()
            except Exception:
                pass
        self._connections.clear()
        self._conn_state.clear()
        if self._peer is not None:
            try:
                await self._peer.destroy()
            except Exception:
                pass
            self._peer = None
        self._started = False
        self._ready.clear()

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    @property
    def clients(self) -> int:
        return len(self._connections)

    # ---------------- 处理 ----------------
    async def _handle(self, conn, msg: dict) -> None:
        """处理一条完整请求并回发响应 (必要时分块)。"""
        # ---- 鉴权 ----
        op = msg.get("op")
        if self.token and not _is_authed(conn):
            token = None
            if op == "auth":
                token = (msg.get("args") or {}).get("token") or msg.get("token")
            if self._check_token(token):
                _mark_authed(conn)
                await self._reply(conn, {"id": msg.get("id"), "ok": True, "result": {"authenticated": True}})
                return
            await self._reply(
                conn,
                {
                    "id": msg.get("id"),
                    "ok": False,
                    "error": {"code": "unauthorized", "message": "需要 token"},
                },
            )
            try:
                await conn.close()
            except Exception:
                pass
            return

        # ---- kuuki 老协议自动路由 ----
        if op is None and any(k in msg for k in ("t", "mouse", "text", "key", "calibrate")):
            try:
                result = await asyncio.to_thread(self.service.handle, "kuuki", {"message": msg})
                await self._reply(conn, {"id": msg.get("id"), "ok": True, "result": result})
            except RemoteError as exc:
                await self._reply(conn, {"id": msg.get("id"), "ok": False, "error": exc.to_dict()})
            return

        if op == "auth":
            await self._reply(conn, {"id": msg.get("id"), "ok": True, "result": {"authenticated": True}})
            return

        # ---- 普通 op ----
        try:
            result = await asyncio.to_thread(self.service.handle_request, msg)
            await self._reply(conn, {"id": msg.get("id"), "ok": True, "result": result})
        except RemoteError as exc:
            await self._reply(conn, {"id": msg.get("id"), "ok": False, "error": exc.to_dict()})
        except Exception as exc:  # noqa: BLE001
            await self._reply(
                conn,
                {
                    "id": msg.get("id"),
                    "ok": False,
                    "error": {"code": "internal", "message": f"{exc.__class__.__name__}: {exc}"},
                },
            )

    async def _reply(self, conn, payload: dict) -> None:
        """回一条响应; 持锁保证它的一组分块**连着**发出去。

        一个响应可能被拆成 head + N×data + end。若两条大响应的分块交错发出,
        虽然接收端按 ``_id`` 分组仍能重组, 但一组的块会横跨另一组的块,
        更容易撞上 ``PeerJsChunker.max_pending`` 的淘汰。整组连着发最省心。
        """
        state = self._conn_state.get(conn)
        if state is None:  # 连接已断开/未登记时退化为不带锁发送
            await self._send(conn, payload)
            return
        async with state["send_lock"]:
            await self._send(conn, payload)

    async def _send(self, conn, payload: dict) -> None:
        """发一条响应; 大响应自动分块。"""
        try:
            for part in split_message(payload, self.chunk_size):
                await conn.send(part)
        except Exception as exc:  # 连接已断等
            log.debug("发送失败: %s: %s", exc.__class__.__name__, exc)

    # ---------------- 描述 ----------------
    def describe(self) -> dict:
        return {
            "transport": "peerjs",
            "peer_id": self.peer_id,
            "room": self.room,
            "broker": "0.peerjs.com:443 (公开 cloud broker)",
            "ready": self.ready,
            "clients": self.clients,
            "token_required": bool(self.token),
        }
