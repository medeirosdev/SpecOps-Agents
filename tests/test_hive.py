from __future__ import annotations

import json
import os
import time
from pathlib import Path

from specops import hive as hive_mod
from specops.hive import Hive, default_root

from .conftest import PROJECT, SESSION_ID


def make_hive(root: Path, **kwargs) -> Hive:
    kwargs.setdefault("since", 0)
    return Hive(root=root, **kwargs)


def line(obj: dict) -> str:
    return json.dumps(obj) + "\n"


def prompt(text: str, cwd: str = "/w") -> dict:
    return {
        "type": "user",
        "message": {"role": "user", "content": text},
        "cwd": cwd,
        "timestamp": "2026-09-28T10:00:00Z",
    }


def test_default_root_respects_claude_config_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert default_root() == tmp_path / "projects"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert default_root() == Path.home() / ".claude" / "projects"


def test_missing_root_is_empty(tmp_path: Path) -> None:
    h = make_hive(tmp_path / "nope")
    assert h.poll() is False
    assert h.snapshot()["sessions"] == []


def test_discovers_sessions_and_subagents(projects: Path) -> None:
    h = make_hive(projects)
    assert h.poll() is True
    snap = h.snapshot(SESSION_ID)
    (s,) = snap["sessions"]
    assert s["id"] == SESSION_ID and s["project"] == "shop" and s["detail"] is True
    _main, sub = s["agents"]
    assert sub["name"] == "general-purpose" and sub["description"] == "Run the test 50 times"
    assert sub["parent_id"] == SESSION_ID
    assert h.poll() is False  # nothing new


def test_snapshot_without_detail_is_brief(projects: Path) -> None:
    h = make_hive(projects)
    h.poll()
    (s,) = h.snapshot()["sessions"]
    assert s["detail"] is False and "activities" not in s["agents"][0]


def test_follows_appended_lines_including_partial_writes(tmp_path: Path) -> None:
    project = tmp_path / "-w"
    project.mkdir()
    path = project / "sess.jsonl"
    path.write_text(line(prompt("first")))
    h = make_hive(tmp_path)
    h.poll()
    agent = h.sessions["sess"].main
    assert agent.last_prompt == "first"

    raw = line(prompt("second"))
    with path.open("a") as fh:
        fh.write(raw[:20])  # writer is mid-line
    assert h.poll() is True
    assert agent.last_prompt == "first"
    with path.open("a") as fh:
        fh.write(raw[20:])
    h.poll()
    assert agent.last_prompt == "second"


def test_bad_lines_are_skipped(tmp_path: Path) -> None:
    project = tmp_path / "-w"
    project.mkdir()
    (project / "s.jsonl").write_text("not json\n[1,2]\n" + line(prompt("ok")))
    h = make_hive(tmp_path)
    h.poll()
    assert h.sessions["s"].main.last_prompt == "ok"


def test_truncated_file_is_reread(tmp_path: Path) -> None:
    project = tmp_path / "-w"
    project.mkdir()
    path = project / "s.jsonl"
    path.write_text(line(prompt("a long first prompt")) * 3)
    h = make_hive(tmp_path)
    h.poll()
    path.write_text(line(prompt("new")))
    h.poll()
    main = h.sessions["s"].main
    assert main.last_prompt == "new"
    assert [a.text for a in main.activities] == ["new"]  # the old events were forgotten
    assert main.id == "s" and main.kind == "main"


def test_large_file_only_reads_the_tail(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(hive_mod, "INITIAL_TAIL_BYTES", 500)
    project = tmp_path / "-w"
    project.mkdir()
    lines = [line(prompt(f"prompt {i:03d}")) for i in range(50)]
    (project / "s.jsonl").write_text("".join(lines))
    h = make_hive(tmp_path)
    h.poll()
    prompts = [a.text for a in h.sessions["s"].main.activities]
    assert prompts[-1] == "prompt 049"
    assert 0 < len(prompts) < 50  # the head was skipped, and no half line was parsed


def test_since_and_project_filters(projects: Path, tmp_path: Path) -> None:
    other = projects / "-home-you-code-blog"
    other.mkdir()
    old = other / "old.jsonl"
    old.write_text(line(prompt("ancient")))
    week_ago = time.time() - 7 * 86400
    os.utime(old, (week_ago, week_ago))
    fresh = projects / PROJECT / f"{SESSION_ID}.jsonl"
    os.utime(fresh, None)

    h = make_hive(projects, since=3600)
    h.poll()
    assert set(h.sessions) == {SESSION_ID}

    h = make_hive(projects, since=0, project_filter="BLOG")
    h.poll()
    assert set(h.sessions) == {"old"}


def test_max_sessions_keeps_the_newest(tmp_path: Path) -> None:
    project = tmp_path / "-w"
    project.mkdir()
    for i in range(5):
        path = project / f"s{i}.jsonl"
        path.write_text(line(prompt(str(i))))
        os.utime(path, (1_000_000 + i, 1_000_000 + i))
    h = make_hive(tmp_path, max_sessions=2)
    h.poll()
    assert set(h.sessions) == {"s3", "s4"}


def test_new_subagent_appears_on_rescan(projects: Path, monkeypatch) -> None:
    h = make_hive(projects)
    h.poll()
    sub_dir = projects / PROJECT / SESSION_ID / "subagents"
    (sub_dir / "agent-zz.jsonl").write_text(line(prompt("late helper")))
    h._last_scan = 0  # skip the 2s rescan throttle
    h.poll()
    assert "zz" in h.sessions[SESSION_ID].subagents
    assert h.sessions[SESSION_ID].subagents["zz"].agent_type == ""  # no meta file: still shown


def test_listeners_and_version(projects: Path) -> None:
    h = make_hive(projects)
    calls = []
    h.on_change(lambda: calls.append(h.version))
    h.poll()
    h.poll()
    assert calls == [1]


def test_background_thread_start_stop(projects: Path) -> None:
    h = make_hive(projects)
    h.start(interval=0.01)
    deadline = time.time() + 5
    while not h.sessions and time.time() < deadline:
        time.sleep(0.01)
    h.stop()
    assert SESSION_ID in h.sessions


def test_only_the_tail_of_a_big_transcript_is_read(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(hive_mod, "INITIAL_TAIL_BYTES", 200)
    project = tmp_path / "-w"
    project.mkdir()
    (project / "big.jsonl").write_text(line(prompt("old " * 100)) + line(prompt("new")))
    (project / "small.jsonl").write_text(line(prompt("hi")))
    h = make_hive(tmp_path)
    h.poll()
    big, small = h.sessions["big"], h.sessions["small"]
    assert big.main.last_prompt == "new" and big.main.partial
    assert not small.main.partial
    sessions = {x["id"]: x for x in h.snapshot("big")["sessions"]}
    assert sessions["big"]["partial"] is True and sessions["small"]["partial"] is False
