from __future__ import annotations

import time

from specops.demo import Demo
from specops.hive import Hive


def test_demo_colony_is_readable_by_the_hive() -> None:
    demo = Demo(speed=40)
    root = demo.start()
    hive = Hive(root=root, since=0)
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            hive.poll()
            subs = [a for s in hive.sessions.values() for a in s.subagents.values()]
            if len(hive.sessions) == 4 and any(a.phase == "done" for a in subs):
                break
            time.sleep(0.05)
    finally:
        demo.stop()

    projects = {s.project_name() for s in hive.sessions.values()}
    assert projects == {"dashboard", "api-server", "blog", "billing"}
    snap = hive.snapshot()
    assert all(s["title"] for s in snap["sessions"])
    subs = [a for s in hive.sessions.values() for a in s.subagents.values()]
    assert subs and all(a.parent_id for a in subs)
