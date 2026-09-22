"""多机 (slave) 扇出的真机演示 —— 一台真 agent + 三张注册表条目。

目的: 证明 ``remote.ctl`` 的"控制多台"不是纸面设计。做法:

- 在本机起一个 WS 受控端 (127.0.0.1:8799, token=demo)
- 往一张临时注册表里塞 4 个别名: pc-a / pc-b / pc-lab 都指向这台真 agent,
  pc-dead 指向没人监听的 9999
- 用 ctl 的子命令真的跑一遍: ping -a / shot -a / pos -g office / windows -g office
- 看三件事: ① 并发扇出 (总耗时 << 逐台之和) ② 一台挂了不影响别的
  ③ 结果会回填 registry 的 online/offline

跑法 (仓库根目录)::

    .venv-win/Scripts/python.exe evidence/ctl/demo_fanout.py

只连回环地址, 只读写 build/ 下的临时目录, 不动用户真正的 ~/.kuuki/registry.json。
"""

import os
import pathlib
import socket
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
PORT = 8799
DEAD_PORT = 9999
OUT = ROOT / "build" / "ctl-demo"          # build/ 被 .gitignore 忽略, 用来放落盘产物
SHOTS = OUT / "shots"
REGISTRY = OUT / "registry.json"
LOG = OUT / "agent.log"

sys.path.insert(0, str(ROOT))


def wait_port(port: int, timeout: float = 25.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError(f"受控端没能在 {timeout}s 内监听 {port}")


def ctl(*argv: str) -> int:
    from remote.ctl import main

    print(f"\n$ ctl {' '.join(argv)}")
    return main(["--registry", str(REGISTRY), *argv])


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    server = subprocess.Popen(
        [sys.executable, "-m", "remote", "--ws", "--no-grpc", "--no-peerjs",
         "--ws-port", str(PORT), "--token", "demo"],
        cwd=str(ROOT), stdout=open(LOG, "w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )
    try:
        wait_port(PORT)
        print(f"受控端已起: ws://127.0.0.1:{PORT} (token=demo)")

        endpoint = f"ws://127.0.0.1:{PORT}"
        for alias, groups in (("pc-a", "office"), ("pc-b", "office"), ("pc-lab", "lab")):
            ctl("machines", "add", alias, "--transport", "ws", "--endpoint", endpoint,
                "--token", "demo", "-g", groups, "--force")
        # 一台注定连不上的, 用来看故障隔离
        ctl("machines", "add", "pc-dead", "--transport", "ws",
            "--endpoint", f"ws://127.0.0.1:{DEAD_PORT}", "--token", "demo",
            "-g", "office", "--timeout", "3", "--force")

        print("\n========== 1) 全部机器 ping: 一台挂了不影响别的 ==========")
        started = time.perf_counter()
        ctl("ping", "-a")
        print(f"(4 台并发扇出总耗时 {time.perf_counter() - started:.2f}s; "
              f"pc-dead 单独带 3s 超时)")

        print("\n========== 2) 只看 office 组 ==========")
        ctl("ping", "-g", "office")

        print("\n========== 3) 扇出截屏: 目录按别名分文件 ==========")
        ctl("shot", str(SHOTS), "-a", "--format", "jpeg", "--quality", "60",
            "--region", "0,0,320,200")
        if SHOTS.exists():
            for item in sorted(SHOTS.iterdir()):
                print(f"  {item.name}  {item.stat().st_size} bytes")

        print("\n========== 4) 扇出读状态: 鼠标坐标 / 窗口列表 ==========")
        ctl("pos", "-g", "office")
        ctl("windows", "-g", "office", "--limit", "2")

        print("\n========== 5) 结果回填进注册表 (online / offline) ==========")
        ctl("machines", "list")
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        print(f"\n受控端已停。日志: {LOG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
