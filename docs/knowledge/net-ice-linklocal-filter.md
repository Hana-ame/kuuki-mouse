# ICE：必须剔掉已断开网卡的 link-local 地址

Windows 上**已经断开**的虚拟网卡（蓝牙 PAN / OpenVPN TAP / Hyper-V 之类）仍然持有
`169.254.x.x` 这类 link-local 地址。ICE 收集本机候选时会把它们一起发出去，对端连它们
必然超时。

**症状是「跨机连不上、同机却好好的」** —— 同机走 host 候选能直连，跨机才用得上这些
坏地址，于是失败。极难定位，因为地址本身看起来完全合法。

`remote/ice.py::is_unusable_address` 负责剔除：

- 回环、link-local（IPv4 + IPv6 的 `fe80::`）、未指定地址、组播、解析不了的，一律剔除
- **真实局域网地址必须保留**（192.168 / 10.x / 公网）—— 留不住就真没候选可用了

排障后门 `KUUKI_ICE_KEEP_LINKLOCAL` 可以关掉过滤，用来确认是不是这层误杀。

> 测试注意：它是个幂等开关（`_patched` 标记），**测前必须复位 `_patched`**，
> 否则这条分支永远走不到 —— 测试会假绿。

这个模块曾经**零测试覆盖**（grep 搜 `ice` 会误匹配 `device` / `service`，要用
`remote.ice` 或 `patch_aioice_addresses` 精确搜才查得出），现已补 13 项。
