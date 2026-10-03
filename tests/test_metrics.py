"""Usage metrics: grouping agents and the numbers compared."""

from __future__ import annotations

import pytest

from specops import metrics
from specops.cli import main
from specops.model import Agent, Session

from .test_model import assistant, tool_result, tool_use, user


def worker(agent_id: str, kind: str, model: str = "claude-haiku-4-5") -> Agent:
    a = Agent(id=agent_id, session_id="s", kind="sub", agent_type=kind)
    a.ingest(user("go", at=0))
    for i in range(3):  # the same search three times: two retries
        msg = assistant(
            tool_use(f"{agent_id}{i}", "Grep", pattern="x"), at=i + 1, mid=f"{agent_id}{i}"
        )
        msg["message"]["model"] = model
        msg["message"]["usage"] = {"input_tokens": 100, "output_tokens": 10}
        a.ingest(msg)
        a.ingest(user(tool_result(f"{agent_id}{i}", "no", error=i == 0), at=i + 1.5))
    return a


def squad() -> Session:
    main_agent = Agent(id="s", session_id="s", kind="main")
    main_agent.ingest(user("hi", at=0, cwd="/code/shop"))
    subs = {"a": worker("a", "writer"), "b": worker("b", "writer"), "c": worker("c", "Explore")}
    return Session(id="s", project_dir="p", main=main_agent, subagents=subs)


def test_groups_by_profile_and_marks_specops_profiles() -> None:
    report = metrics.collect([squad()], by="profile", profiles={"writer": "profile"})
    groups = {g["key"]: g for g in report["groups"]}
    assert set(groups) == {"writer", "Explore", "Claude (main)"}
    writer = groups["writer"]
    assert (writer["agents"], writer["tool_calls"], writer["retries"]) == (2, 6, 4)
    assert writer["error_rate"] == pytest.approx(2 / 6)
    assert writer["tokens_out"] == 60 and writer["cost"] == pytest.approx(2 * 3 * 150e-6)
    origins = {r["profile"]: r["origin"] for r in report["agents"]}
    assert origins == {"Claude (main)": "main", "writer": "profile", "Explore": "built-in"}
    assert report["totals"]["agents"] == 4 and report["totals"]["sessions"] == 1


def test_groups_by_model_and_project() -> None:
    by_model = metrics.collect([squad()], by="model")
    assert {g["key"] for g in by_model["groups"]} == {"claude-haiku-4-5", "unknown"}
    by_project = metrics.collect([squad()], by="project")
    assert [g["key"] for g in by_project["groups"]] == ["shop"]
    with pytest.raises(ValueError):
        metrics.collect([], by="color")


def test_table_has_a_row_per_group_and_a_total() -> None:
    text = metrics.table(metrics.collect([squad()], by="profile"))
    lines = text.splitlines()
    assert lines[0].split()[:2] == ["profile", "agents"] and lines[-1].startswith("total")


def test_cli_prints_json(projects, capsys, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SPECOPS_HOME", str(tmp_path / "home"))
    assert (
        main(["metrics", "--root", str(projects), "--since", "all", "--json", "--no-antigravity"])
        == 0
    )
    import json

    report = json.loads(capsys.readouterr().out)
    assert report["by"] == "profile" and report["groups"] and "agents" not in report
