"""在本机桌面上**本地**跑一次校准, 并把每个点的认标记过程打出来。

为什么不走 WS: 报告里只有结论, "这一帧里有哪些红色候选、选了哪个"属于过程,
只有服务端进程里的 ``reporter`` 回调拿得到。本机就是受控端, 本地跑等价且省事。

用法::

    python -u evidence/calibration/trace_local.py [cols] [rows] [max_width]
"""

import sys

from remote.calibrate import Calibration, run
from remote.input import InputController
from remote.screen import ScreenCapture


def main() -> int:
    cols = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    rows = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    max_width = int(sys.argv[3]) if len(sys.argv) > 3 else 1280

    def reporter(kind: str, payload: dict) -> None:
        screen = payload["screen"]
        detail = payload["detail"]
        changed = detail["changed"]
        accepted = [c for c in detail["candidates"] if c["accepted"]]
        where = payload["frame"]
        marker = f"({where['x']:.1f},{where['y']:.1f})" if where else "没认到"
        print(f"\n[{kind}] 鼠标 ({screen['x']:.0f},{screen['y']:.0f}) -> 图上 {marker}")
        print(f"    帧差 {len(changed)} 处: "
              f"{[(c['x'], c['y'], c['w'], c['h']) for c in changed][:6]}")
        print(f"    红候选 {len(detail['candidates'])} 个, 其中尺寸合格 {len(accepted)} 个: "
              f"{[(c['x'], c['y'], c['w'], c['h'], c['pixels']) for c in accepted][:6]}")

    report = run(
        ScreenCapture(), InputController(),
        cols=cols, rows=rows, max_width=max_width, settle=0.2,
        reporter=reporter,
    )
    print(f"\nverdict={report['verdict']} sampled={report['sampled']}/"
          f"{report['requested']}")
    print(f"declared_scale={report.get('declared_scale')} "
          f"fit_scale={report.get('fit_scale')} residual={report.get('residual')}")
    print(f"origin={report.get('origin')}")
    if report.get("calibration"):
        calib = Calibration.from_dict(report["calibration"])
        print(f"calibration={calib.to_dict()}")
    print(f"advice: {report.get('advice')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
