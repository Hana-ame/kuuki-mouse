# gRPC 惰性导入：依赖按传输分级

`remote/grpc_server.py` 顶层就 `import grpc`，而 **grpcio 连 `requirements.txt` 里
都没写**（只在 `requirements-remote.txt`）。顶层导入意味着「只开 PeerJS 的默认用法」
会因为没装 grpcio 直接 `ImportError` —— 在默认改成只开 PeerJS 之后这就讲不通了。

解法：`remote/__main__.py` 里改成**按需导入**，只有解析出要开 gRPC 时才 import。
验证手段：

```python
import sys, remote.__main__
assert "grpc" not in sys.modules          # 默认路径不该引入 grpc
```

**不要顺手把 `websockets` 也省掉** —— `peerjs/socket.py` 顶层 import 它连 broker，
是默认路径的真依赖。区分「真依赖」和「某条可选路径才需要的依赖」，是这次依赖
注释的重点。
