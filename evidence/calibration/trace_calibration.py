"""把一次校准的**过程**打到屏幕上 (残差大的时候报告只说结论, 这里是过程)。

用法::

    python -u evidence/calibration/trace_calibration.py [cols] [rows] [max_width]
"""

import asyncio
import sys

from remote.client import WsClient

URL = "ws://127.0.0.1:8765/"


async def main() -> int:
    cols = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    rows = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    max_width = int(sys.argv[3]) if len(sys.argv) > 3 else 1280

    async with WsClient(URL, timeout=60) as client:
        info = await client.call("info", {})
        print(f"受控端: {info.get('hostname')} / {info.get('platform')}")
        report = await client.call(
            "screen.calibrate",
            {"cols": cols, "rows": rows, "max_width": max_width, "settle": 0.2},
        )
        print(f"verdict={report['verdict']} sampled={report['sampled']}/"
              f"{report['requested']}  residual={report.get('residual')}")
        print(f"declared_scale={report.get('declared_scale')} "
              f"fit_scale={report.get('fit_scale')}")
        print(f"advice: {report.get('advice')}")

        # 逐点复算: 期望的图坐标 vs 实际认到的
        declared = report["declared_scale"]
        print("\n点\t\t鼠标坐标\t期望图坐标\t认到的图坐标\t偏差")
        for item in report.get("samples", []):
            sx, sy = item["screen"]["x"], item["screen"]["y"]
            fx, fy = item["frame"]["x"], item["frame"]["y"]
            ex, ey = sx / declared, sy / declared
            print(f"\t\t({sx:6.0f},{sy:6.0f})\t({ex:6.1f},{ey:6.1f})\t"
                  f"({fx:6.1f},{fy:6.1f})\t{((fx - ex) ** 2 + (fy - ey) ** 2) ** 0.5:7.1f}")
        for miss in report.get("missed", []):
            spot = miss["screen"]
            print(f"\t\t({spot['x']:6.0f},{spot['y']:6.0f})\t没认到标记  "
                  f"changed={miss.get('changed')} candidates={miss.get('candidates')} "
                  f"accepted={miss.get('accepted')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
