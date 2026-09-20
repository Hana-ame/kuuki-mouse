# 知识点索引

> 一个文件一个知识点，都是**踩过之后才记下来**的，不是设计文档的转写。
> 每条都写了「当时是什么现象 / 为什么 / 该怎么避开」，按前缀分组，也可以直接用
> `grep -rl <关键词> docs/knowledge/` 全文检索。

## 架构与产品定位

*改代码前该先知道的几条硬约束*

- [控制端不挑平台](arch-control-end-platform-agnostic.md)
- [一份实现、三种传输](arch-one-impl-three-transports.md)
- [`remote/win/` 是实验性死代码](arch-remote-win-dead-code.md)
- [截屏只有一个后端 PIL.ImageGrab](arch-screenshot-single-backend.md)
- [受控端只支持 Windows（产品定位）](arch-windows-only-controlled-end.md)

## 协议与 op 实现

*同一条 op 经三条传输必须等价，这里的坑都是这么抓出来的*

- [拖拽路径全程只按一次、只松一次](protocol-drag-single-press-release.md)
- [gRPC 无返回值的 RPC 用 `Ack{ok, message}` 兜底](protocol-grpc-ack-unwrap.md)
- [组合键要在客户端拆，不能在服务端拆](protocol-grpc-hotkey-split.md)
- [`MessageToDict` 会省略取默认值的字段](protocol-messagetodict-defaults.md)
- [【未修】`mouse.click` 静默忽略 `x` / `y`](protocol-mouse-click-ignores-xy.md)
- [proto3 普通标量分不清「显式给 0」与「缺省」](protocol-proto3-optional.md)
- [多步滚动要保持总量守恒](protocol-scroll-total-conservation.md)
- [「0 是合法值」的字段不能用 `x or default`](protocol-zero-is-valid-value.md)

## 传输开关与依赖

*三种传输按需开启之后的语义、校验与依赖分级*

- [gRPC 惰性导入：依赖按传输分级](transport-grpc-lazy-import.md)
- [三种传输「点名即选择」，默认只开 PeerJS](transport-point-to-choose.md)
- [房间码是配对用不是凭证；token 提示要按传输分三种](transport-room-code-not-credential.md)
- [启动前拦截：新语义才会出现的组合](transport-startup-guards.md)

## 网络 / ICE / PeerJS

*WebRTC 相关的对策与被 fork 的性质逼出来的格式*

- [应用层分块的三个 bug（同一轮里一起抓出来的）](net-app-layer-chunking.md)
- [ICE：必须剔掉已断开网卡的 link-local 地址](net-ice-linklocal-filter.md)
- [peerjs fork 的两个坑（不归我们，但影响判断）](net-peerjs-fork-pitfalls.md)
- [PeerJS 超时要比「实测握手时间」大，不能拍脑袋](net-peerjs-handshake-timeout.md)
- [`result or {}` 会吞掉合法的 falsy 返回值](net-result-or-empty-swallows-falsy.md)

## 控制端 ctl 与 registry

*多机编排：并发、原子写、权限、退出码*

- [argparse：让全局选项前后都能写](ctl-argparse-options-anywhere.md)
- [仿真集群必须跑在独立线程的事件循环里](ctl-dummy-swarm-thread.md)
- [exit 1（机器失败）≠ exit 2（用法错误）](ctl-exit-code-semantics.md)
- [`GrpcClient` 是同步的，并发分发要 `asyncio.to_thread`](ctl-grpc-client-sync.md)
- [registry 并发写会丢更新（已修：事务 + 原子写）](ctl-registry-atomic-write.md)
- [registry 里有 token 明文，文件权限要收紧](ctl-registry-permissions.md)
- [ctl 的目标只能走 `-t/-g/-a`，不能用位置参数](ctl-targets-by-flag.md)

## 打包与 CI

*英文 CI 上才暴露的问题占了一半*

- [判断模块有没有打进包，要看 PYZ toc](ci-check-module-in-pyz-toc.md)
- [同一段防御代码抄到第三份时，就该抽出来](ci-dedupe-defensive-code.md)
- [`requirements.txt` 曾缺 aiohttp（任何新环境一开 PeerJS 就崩）](ci-missing-aiohttp-dep.md)
- [PyInstaller：onedir + 路径基准](ci-pyinstaller-spec-gotchas.md)
- [CI shell 步骤：`set -uo pipefail` 缺了 `-e` 等于没有断言](ci-shell-step-pitfalls.md)
- [中文输出在非中文 Windows 上会崩](ci-utf8-stdio-before-parseargs.md)
- [CI workflow 里两处会「静默失效」的写法](ci-workflow-yaml-pitfalls.md)

## 文档维护

*让文档真的跟着代码走，而不是写一次就烂*

- [改行为时先 grep 正文的断言句，别只改代码块](docs-change-prose-not-only-code-blocks.md)
- [文档命令：parser 通过 ≠ 运行期能跑](docs-endpoint-format-unchecked.md)
- [照 README 逐条真跑一遍，比单元测试有用](docs-run-readme-end-to-end.md)
- [文档里的测试数量，是「文档有没有跟着代码走」的指示剂](docs-test-count-as-canary.md)
- [用真 parser 校验文档里的命令](docs-validate-commands-with-parser.md)

## Web 前端 / Pages

*手机配对页与 GitHub Pages 部署*

- [手机端 token 握手：连上先 `{"op":"auth"}`](web-auth-handshake-and-hash-token.md)
- [用 node + 最小 DOM 桩测试真的 `web/script.js`](web-node-dom-stub-test.md)
- [Pages 部署要加 `web/.nojekyll`](web-nojekyll-on-pages.md)
- [第三方库自托管到 `web/vendor/`，别用 CDN](web-vendor-libs-offline.md)
- [改完前端要在线上验，不能只看本地](web-verify-online-not-local.md)

## GUI 自动化实战

*用这套工具真实操控桌面的可行套路*

- [中文输入走剪贴板 + Ctrl+V，不要用逐字注入](gui-chinese-input-via-clipboard.md)
- [GUI 自动化的可靠套路：截图闭环](gui-screenshot-loop-and-coordinate-scale.md)
- [真人正在用这台电脑时，注入会互相干扰](gui-user-interference.md)

## 本机环境

*Windows / Git Bash 这一侧反复踩的坑*

- [测试里的 asyncio 规则](env-asyncio-test-rules.md)
- [后台进程与端口：三个反复踩的坑](env-background-process-and-port.md)
- [Git Bash / Windows 侧的零碎坑](env-bash-path-and-shell-quirks.md)
- [HTTPS push 会永久卡死，push 走 SSH](env-git-push-ssh.md)

## 未修 / 未做清单

*已知未修的 bug 与未做的验证，都在这儿*

- [已知未修 / 未做清单](todo-open-items.md)

---

共 **54 条**。新增知识点文件时按上述前缀命名，索引会自动接住它。
