"""``python -m remote.ctl`` —— **控制端** CLI: 一台控制多台 (原 ``kuuki_ctl``)。

它与 ``python -m remote.client`` 的分工——两者都连同样的三传输, 但层级不同:

- ``remote.client`` = **单机调试级**: 一条命令打一台机器一个 op, 打完就退出。
  地址只能临时用 ``--url`` / ``--target`` / ``--peer`` 传, **不记任何东西**。
- ``remote.ctl``     = **多机编排级**: 先把机器记进 ``registry.json`` 起个别名,
  之后按别名/组/全体分发同一条命令, 并发执行、超时保护、**结果按机器汇总**。

    ctl machines add pc1 --transport ws --endpoint ws://192.168.1.20:8765 --token T
    ctl machines add pc2 --transport grpc --endpoint 192.168.1.21:50051 --group office
    ctl machines list
    ctl ping pc1 pc2            # 单发多台
    ctl ping all                # 广播
    ctl info -g office          # 组播
    ctl shot pc1 pc1.png        # 截屏存文件
    ctl move pc1 400 300 --duration .3
    ctl combo pc1 ctrl+shift+s --hold 200

``registry.json`` 的默认位置: 环境变量 ``$KUUKI_REGISTRY``, 否则
``~/.kuuki/registry.json`` (命令行可用 ``--registry PATH`` 覆盖)。

安全提醒: registry 里的 token 是**明文**存的, 文件会尽量收权限
(POSIX 0600; Windows 上``machines add``时会尝试用 icacls 收紧 ACL)。
不想落盘就把 token 留在环境变量 ``KUUKI_REMOTE_TOKEN`` 里, registry 里留空。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

# 允许 `python remote/ctl.py` 直接跑
if __package__ in (None, ""):  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from remote.service import VERSION  # noqa: E402

TRANSPORTS = ("ws", "grpc", "peerjs")

#: 各传输的默认单台超时。peerjs 明显更宽: 它的一次调用里含 broker 注册 +
#: ICE 候选交换 + 打洞, 局域网内也要几秒; 按 ws 那套 10s 给, 慢一点的网络
#: 会在握手阶段就把预算吃光, 表现为"每次都刚好超时"。
DEFAULT_TIMEOUT: Dict[str, float] = {"ws": 10.0, "grpc": 10.0, "peerjs": 30.0}

#: registry 文件格式版本
SCHEMA = 1

#: PeerJS 的 peer id 允许的字符 (broker 侧限制; 不含 ':' 与空白)
_PEER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")


def default_timeout_for(transport: str) -> float:
    return DEFAULT_TIMEOUT.get(transport, 10.0)


def validate_endpoint(transport: str, endpoint: str) -> Optional[str]:
    """检查 endpoint 与传输是否匹配; 没问题返回 None, 否则返回给人看的原因。

    早失败比晚失败好: 三种传输的地址长得完全不一样, 拼错了也不会在
    ``machines add`` 时报错, 而是等到真正下发时才抛一个看不懂的连接错误,
    在多机汇总里很容易被当成"那台机器挂了"。
    """
    endpoint = (endpoint or "").strip()
    if not endpoint:
        return "endpoint 不能为空"
    if transport == "ws":
        if not endpoint.startswith(("ws://", "wss://")):
            return f"ws 传输的 endpoint 必须是 ws:// 或 wss:// 开头的 URL, 收到 {endpoint!r}"
        return None
    if transport == "grpc":
        if "://" in endpoint:
            return f"grpc 传输的 endpoint 是 host:port (不要带 scheme), 收到 {endpoint!r}"
        return None
    if transport == "peerjs":
        if "://" in endpoint or "/" in endpoint or " " in endpoint:
            return (
                f"peerjs 传输的 endpoint 是 peer id (如 kuuki-mouse-ABCDE), "
                f"不是 URL, 收到 {endpoint!r}"
            )
        if not _PEER_ID_RE.match(endpoint):
            return (
                f"{endpoint!r} 不是合法的 peer id: 只允许字母数字与 . _ -, "
                f"长度 3-64 且以字母数字开头"
            )
        return None
    return f"未知传输 {transport!r}"


# ================================================================ registry


def default_registry_path() -> str:
    """默认 registry 位置: ``$KUUKI_REGISTRY`` 优先, 否则 ``~/.kuuki/registry.json``。"""
    override = os.environ.get("KUUKI_REGISTRY")
    if override:
        return os.path.expanduser(override)
    return os.path.join(os.path.expanduser("~"), ".kuuki", "registry.json")


@dataclass
class Machine:
    """一台受控端的连接信息。"""

    alias: str
    transport: str
    endpoint: str
    token: Optional[str] = None
    #: agent 只支持 Windows, 这里恒为 "win"; 留字段是为了将来真多平台时不用改结构
    os: str = "win"
    groups: List[str] = field(default_factory=list)
    timeout: float = 10.0
    note: str = ""
    #: online / offline / unknown, 由每次调用的结果回填
    state: str = "unknown"
    last_seen: Optional[str] = None
    last_error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, alias: str, raw: dict) -> "Machine":
        groups = raw.get("groups") or raw.get("group") or []
        if isinstance(groups, str):
            groups = [g for g in groups.split(",") if g]
        return cls(
            alias=alias,
            transport=str(raw.get("transport", "ws")),
            endpoint=str(raw.get("endpoint", "")),
            token=raw.get("token"),
            os=str(raw.get("os", "win")),
            groups=list(groups),
            timeout=float(raw.get("timeout", 10.0) or 10.0),
            note=str(raw.get("note", "")),
            state=str(raw.get("state", "unknown")),
            last_seen=raw.get("last_seen"),
            last_error=raw.get("last_error"),
        )

    def target_hint(self) -> str:
        """给用户看的一行定位信息 (不带 token)。"""
        return f"{self.endpoint} ({self.transport})"


def _replace_with_retry(source: str, destination: str, attempts: int = 40) -> None:
    """``os.replace`` + 退避重试 —— **Windows 特有的一步**。

    POSIX 的 rename 覆盖是原子的, 目标文件正被别人读也没关系。Windows 不一样:
    目标只要还开着句柄, ``MoveFileEx`` 就直接回 WinError 5「拒绝访问」。多操纵端场景下
    另一个操纵端恰好在 ``Registry.load`` 的 ``open()`` 里, 这里就会崩 —— 实测 50 次
    并发写里能撞上 13 次。

    重试窗口总共约 1s, 对手只是一次读文件的 open/close, 足够它走完。
    """
    delay = 0.005
    for attempt in range(attempts):
        try:
            os.replace(source, destination)
            return None
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 1.4, 0.05)
    return None


def _read_json(path: str, attempts: int = 40, delay: float = 0.005) -> dict:
    """读 JSON, Windows 上遇到 ``PermissionError`` 退避重试。

    同一个 catch: 对面进程正在 ``os.replace`` 的瞬间, Windows 会把目标标成"待替换",
    这时的 ``open(path, 'r')`` 直接 ``PermissionError`` (Errno 13) —— **不是权限问题**,
    只是撞上了别人的写。跨进程多操纵端实测 25 次里撞上一次, 不重试就会把整个
    ``machines add`` 打成崩溃。

    POSIX 的 rename 不会让别人读到一半, 也不需要这层, 但重试在这里是无害的。
    """
    for attempt in range(attempts):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            raise
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 1.4, 0.05)
    raise AssertionError("unreachable")  # pragma: no cover


class FileLock:
    """跨进程的**建议锁** (advisory lock), 尽力而为。

    只在"大家都配合"时才互斥: 不配合的进程照样能改文件。所以它保护的是**我们自己的多个
    操纵端**互相打架, 挡不住第三方, 那层靠 :meth:`Registry.save` 的原子写兜底 (最多读到
    旧快照, 不会读到写坏的 JSON)。

    拿不到锁会退化成"当没锁"而不是报错 —— registry 不是金融账本, 宁可偶尔丢一次状态
    回填, 也不能让 ``ping`` 因为别的操纵端卡住而失败。

    Windows 跨平台那条弯路总结一句: 文件锁只能管**跨进程**, 管不了同进程里的多线程,
    所以类里面还额外叠了一把 ``threading.Lock`` —— 两层都要。

    POSIX 用 ``flock`` (进程退出自动释放); Windows 没有 flock, 用 ``msvcrt.locking``
    锁 lock 文件的第一个字节, 阻塞版要自己轮询 (``LK_NBLCK`` 拿不到就抛 ``OSError``)。
    """

    #: **同进程**互斥。Windows 的 msvcrt 锁只跨进程生效 —— 实测同一进程的 4 个线程能
    #: 同时进临界区 (峰值 4/4), 而 POSIX 的 flock 按 open file description 是能互斥的。
    #: 所以进程内这层必须自己补, 否则「一个操纵端里并行发起多个下发」照样丢更新。
    _local_locks: Dict[str, Any] = {}
    _local_guard: Any = None

    def __init__(self, path: str, timeout: float = 5.0, poll: float = 0.02):
        self.path = path
        self.timeout = timeout
        self.poll = poll
        self._handle = None
        self._local = None
        self.held = False

    @classmethod
    def _process_lock(cls, path: str) -> Any:
        with cls._guard():
            lock = cls._local_locks.get(path)
            if lock is None:
                lock = threading.Lock()
                cls._local_locks[path] = lock
            return lock

    @classmethod
    def _guard(cls) -> Any:
        with FileLock._meta_lock:
            if cls._local_guard is None:
                cls._local_guard = threading.Lock()
            return cls._local_guard

    _meta_lock: Any = threading.Lock()

    def __enter__(self) -> bool:
        self._local = self._process_lock(self.path)
        self._local.acquire()  # 进程内在前
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            self._handle = open(self.path, "a+b")
            if os.name == "nt":
                import msvcrt

                # 锁定的区域必须真的有内容, 空文件锁不了 —— 但**不能用 read 去判断**:
                # Windows 上别人锁住的字节范围对别的进程是不可读的, read(1) 会直接
                # PermissionError (看着像"没权限", 其实是"别人正在持有锁"), 于是会被误判
                # 成"锁不上"直接退化 —— 实测多进程场景下每次都退化, 更新照丢。用 size 判断。
                if os.path.getsize(self.path) == 0:
                    try:
                        self._handle.write(b".")
                        self._handle.flush()
                    except OSError:  # 别人抢先写了也一样能用
                        pass
                self._handle.seek(0)
                deadline = time.perf_counter() + self.timeout
                while True:
                    try:
                        msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                        self.held = True
                        break
                    except OSError:
                        if time.perf_counter() >= deadline:
                            break
                        time.sleep(self.poll)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
                self.held = True
        except Exception:  # noqa: BLE001 - 锁不上就退化, 不阻断主流程
            self.held = False
        return self.held

    def __exit__(self, *_exc) -> None:
        if self._handle is not None:
            try:
                if self.held:
                    if os.name == "nt":
                        import msvcrt

                        self._handle.seek(0)
                        msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    self._handle.close()
                finally:
                    self._handle = None
                    self.held = False
        # 先放开文件锁给别人 (跨进程), 再放进程内的锁给下一个线程 -- 顺序别反
        if self._local is not None:
            self._local.release()
            self._local = None


class Registry:
    """``registry.json`` 的读写。文件格式: ``{"version": 1, "machines": {...}}``。"""

    def __init__(self, path: str):
        self.path = path
        self.data: Dict[str, Any] = {"version": SCHEMA, "machines": {}}
        #: 最近一次 :meth:`transaction` 有没有真的拿到锁 (拿不到会退化, 仅供诊断)
        self.locked: bool = False

    # ---------------- 读写 ----------------

    @classmethod
    def load(cls, path: Optional[str] = None) -> "Registry":
        registry = cls(path or default_registry_path())
        try:
            raw = _read_json(registry.path)
        except FileNotFoundError:
            return registry
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"registry {registry.path} 不是合法 JSON: {exc}")
        version = int(raw.get("version", 1))
        machines = raw.get("machines", {})
        if not isinstance(machines, dict):
            raise RuntimeError(f"registry {registry.path} 的 machines 应为对象")
        registry.data["version"] = version
        registry.data["machines"] = machines
        return registry

    def save(self) -> None:
        """原子写: 先写临时文件再 ``os.replace``。

        直接覆盖的话, 另一个进程读到一半会看到写残的 JSON, 然后整份 registry 就报
        ``不是合法 JSON`` 了。这里换成 rename —— POSIX 与 NTFS 的 rename 都是原子的。
        """
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        existed = os.path.exists(self.path)
        # tmp 名字必须带随机串: 同一个进程里的多个操纵端共享 pid, 名字撞了就会互相
        # truncate 同一个临时文件, 最后 replace 上去的是被截断过的那份 (实测 50 台 -> 1 台)
        temporary = f"{self.path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if not existed:
            restrict_permissions(temporary)  # 换名前先收权限, 免得有一瞬间是宽松的
        try:
            _replace_with_retry(temporary, self.path)
        finally:
            if os.path.exists(temporary):  # 换名没成功的话别把垃圾留在盘上
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    # ---------------- 并发: 事务 ----------------

    @contextmanager
    def transaction(self) -> Iterator["Registry"]:
        """原子的 read-modify-write —— **多个操纵端并存时的必需步骤**。

        朴素的 ``load() -> 改 -> save()`` 在多进程/多线程下会丢更新: 两个操纵端各自
        load 到同一份快照, 各自改自己的那部分, 后保存的会把先保存的**整份**冲掉。
        实测双线程各 add 25 台 -> 最后只剩 26 台, 丢了 24 条。

        这里在文件锁里**重新读盘**、改最新的那份、再原子写回。别人在锁外往别的键上写
        的条目因此会被保留; 撞同一台机器的状态回填则按"后写完胜", 这是可接受的 ——
        状态本来就是"最后一次看到的样子"。

        用法::

            with registry.transaction() as live:
                live.put(machine)        # 对 live 做改动才会被保存

        ``with`` 块里 ``return`` / 抛异常时**不会**写盘 (gen 在 yield 处收到 GeneratorExit,
        yield 之后的 save 不执行) —— 校验失败就该什么都不做, 正好。
        """
        with FileLock(f"{self.path}.lock") as held:
            self.locked = held
            fresh = type(self).load(self.path)
            try:
                yield fresh
            except GeneratorExit:
                raise
            fresh.save()

    # ---------------- 条目操作 ----------------

    def machines(self) -> Dict[str, Machine]:
        return {alias: Machine.from_dict(alias, raw) for alias, raw in self.data["machines"].items()}

    def get(self, alias: str) -> Optional[Machine]:
        raw = self.data["machines"].get(alias)
        return Machine.from_dict(alias, raw) if raw is not None else None

    def put(self, machine: Machine) -> None:
        self.data["machines"][machine.alias] = machine.to_dict()

    def remove(self, alias: str) -> bool:
        return self.data["machines"].pop(alias, None) is not None

    def resolve(
        self,
        targets: Sequence[str],
        group: Optional[str] = None,
        all_machines: bool = False,
    ) -> List[Machine]:
        """把命令行上的目标描述解析成机器列表。

        - ``all`` / ``all_machines=True``: 全部
        - ``--group g``: 该组全部成员
        - 其余按别名精确匹配, 未知别名报 ``KeyError``
        """
        machines = self.machines()
        picked: List[Machine] = []
        seen: set = set()

        def add(machine: Machine) -> None:
            if machine.alias not in seen:
                seen.add(machine.alias)
                picked.append(machine)

        if all_machines or "all" in targets:
            for alias in sorted(machines):
                add(machines[alias])
        if group:
            members = [m for m in machines.values() if group in m.groups]
            if not members:
                known = sorted({g for m in machines.values() for g in m.groups})
                raise KeyError(f"没有机器属于组 {group!r}; 已知组: {', '.join(known) or '(无)'}")
            for machine in sorted(members, key=lambda m: m.alias):
                add(machine)
        for raw in targets:
            if raw == "all":
                continue
            if raw in machines:
                add(machines[raw])
                continue
            raise KeyError(f"未知别名 {raw!r}; 已注册: {', '.join(sorted(machines)) or '(空)'}")
        if not picked:
            raise KeyError("没有选中任何机器 —— 给个别名、--group, 或 all")
        return picked


def restrict_permissions(path: str) -> None:
    """尽量把 registry 收到只有本机用户能读 (token 明文存在那儿)。

    失败了也不碍事 —— 这只是收窄, 不影响文件可用性, 所以全部异常都吞掉。
    """
    try:
        if os.name == "nt":
            user = os.environ.get("USERNAME") or os.environ.get("USER") or "Everyone"
            # 先显式授予当前用户完全控制, 再断开继承 —— 顺序反了有可能把 ACL 清成空的
            subprocess.run(
                ["icacls", path, "/grant:r", f"{user}:(F)"],
                timeout=8, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["icacls", path, "/inheritance:r"],
                timeout=8, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            os.chmod(path, 0o600)
    except Exception:  # noqa: BLE001 - 收紧权限是尽力而为
        pass


# ================================================================ 传输层适配


async def call_machine(machine: Machine, op: str, args: Optional[dict] = None) -> dict:
    """对一台机器发一条 op, 返回 result dict。三种传输在此对齐。"""
    args = args or {}
    if machine.transport == "ws":
        from remote.client import WsClient

        async with WsClient(machine.endpoint, token=machine.token, timeout=machine.timeout) as client:
            return await client.call(op, args)

    if machine.transport == "grpc":
        from remote.client import GrpcClient

        def blocking() -> dict:
            with GrpcClient(machine.endpoint, token=machine.token, timeout=machine.timeout) as client:
                return client.call(op, args)

        # GrpcClient 是同步的 (gRPC 库本身同步), 丢到线程里避免堵住事件循环
        return await asyncio.to_thread(blocking)

    if machine.transport == "peerjs":
        from remote.peerjs_client import PeerJsClient

        client = PeerJsClient(machine.endpoint, token=machine.token, timeout=machine.timeout)
        await client.connect()
        try:
            return await client.call(op, args)
        finally:
            await client.close()

    raise ValueError(f"未知传输 {machine.transport!r}; 支持: {', '.join(TRANSPORTS)}")


async def capture_machine(machine: Machine, args: Optional[dict] = None) -> Tuple[dict, bytes]:
    """抓一帧, 返回 ``(meta, data)``。

    三传输的截图接口长得不一样: WS 走二进制帧 (有 header)、gRPC 返回 protobuf
    image (元数据在 message 字段)、PeerJS 只给 base64 → bytes。这里统一掉。
    """
    args = args or {}
    if machine.transport == "ws":
        from remote.client import WsClient

        async with WsClient(machine.endpoint, token=machine.token, timeout=machine.timeout) as client:
            header, payload = await client.screenshot(args)
        return dict(header or {}, transport="ws"), payload

    if machine.transport == "grpc":
        from remote.client import GrpcClient

        def blocking() -> Tuple[dict, bytes]:
            with GrpcClient(machine.endpoint, token=machine.token, timeout=machine.timeout) as client:
                data = client.screenshot(args)
                image = client.last_image
                return {
                    "transport": "grpc",
                    "format": image.format,
                    "width": image.width,
                    "height": image.height,
                    "duration_ms": round(image.duration_ms, 2),
                }, data

        return await asyncio.to_thread(blocking)

    if machine.transport == "peerjs":
        from remote.peerjs_client import PeerJsClient

        client = PeerJsClient(machine.endpoint, token=machine.token, timeout=machine.timeout)
        await client.connect()
        try:
            payload = await client.screenshot(args)
            meta = dict(client.last_capture or {})
        finally:
            await client.close()
        # last_capture 里已经带了 format/width/height/duration_ms/bytes,
        # 补上 transport 后与 WS / gRPC 那两路的 meta 形状一致
        meta["transport"] = "peerjs"
        meta.setdefault("bytes", len(payload))
        return meta, payload

    raise ValueError(f"未知传输 {machine.transport!r}; 支持: {', '.join(TRANSPORTS)}")


# ================================================================ 分发与汇总


async def dispatch(
    machines: Sequence[Machine],
    worker,
    serial: bool = False,
) -> List[dict]:
    """把 ``worker(machine)`` 分发给多台机器, 汇总成统一结构的结果列表。

    ``worker`` 是可等待函数, 抛异常即判失败。每台自带 ``machine.timeout``,
    超时统一报 ``TimeoutError`` 文案 (同一个错误不至于在不同机器上长得不一样)。
    """
    results: List[dict] = []

    async def one(machine: Machine) -> dict:
        started = time.perf_counter()
        try:
            value = await asyncio.wait_for(worker(machine), timeout=machine.timeout)
        except asyncio.TimeoutError:
            return _failure(machine, TimeoutError(f"超过 {machine.timeout}s 未响应"), started)
        except Exception as exc:  # noqa: BLE001 - 汇总要的是错误本身, 不是崩溃
            return _failure(machine, exc, started)
        elapsed = (time.perf_counter() - started) * 1000.0
        return {"alias": machine.alias, "ok": True, "elapsed_ms": round(elapsed, 1), "result": value}

    if serial:
        for machine in machines:
            results.append(await one(machine))
    else:
        results = list(await asyncio.gather(*(one(machine) for machine in machines)))
    return results


def _failure(machine: Machine, exc: BaseException, started: float) -> dict:
    elapsed = (time.perf_counter() - started) * 1000.0
    return {
        "alias": machine.alias,
        "ok": False,
        "elapsed_ms": round(elapsed, 1),
        "error": f"{exc.__class__.__name__}: {exc}",
    }


def update_states(registry: Registry, results: Sequence[dict]) -> None:
    """把本次结果回填进 registry: 成功标 online + last_seen, 失败标 offline + last_error。"""
    now = _now()
    for item in results:
        raw = registry.data["machines"].get(item["alias"])
        if raw is None:
            continue
        if item["ok"]:
            raw["state"] = "online"
            raw["last_seen"] = now
            raw["last_error"] = None
        else:
            raw["state"] = "offline"
            raw["last_error"] = item["error"]


# ================================================================ 输出


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def render(results: Sequence[dict], as_json: bool) -> None:
    if as_json:
        print(json.dumps(list(results), ensure_ascii=False, indent=2))
        return
    for item in results:
        mark = "OK  " if item["ok"] else "FAIL"
        head = f"{mark} {item['alias']:<12} {item['elapsed_ms']:>7.1f}ms"
        if item["ok"]:
            payload = json.dumps(item["result"], ensure_ascii=False)
            print(f"{head}  {payload}")
        else:
            print(f"{head}  {item['error']}")


def save_frames(directory: str, alias: str, index: int, extension: str, payload: bytes) -> str:
    """落盘一帧, 目录按别名分子目录 (多机不会互相覆盖)。"""
    folder = os.path.join(directory, alias)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{index:04d}.{extension}")
    with open(path, "wb") as handle:
        handle.write(payload)
    return path


def expand_path(path: str, alias: str, multi: bool) -> str:
    """多台同时截屏时给文件名加别名, 避免后一台覆盖前一台。

    - 占位符 ``{alias}`` / ``{ts}`` 会替换
    - 多机 + 单个文件名: 自动插成 ``name-alias.ext``
    """
    if "{alias}" in path or "{ts}" in path:
        return path.format(alias=alias, ts=time.strftime("%Y%m%d-%H%M%S"))
    if not multi:
        return path
    root, extension = os.path.splitext(path)
    if not extension:  # 没扩展名当作目录
        return os.path.join(path, f"{alias}.png")
    return f"{root}-{alias}{extension}"


# ================================================================ CLI


def _shared_options(parser: argparse.ArgumentParser, include: Sequence[str] = ()) -> None:
    """全局选项, **命令前后都能写**。

    默认值一律用 ``argparse.SUPPRESS``: 子命令 parser 与外层 parser 共享同一个
    namespace, 如果这里给正常默认值, 子命令解析时会用它**覆盖**掉命令行上已经
    给过的值(action 的默认值总会写进 namespace)。SUPPRESS = 没出现就不往里写,
    最后统一由 :func:`_fill_defaults` 补齐。

    ``include`` 为空表示全部加。
    """
    wanted = set(include) or None  # None = 不加限制
    group = parser.add_argument_group("全局选项 (写在命令前或后都可以)")
    if wanted is None or "registry" in wanted:
        group.add_argument("--registry", default=argparse.SUPPRESS, help=f"默认 {default_registry_path()}")
    if wanted is None or "timeout" in wanted:
        group.add_argument(
            "--timeout", type=float, default=argparse.SUPPRESS,
            help="覆盖每台机器的超时秒数",
        )
    group.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="机器可读输出")
    group.add_argument("--serial", action="store_true", default=argparse.SUPPRESS, help="串行执行 (默认并发)")
    group.add_argument(
        "--no-update", action="store_true", default=argparse.SUPPRESS,
        help="不把本次结果回填进 registry (不改 registry 文件)",
    )


_SHARED_DEFAULTS = {
    "registry": None,
    "timeout": None,
    "json": False,
    "serial": False,
    "no_update": False,
}


def _fill_defaults(args: argparse.Namespace) -> argparse.Namespace:
    """补齐用 SUPPRESS 省略掉的全局选项。"""
    for key, value in _SHARED_DEFAULTS.items():
        if not hasattr(args, key):
            setattr(args, key, value)
    return args


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m remote.ctl",
        description="kuuki remote 控制端: 一台管多台 (别名/组/广播 + 结果汇总)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"kuuki ctl {VERSION}")
    _shared_options(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    # ---- machines
    machines = sub.add_parser("machines", help="机器注册表管理")
    machines_sub = machines.add_subparsers(dest="action", required=True)

    listing = machines_sub.add_parser("list", help="列出已注册机器")
    listing.add_argument("--show-token", action="store_true", help="显示 token (默认打码)")
    _shared_options(listing)

    add = machines_sub.add_parser("add", help="注册一台机器")
    add.add_argument("alias")
    add.add_argument("--transport", choices=TRANSPORTS, required=True)
    add.add_argument("--endpoint", required=True, help="ws://host:8765 / host:50051 / kuuki-mouse-XXXX")
    add.add_argument("--token", default=None)
    # -g 与动作命令里的 "--group" 是同一个意思, 别名统一起来写起来更顺手
    add.add_argument("-g", "--group", action="append", default=[], help="可重复, 也可用逗号分隔")
    add.add_argument("--os", default="win", help="agent 端平台 (agent 只支持 Windows, 恒为 win)")
    add.add_argument(
        "--timeout", type=float, default=argparse.SUPPRESS,
        help=f"这台机器的默认超时; 不给则按传输取 (ws/grpc {DEFAULT_TIMEOUT['ws']}s, "
             f"peerjs {DEFAULT_TIMEOUT['peerjs']}s —— 它还要算上 broker 注册与 ICE 打洞)",
    )
    add.add_argument("--note", default="")
    add.add_argument("--force", action="store_true", help="覆盖同名条目")
    # machines add 自己的 --timeout 是"这台机器的默认超时", 全局那个是"本次调用超时",
    # 两个同名会撞车 —— 这里只保留前者。
    _shared_options(add, include=("registry", "json"))

    rm = machines_sub.add_parser("rm", help="注销一台机器")
    rm.add_argument("alias")
    _shared_options(rm)

    show = machines_sub.add_parser("show", help="看一台机器的完整配置")
    show.add_argument("alias")
    show.add_argument("--show-token", action="store_true")
    _shared_options(show)

    # ---- 动作命令: 都带 targets
    _add_action(sub, "ping", "连通性检查")
    _add_action(sub, "info", "服务/环境信息")
    _add_action(sub, "pos", "读光标位置 (mouse.position)")

    # op 的名字本身是位置参数, 再塞一组位置参数当目标会分不清谁是名字谁是别名,
    # 所以它只用 -t/--to/-g/-a 来选目标。
    op = _add_action(sub, "op", "发任意 op", positional_targets=False)
    op.add_argument("name")
    op.add_argument("--args", default="{}", help="JSON 参数")

    # 这三个接的路径也是位置参数, 会被 alias 列表吃进去, 所以目标统一走 -t/-g/-a
    shot = _add_action(sub, "shot", "截屏存文件", positional_targets=False)
    shot.add_argument("path", nargs="?", default="shot.png")
    _add_shot_args(shot)

    for name, directory in (("watch", "连续抓帧落到目录 (轮询)"), ("tail", "同 watch, 默认一直抓到 Ctrl-C")):
        watch = _add_action(sub, name, directory, positional_targets=False)
        watch.add_argument("path", nargs="?", default="frames")
        watch.add_argument("--fps", type=float, default=2.0)
        watch.add_argument("--count", type=int, default=5)
        _add_shot_args(watch)

    move = _add_action(sub, "move", "绝对移动")
    move.add_argument("x", type=int)
    move.add_argument("y", type=int)
    move.add_argument("--duration", type=float, default=0.0)

    rel = _add_action(sub, "move-rel", "相对移动")
    rel.add_argument("--dx", type=int, default=0)
    rel.add_argument("--dy", type=int, default=0)
    rel.add_argument("--duration", type=float, default=0.0)

    click = _add_action(sub, "click", "点击")
    click.add_argument("--button", default="left")
    click.add_argument("--clicks", type=int, default=1)
    click.add_argument("--interval", type=float, default=0.05)
    click.add_argument("--hold", type=float, default=None)

    for name, help_text in (("down", "按住"), ("up", "松开")):
        _add_action(sub, name, help_text).add_argument("--button", default="left")

    scroll = _add_action(sub, "scroll", "滚轮 (多步平滑)")
    scroll.add_argument("--dx", type=int, default=0)
    scroll.add_argument("--dy", type=int, default=0)
    scroll.add_argument("--steps", type=int, default=1)
    scroll.add_argument("--interval", type=float, default=None)
    scroll.add_argument("--x", type=int, default=None, help="先移到该处再滚")
    scroll.add_argument("--y", type=int, default=None)

    scroll_h = _add_action(sub, "scroll-h", "横向滚轮")
    scroll_h.add_argument("--dx", type=int, default=0)
    scroll_h.add_argument("--steps", type=int, default=1)

    drag = _add_action(sub, "drag", "两点拖拽")
    drag.add_argument("x1", type=int)
    drag.add_argument("y1", type=int)
    drag.add_argument("x2", type=int)
    drag.add_argument("y2", type=int)
    drag.add_argument("--button", default="left")
    drag.add_argument("--duration", type=float, default=None)

    dragp = _add_action(sub, "dragp", "路径拖拽: \"x,y;x,y;x,y\"")
    dragp.add_argument("points")
    dragp.add_argument("--button", default="left")
    dragp.add_argument("--duration", type=float, default=None)

    typing = _add_action(sub, "type", "逐字符输入")
    typing.add_argument("text")
    typing.add_argument("--interval", type=float, default=0.0)

    _add_action(sub, "paste", "剪贴板粘贴 (中文/emoji 走这个)").add_argument("text")

    key = _add_action(sub, "key", "单键 tap/press/release")
    key.add_argument("key")
    key.add_argument("--action", default="tap", choices=["tap", "press", "release"])
    key.add_argument("--modifiers", nargs="*", default=[])

    combo = _add_action(sub, "combo", "组合键, 可带按住时长")
    combo.add_argument("keys", help="写成 ctrl+shift+s")
    combo.add_argument("--hold", type=float, default=0.0, help="按住毫秒")

    hold = _add_action(sub, "hold", "按住单键 N 毫秒")
    hold.add_argument("key")
    hold.add_argument("ms", type=float)

    # 同上: check 的键名是 "一坨位置参数", 会和别名列表分不清, 目标只走 -t/-g/-a
    check = _add_action(sub, "check", "键支持性预检 (不真按)", positional_targets=False)
    check.add_argument("keys", nargs="+")

    # 窗口: 多机编排里"先认窗口, 再在窗口里定位"同样成立。只有 Windows 受控端
    # 实现 (remote/window.py); 别的平台会回 unsupported, ctl 照常把它显示出来。
    windows = _add_action(sub, "windows", "列出受控端顶层窗口")
    windows.add_argument("--title", default="", help="标题子串过滤")
    windows.add_argument("--process", default="", help="进程名或 pid 子串")
    windows.add_argument("--limit", type=int, default=0, help="最多列几个 (0 = 不限)")
    windows.add_argument("--include-hidden", action="store_true")

    # 显示器: 多屏机器上"截图是哪块屏 / 鼠标坐标又是哪套"必须先说清楚, 编排前
    # 先看一眼虚拟桌面边界 (原点可以是负的)。只有 Windows 受控端实现。
    monitors = _add_action(sub, "monitors", "列出受控端显示器与虚拟桌面边界")
    monitors.add_argument("--x", type=int, default=None, help="只查这个点在哪块屏上")
    monitors.add_argument("--y", type=int, default=None, help="与 --x 一起给")

    # 文字识别: 编排里"这个按钮上写着什么"也得能问 —— 不同机器可能语言不同、
    # 版本不同, 硬编码坐标之外的另一种稳法。只有 Windows 受控端有 (系统 OCR)。
    ocr = _add_action(sub, "ocr", "认受控端屏幕上的字 (Windows 内置 OCR)")
    ocr.add_argument("--region", default=None, help="只认这一块: left,top,width,height")
    ocr.add_argument("--monitor", type=int, default=None, help="认第几块屏 (下标)")
    ocr.add_argument("--all-screens", action="store_true", help="认整个虚拟桌面")
    ocr.add_argument("--lang", default="", help="语言 tag (zh-Hans-CN); 空 = 按受控端语言偏好")
    ocr.add_argument("--no-words", action="store_true", help="不要逐词的矩形 (省体积)")

    # 按文字定位: 多机编排里"点写着『下一步』的那个按钮"比"点 (932, 604)"稳 ——
    # 每台机器的窗口位置、语言、版本都可能不一样
    find = _add_action(sub, "find-text", "找屏幕上写着某段文字的那块 (OCR)")
    find.add_argument("--query", required=True, help="要找的文字 (--match regex 时是正则)")
    find.add_argument("--match", default="contains", choices=("contains", "exact", "regex"),
                      help="怎么算命中 (默认 contains)")
    find.add_argument("--unit", default="line", choices=("line", "word"),
                      help="按行匹配还是按词 (默认 line)")
    find.add_argument("--case-sensitive", action="store_true", help="区分大小写")
    # 不叫 --all: 那个名字在 ctl 里是"发给所有机器" (全局开关), 会撞
    find.add_argument("--all-matches", action="store_true",
                      help="返回全部匹配 (默认只给最靠上的)")
    find.add_argument("--limit", type=int, default=None, help="最多返回几个 (配合 --all)")
    find.add_argument("--region", default=None, help="只在这一块里找: left,top,width,height")
    find.add_argument("--monitor", type=int, default=None, help="找第几块屏 (下标)")
    find.add_argument("--all-screens", action="store_true", help="找整个虚拟桌面")
    find.add_argument("--lang", default="", help="语言 tag (zh-Hans-CN); 空 = 按受控端语言偏好")

    focus = _add_action(sub, "focus", "把受控端某个窗口切到前台")
    focus.add_argument("--hwnd", type=int, default=None, help="窗口句柄 (最可靠)")
    focus.add_argument("--title", default="", help="标题子串")
    focus.add_argument("--process", default="", help="进程名或 pid 子串")
    focus.add_argument("--index", type=int, default=0, help="命中多个时取第几个")
    focus.add_argument("--wait", type=float, default=None, help="切换后最多等多久确认")

    # 坐标校准: 每台被控机的屏幕布局可能不同 (尤其多显示器), 编排前先在目标机上
    # 跑一次, 拿到的 fit 可以喂给 client 的 locate --calib。会动鼠标。
    calib = _add_action(sub, "calibrate", "实测这台机器的图坐标<->鼠标坐标换算")
    calib.add_argument("--cols", type=int, default=3, help="靶点网格列数")
    calib.add_argument("--rows", type=int, default=3, help="靶点网格行数")
    calib.add_argument("--margin", type=float, default=0.12, help="留边比例")
    calib.add_argument("--settle", type=float, default=0.1, help="移动后等多久再抓帧")
    calib.add_argument("--tolerance", type=float, default=2.0, help="判定没偏的残差上限")
    calib.add_argument("--max-width", type=int, default=0, help="顺带做缩放校准")
    calib.add_argument("--no-restore", action="store_true", help="不把鼠标挪回原位")
    return parser


def _add_action(
    sub,
    name: str,
    help_text: str,
    positional_targets: bool = True,
) -> argparse.ArgumentParser:
    """建一个动作子命令。

    默认把**目标机器**放在紧跟命令的位置 (``ctl ping self self-grpc``);
    ``positional_targets=False`` 时目标只能走 ``-t/--to`` —— 命令自己还有
    位置参数时会用到 (``op`` 的 op 名)。
    """
    parser = sub.add_parser(name, help=help_text)
    if positional_targets:
        parser.add_argument("targets", nargs="*", default=[], help="别名 (可多个), 或写 all")
    parser.add_argument("-t", "--to", action="append", default=[], help="指定一台 (可重复)")
    parser.add_argument("-g", "--group", default=None, help="组播: 该组全部成员")
    parser.add_argument("-a", "--all", action="store_true", help="广播: 全部已注册机器")
    _shared_options(parser)
    return parser


def _add_shot_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", default="png", choices=["png", "jpeg", "jpg", "webp"])
    parser.add_argument("--quality", type=int, default=80)
    parser.add_argument("--max-width", type=int, default=0)
    parser.add_argument("--max-height", type=int, default=0)
    parser.add_argument("--draw-cursor", action="store_true")
    parser.add_argument("--region", default=None, help="left,top,width,height")
    parser.add_argument("--monitor", type=int, default=None,
                        help="抓第几块屏 (下标, 见 monitors 命令); 与 --all-screens 二选一")
    parser.add_argument("--all-screens", action="store_true",
                        help="抓整个虚拟桌面 (多屏拼起来的边界, 原点可能是负的)")


def _shot_args(args: argparse.Namespace) -> dict:
    out: Dict[str, Any] = {"format": args.format, "quality": args.quality}
    if args.max_width:
        out["max_width"] = args.max_width
    if args.max_height:
        out["max_height"] = args.max_height
    if args.draw_cursor:
        out["draw_cursor"] = True
    if args.region:
        out["region"] = [int(v) for v in args.region.split(",")]
    # monitor / all_screens 是"抓哪一块": 只把真的给了的传下去 (0 是合法下标)
    if getattr(args, "monitor", None) is not None:
        out["monitor"] = int(args.monitor)
    if getattr(args, "all_screens", False):
        out["all_screens"] = True
    return out


def _wanted(args: argparse.Namespace) -> List[str]:
    """合并位置目标与 ``--to`` 两种写法。"""
    return list(getattr(args, "targets", []) or []) + list(getattr(args, "to", []) or [])


def _machine_op(args: argparse.Namespace) -> Tuple[str, dict]:
    """把 CLI 动作翻译成 ``(op, args)`` —— 这里不做任何类型转换以外的事。"""
    command = args.command
    if command == "ping":
        return "ping", {}
    if command == "info":
        return "info", {}
    if command == "pos":
        return "mouse.position", {}
    if command == "op":
        return args.name, json.loads(args.args)
    if command == "move":
        return "mouse.move", {"x": args.x, "y": args.y, "duration": args.duration}
    if command == "move-rel":
        return "mouse.move_rel", {"dx": args.dx, "dy": args.dy, "duration": args.duration}
    if command == "click":
        payload = {"button": args.button, "clicks": args.clicks, "interval": args.interval}
        if args.hold is not None:
            payload["hold"] = args.hold
        return "mouse.click", payload
    if command in ("down", "up"):
        return f"mouse.{command}", {"button": args.button}
    if command == "scroll":
        payload: Dict[str, Any] = {"dx": args.dx, "dy": args.dy, "steps": args.steps}
        if args.interval is not None:
            payload["interval"] = args.interval
        if args.x is not None:
            payload["x"] = args.x
        if args.y is not None:
            payload["y"] = args.y
        return "mouse.scroll", payload
    if command == "scroll-h":
        return "mouse.scroll_h", {"dx": args.dx, "dy": 0, "steps": args.steps}
    if command == "drag":
        payload = {"x1": args.x1, "y1": args.y1, "x2": args.x2, "y2": args.y2, "button": args.button}
        if args.duration is not None:
            payload["duration"] = args.duration
        return "mouse.drag", payload
    if command == "dragp":
        payload = {"points": args.points, "button": args.button}
        if args.duration is not None:
            payload["duration"] = args.duration
        return "mouse.drag", payload
    if command == "type":
        return "keyboard.type", {"text": args.text, "interval": args.interval}
    if command == "paste":
        return "keyboard.paste", {"text": args.text}
    if command == "key":
        return "keyboard.key", {"key": args.key, "action": args.action, "modifiers": args.modifiers}
    if command == "combo":
        return "keyboard.combo", {"keys": args.keys, "hold_ms": args.hold}
    if command == "hold":
        return "keyboard.hold", {"key": args.key, "ms": args.ms}
    if command == "check":
        return "keyboard.check", {"keys": args.keys}
    if command == "windows":
        return "window.list", {
            "title": args.title,
            "process": args.process,
            "limit": args.limit,
            "include_hidden": args.include_hidden,
        }
    if command == "monitors":
        # 只给了 x 或 y 中的一个时按"没给"处理 —— 单点查询要两个都有意义
        payload = {}
        if args.x is not None and args.y is not None:
            payload = {"x": args.x, "y": args.y}
        return "screen.monitors", payload
    if command == "ocr":
        payload: Dict[str, Any] = {}
        if args.region:
            payload["region"] = [int(v) for v in args.region.split(",")]
        # monitor=0 是"第一块屏", 不能用 or 兜底
        if args.monitor is not None:
            payload["monitor"] = int(args.monitor)
        if args.all_screens:
            payload["all_screens"] = True
        if args.lang:
            payload["lang"] = args.lang
        if args.no_words:
            payload["include_words"] = False
        return "screen.ocr", payload
    if command == "find-text":
        payload: Dict[str, Any] = {
            "query": args.query,
            "match": args.match,
            "unit": args.unit,
        }
        if args.case_sensitive:
            payload["case_sensitive"] = True
        if args.all_matches:
            payload["all"] = True
        if args.limit is not None:
            payload["limit"] = int(args.limit)
        if args.region:
            payload["region"] = [int(v) for v in args.region.split(",")]
        if args.monitor is not None:
            payload["monitor"] = int(args.monitor)
        if args.all_screens:
            payload["all_screens"] = True
        if args.lang:
            payload["lang"] = args.lang
        return "screen.find_text", payload
    if command == "focus":
        payload: Dict[str, Any] = {"title": args.title, "process": args.process}
        if args.hwnd is not None:
            payload["hwnd"] = int(args.hwnd)
        payload["index"] = int(args.index or 0)
        # --wait 没给就别填 0: 0 表示"不等待确认", 而缺省是服务端默认的轮询
        if args.wait is not None:
            payload["wait"] = float(args.wait)
        if not payload["title"] and not payload["process"] and "hwnd" not in payload:
            raise ValueError("focus 需要 --hwnd / --title / --process 之一")
        return "window.focus", payload
    if command == "calibrate":
        return "screen.calibrate", {
            "cols": int(args.cols or 0),
            "rows": int(args.rows or 0),
            "margin": float(args.margin or 0),
            "settle": float(args.settle or 0),
            "tolerance": float(args.tolerance or 0),
            "max_width": int(args.max_width or 0),
            "restore": not bool(args.no_restore),
        }
    raise ValueError(f"未知命令 {command!r}")


# ================================================================ main


def _run_machines(args: argparse.Namespace, registry: Registry) -> int:
    action = args.action
    if action == "list":
        machines = registry.machines()
        if not machines:
            print(f"registry 里没有机器 ({registry.path})")
            print("  加一台: python -m remote.ctl machines add <别名> --transport ws "
                  "--endpoint ws://127.0.0.1:8765")
            return 0
        print(f"registry: {registry.path}")
        for alias in sorted(machines):
            machine = machines[alias]
            token = machine.token or ""
            shown = token if (args.show_token or not token) else f"{token[:2]}***{token[-1:]}"
            groups = ",".join(machine.groups) or "-"
            print(f"  {alias:<12} {machine.transport:<7} {machine.target_hint():<34} "
                  f"组={groups:<10} {machine.state:<7} {shown}")
        return 0

    if action == "add":
        problem = validate_endpoint(args.transport, args.endpoint)
        if problem is not None:
            print(f"endpoint 与传输不匹配: {problem}", file=sys.stderr)
            return 2
        groups: List[str] = []
        for value in args.group:
            groups.extend([piece for piece in value.split(",") if piece])
        machine = Machine(
            alias=args.alias,
            transport=args.transport,
            endpoint=args.endpoint,
            token=args.token,
            os=args.os,
            groups=groups,
            # --timeout 没给就按传输取默认 (peerjs 更宽, 见 DEFAULT_TIMEOUT)
            timeout=float(getattr(args, "timeout", 0) or default_timeout_for(args.transport)),
            note=args.note,
        )
        # 重名检查也得在锁里做, 否则两个操纵端会同时通过检查再互相覆盖 (TOCTOU)
        with registry.transaction() as live:
            if live.get(args.alias) is not None and not args.force:
                print(f"别名 {args.alias!r} 已存在; 要覆盖加 --force", file=sys.stderr)
                return 2
            live.put(machine)
        print(f"已注册 {args.alias}: {machine.target_hint()}  -> {registry.path}")
        return 0

    if action == "rm":
        with registry.transaction() as live:
            if not live.remove(args.alias):
                print(f"别名 {args.alias!r} 不存在", file=sys.stderr)
                return 2
        print(f"已注销 {args.alias}")
        return 0

    if action == "show":
        machine = registry.get(args.alias)
        if machine is None:
            print(f"别名 {args.alias!r} 不存在", file=sys.stderr)
            return 2
        raw = machine.to_dict()
        if raw.get("token") and not args.show_token:
            raw["token"] = f"{raw['token'][:2]}***{raw['token'][-1:]}"
        print(json.dumps(raw, ensure_ascii=False, indent=2))
        return 0
    return 2


async def _run_action(args: argparse.Namespace, registry: Registry) -> int:
    try:
        machines = registry.resolve(_wanted(args), group=args.group, all_machines=args.all)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.timeout is not None:
        machines = [Machine(**{**machine.to_dict(), "timeout": args.timeout}) for machine in machines]

    if args.command in ("shot", "watch", "tail"):
        results = await _run_frames(args, machines)
    else:
        op, payload = _machine_op(args)
        if not args.json:
            shown = json.dumps(payload, ensure_ascii=False) if payload else "{}"
            print(f"-> {op} {shown}   目标: {', '.join(m.alias for m in machines)}")
        results = await dispatch(
            machines,
            lambda machine: call_machine(machine, op, payload),
            serial=args.serial,
        )

    if not args.no_update:
        try:
            # 走事务: 别人这期间写进来的条目不能被我们这份旧快照冲掉
            with registry.transaction() as live:
                update_states(live, results)
        except OSError as exc:
            print(f"(registry 写回失败: {exc})", file=sys.stderr)

    render(results, args.json)
    failed = [item["alias"] for item in results if not item["ok"]]
    if failed:
        print(f"\n{len(failed)}/{len(results)} 台失败: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


async def _run_frames(args: argparse.Namespace, machines: Sequence[Machine]) -> List[dict]:
    """``shot`` / ``watch`` / ``tail`` 共用: 抓一段帧 (shot 就是 count=1)。"""
    shot_args = _shot_args(args)
    multi = len(machines) > 1
    rolling = args.command in ("watch", "tail")  # 连续帧: path 是**目录**, 下面按别名分目录
    if args.command == "shot":
        count, fps = 1, 1.0
    else:
        count = args.count if args.count > 0 else 0  # 0 = 一直抓到 Ctrl-C
        fps = max(0.1, args.fps)
    if args.command == "tail" and args.count == 5:  # tail 默认无限
        count = 0

    async def worker(machine: Machine) -> dict:
        path = args.path if rolling else expand_path(args.path, machine.alias, multi)
        saved: List[str] = []
        total_bytes = 0
        index = 0
        while count == 0 or index < count:
            meta, payload = await capture_machine(machine, shot_args)
            total_bytes += len(payload)
            if args.command == "shot":
                directory = os.path.dirname(os.path.abspath(path))
                os.makedirs(directory, exist_ok=True)
                with open(path, "wb") as handle:
                    handle.write(payload)
                saved.append(path)
                return {"path": path, "bytes": len(payload), **meta}
            index += 1
            saved.append(save_frames(path, machine.alias, index,
                                     meta.get("format", args.format), payload))
            if count == 0 or index < count:
                await asyncio.sleep(1.0 / fps)
        return {"frames": len(saved), "bytes": total_bytes, "dir": os.path.join(path, machine.alias),
                "last": saved[-1] if saved else None}

    return await dispatch(machines, worker, serial=args.serial)


def main(argv=None) -> int:
    parser = build_parser()
    args = _fill_defaults(parser.parse_args(argv))
    try:
        registry = Registry.load(args.registry)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        if args.command == "machines":
            return _run_machines(args, registry)
        return asyncio.run(_run_action(args, registry))
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    except json.JSONDecodeError as exc:
        print(f"--args 不是合法 JSON: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"失败: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
