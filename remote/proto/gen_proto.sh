#!/usr/bin/env bash
# 生成 gRPC Python 存根 (remote/proto/kuuki_remote_pb2*.py)。
#
# 为什么需要它: 仓库 .gitignore 忽略 `*pb2.py` / `*pb2_grpc.py`, 但 gRPC 服务端
# 运行必须有存根。已用取反规则 `!remote/proto/*.py` 让 remote/proto/ 下的存根
# 可以入库; 换机器/改 proto 后重新跑本脚本即可。
#
# 用法 (仓库根目录):
#   bash remote/proto/gen_proto.sh
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 -m grpc_tools.protoc \
    -I "$here" \
    --python_out="$here" \
    --grpc_python_out="$here" \
    "$here/kuuki_remote.proto"

# grpc_tools 生成的是绝对导入 `import kuuki_remote_pb2`, 在包内 import 会失败;
# 改成相对导入 (只有这一行需要动)。
sed -i 's/^import kuuki_remote_pb2 as kuuki__remote__pb2$/from . import kuuki_remote_pb2 as kuuki__remote__pb2/' \
    "$here/kuuki_remote_pb2_grpc.py"

# 保证 proto/ 是个包, 相对导入才成立。
[ -f "$here/__init__.py" ] || printf '"""gRPC 生成存根 (由 gen_proto.sh 生成, 不要手改)。"""\n' > "$here/__init__.py"

echo "已生成:"
ls -1 "$here"/kuuki_remote_pb2*.py
