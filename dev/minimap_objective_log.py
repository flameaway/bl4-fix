# Logs, twice a second for 15 minutes, the live objectives (objectives.py) next to the objective
# markers, route goal and route copy the minimap uses, on a change only (dev only). It reads plain
# Python data only, on its own thread (never `--thread`: that blocks the bridge until it ends):
#   python tools/bridge.py <id> -f dev/minimap_objective_log.py
# Output: .bridge/minimap_objectives.log
import math
import sys
import threading
import time
from pathlib import Path

LOG = Path(r"C:\Users\Josh\Documents\projects\bl4-direct\.bridge\minimap_objectives.log")
mm = sys.modules["bl4_minimap"]
D = sys.modules["bl4_minimap.mapdata"].MapData
O = sys.modules["bl4_minimap.objectives"].Objectives
B = sys.modules["bl4_minimap.breadcrumbs"].Breadcrumbs


def r(p):
    return (round(p[0] / 100), round(p[1] / 100)) if p else None


def _log():
    last = None
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"--- start {time.strftime('%H:%M:%S')}\n")
        end = time.monotonic() + 900
        while time.monotonic() < end:
            try:
                state = (
                    tuple(zip(map(r, O.points), O.types)),
                    tuple(((m.texture or "").rsplit(".", 1)[-1], r((m.x, m.y))) for m in mm.State.markers if m.kind == "objective"),
                    tuple(r(x) for x in (B.goal,)), len(mm.State.route),
                    tuple(r(rt[-1]) for rt in D.routes if rt), D.version,
                    tuple((i["texture"].rsplit(".", 1)[-1], r((i["x"], i["y"]))) for i in mm.State.icons if "mission" in i["texture"]),
                )
                if state != last:
                    last = state
                    f.write(f"{time.strftime('%H:%M:%S')} live={state[0]} markers={state[1]} goal={state[2]} dots={state[3]} "
                            f"echo_ends={state[4]} snap_v={state[5]} plain_mission_icons={state[6]}\n")
                    f.flush()
            except Exception as e:  # noqa: BLE001
                f.write(f"error {e!r}\n")
            time.sleep(0.5)
        f.write("--- end\n")


threading.Thread(target=_log, name="minimap_objective_log", daemon=True).start()
print("logging to", LOG)
