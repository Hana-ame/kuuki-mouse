# `requirements.txt` 曾缺 aiohttp（任何新环境一开 PeerJS 就崩）

`peerjs/api.py` 顶层 `import aiohttp`，**但 aiohttp 不在 requirements.txt 里** ——
历史上 aiortc 会间接依赖它，而 **aiortc 1.11+ 不再带**，于是变成裸依赖。

为什么一直没暴露：本机 venv 是多年前手动攒的、里面有 aiohttp；CI 干净环境 + 
`check_bundle` 抓出来（CI 752 模块 vs 本机 856，差的就是它）。

**影响范围远不止打包**：任何人在新环境 `pip install -r requirements.txt` 之后开
PeerJS 都会崩。已补 `aiohttp>=3.9` + `PyYAML>=6.0`。

> 教训：本机 venv 越老，它「碰巧有依赖」的概率越高。判断依赖是否齐全，要看干净
> 环境跑出来的结果（CI），不要看本机。
