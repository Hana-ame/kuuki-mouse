"""ICE 本机候选地址过滤 —— 绕开断开的虚拟网卡。

问题现场（2026-09-19, Windows 侧跑 PeerJS 服务）
------------------------------------------------
Windows 上有多块网卡，其中两块是**已断开**的虚拟/外设网卡，却仍持有
link-local 地址（169.254.x.x）：

| 网卡 | 地址 | 状态 |
|---|---|---|
| Bluetooth Device (Personal Area Network) | 169.254.45.72 | Disconnected |
| TAP-Windows Adapter V9 (OpenVPN Connect) | 169.254.135.7 | Disconnected |
| 以太网（真实） | 192.168.1.2 | Up |

aioice 收集本机 ICE 候选时用 ``ifaddr.get_adapters()`` **枚举所有网卡地址、不做
可用性过滤**，于是它去 bind 那两个 169.254 地址，直接失败：

    aioice.ice: Connection(0) Could not bind to 169.254.135.7
                - [WinError 10049] 请求的地址无效
    aioice.ice: Connection(0) Could not bind to 169.254.45.72
                - [WinError 10049] 请求的地址无效

后果：本机候选不完整 → P2P 通道建不起来 → 控制端连接超时。而信令层一切正常
（broker 注册成功、对端也能被"接入"），所以症状具有误导性。

修法
----
``aioice.ice.get_host_addresses`` 是模块级函数，内部按全局名调用，因此可以在运行时
替换。这里包一层，把**不可用地址**滤掉：

* link-local ``169.254.0.0/16``（IPv4）/ ``fe80::/10``（IPv6）—— 自动配置地址，
  在多数环境里不可路由；断开网卡的残留地址就是这类。
* 环回（aioice 自己已排除，这里冗余保险）。

**只影响本机候选枚举**，不动 ICE 协议、不动远端候选、不要求改系统网卡设置。
可用环境变量 ``KUUKI_ICE_KEEP_LINKLOCAL=1`` 关闭本过滤（排障对照用）。
"""

from __future__ import annotations

import ipaddress
import logging
import os

log = logging.getLogger("kuuki.remote.ice")

__all__ = ["patch_aioice_addresses", "is_unusable_address"]

_patched = False


def is_unusable_address(address: str) -> bool:
    """判断一个本机地址是否应当从 ICE 候选中剔除。"""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True  # 解析不了的一律不要
    if ip.is_loopback:
        return True
    if ip.is_link_local:
        # IPv4 169.254.0.0/16 与 IPv6 fe80::/10 都走这里
        return True
    if ip.is_unspecified or ip.is_multicast:
        return True
    return False


def patch_aioice_addresses() -> bool:
    """替换 ``aioice.ice.get_host_addresses`` 以过滤不可用地址。

    返回是否成功打了补丁（aioice 不在或结构变化时返回 False，不影响主流程）。
    """
    global _patched
    if _patched:
        return True
    if os.environ.get("KUUKI_ICE_KEEP_LINKLOCAL"):
        log.info("KUUKI_ICE_KEEP_LINKLOCAL 已设, 跳过 ICE 地址过滤")
        return False

    try:
        import aioice.ice as ice
    except Exception as exc:  # pragma: no cover
        log.debug("aioice 不可用, 跳过地址过滤: %s", exc)
        return False

    original = getattr(ice, "get_host_addresses", None)
    if original is None:  # pragma: no cover - aioice 结构变了
        log.warning("aioice.ice.get_host_addresses 不存在, 跳过 ICE 地址过滤")
        return False

    def filtered(use_ipv4: bool, use_ipv6: bool):
        addresses = original(use_ipv4, use_ipv6)
        kept = [a for a in addresses if not is_unusable_address(a)]
        dropped = [a for a in addresses if is_unusable_address(a)]
        if dropped:
            log.info("ICE 本机候选已剔除不可用地址: %s (保留 %s)", dropped, kept)
        return kept

    ice.get_host_addresses = filtered
    _patched = True
    log.debug("已应用 aioice 本机候选地址过滤")
    return True
