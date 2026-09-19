#!/usr/bin/env bash
# 生成 gRPC Python 存根 (remote/proto/kuuki_remote_pb2*.py)。
#
# 为什么需要它: 仓库 .gitignore 忽略 `*pb2.py` / `*pb2_grpc.py`, 但 gRPC 服务端
# 运行必须有存根。已用取反规则 `!remote/proto/*.py` 让 remote/proto/ 下的存根
# 可以入库; 换机器/改 proto 后重新跑本脚本即可。
#
# 用法 (仓库根目录):
#   bash remote/proto/gen_proto.sh
#
# 前提: 装了 grpcio-tools (见 requirements-remote.txt 里 "可选" 一节)。
# 注意: 生成物头部会写明它要求的 Protobuf Runtime 版本; 改完 proto 重新生成后,
#       记得同步 requirements-remote.txt 里的 protobuf 下限 —— 否则别人照
#       requirements 装出来的 runtime 会在 import 存根时被版本校验直接拒绝。
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 解释器: 优先用 $PYTHON, 否则依次找 python3 / python。
# (Windows 上通常只有 python, 没有 python3 —— 原来写死 python3 会直接失败。)
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            PY="$candidate"
            break
        fi
    done
fi
if [ -z "$PY" ]; then
    echo "找不到 python3 / python; 可以用 PYTHON=/path/to/python 显式指定" >&2
    exit 1
fi

# 传给 python 的路径要转成 Windows 形式: 在 Git Bash 下 $here 是 /d/xxx 这种
# MSYS 路径, Windows 原生的 python.exe 不认 (会报 "directory does not exist")。
# sed / ls 是 MSYS 程序, 继续用 $here 没问题。
PROTO_ARG="$here"
if command -v cygpath >/dev/null 2>&1; then
    PROTO_ARG="$(cygpath -w "$here")"
fi

"$PY" -m grpc_tools.protoc \
    -I "$PROTO_ARG" \
    --python_out="$PROTO_ARG" \
    --grpc_python_out="$PROTO_ARG" \
    "$PROTO_ARG/kuuki_remote.proto"

# grpc_tools 生成的是绝对导入 `import kuuki_remote_pb2`, 在包内 import 会失败;
# 改成相对导入 (只有这一行需要动)。GNU sed 与 BSD/macOS sed 的 -i 用法不同。
SED_EXPR='s/^import kuuki_remote_pb2 as kuuki__remote__pb2$/from . import kuuki_remote_pb2 as kuuki__remote__pb2/'
if sed --version >/dev/null 2>&1; then
    sed -i "$SED_EXPR" "$here/kuuki_remote_pb2_grpc.py"
else
    sed -i '' "$SED_EXPR" "$here/kuuki_remote_pb2_grpc.py"
fi

# 保证 proto/ 是个包, 相对导入才成立。
[ -f "$here/__init__.py" ] || printf '"""gRPC 生成存根 (由 gen_proto.sh 生成, 不要手改)。"""\n' > "$here/__init__.py"

echo "已生成:"
ls -1 "$here"/kuuki_remote_pb2*.py
