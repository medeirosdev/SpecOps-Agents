"""Profiles, teams and rules over HTTP, the metrics endpoint, and the starter agents."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from specops import starters
from specops.starters import add_starters, seed, starter_names

from .test_web_skills import Server, start


@pytest.fixture
def srv(projects: Path, tmp_path: Path) -> Iterator[Server]:
    yield from start(projects, tmp_path)


def test_studio_writes_need_the_unlock_token(srv: Server) -> None:
    item = {"name": "pt-br", "description": "Language", "body": "Answer in Portuguese."}
    status, _ = srv.post("/api/studio/create", {"kind": "rule", "item": item})
    assert status == 401
    token = srv.unlock()
    status, out = srv.post("/api/studio/create", {"kind": "rule", "item": item}, token)
    assert status == 200 and out["name"] == "pt-br"
    status, _ = srv.post("/api/studio/create", {"kind": "rule", "item": item}, Origin="http://evil")
    assert status == 403


def test_profile_round_trip_and_publish(srv: Server, tmp_path: Path) -> None:
    token = srv.unlock()
    profile = {"name": "rev", "description": "Reviews", "prompt": "Review the diff."}
    assert srv.post("/api/studio/create", {"kind": "profile", "item": profile}, token)[0] == 200
    body = {"kind": "profile", "name": "rev", "target": "global:claude", "on": True}
    status, _ = srv.post("/api/studio/publish", body, token)
    assert status == 200 and (tmp_path / "agents" / "rev.md").is_file()
    status, _, state = srv.request("GET", "/api/studio")
    assert status == 200 and [p["name"] for p in state["profiles"]] == ["rev"]
    assert state["starters"] == starter_names()
    status, _ = srv.post("/api/studio/delete", {"kind": "profile", "name": "rev"}, token)
    assert status == 200 and not (tmp_path / "agents" / "rev.md").exists()


def test_studio_errors(srv: Server) -> None:
    token = srv.unlock()
    assert srv.post("/api/studio/create", {"kind": "planet", "item": {}}, token)[0] == 400
    assert srv.post("/api/studio/create", {"kind": "rule"}, token)[0] == 400
    status, out = srv.post("/api/studio/create", {"kind": "team", "item": {"name": "t"}}, token)
    assert status == 400 and "description" in out["error"]
    assert srv.post("/api/studio/nope", {"kind": "rule"}, token)[0] == 404


def test_starters_are_added_once(srv: Server) -> None:
    token = srv.unlock()
    status, out = srv.post("/api/studio/starters", {}, token)
    expected = [r["name"] for r in starters.RULES] + [p["name"] for p in starters.PROFILES]
    assert status == 200 and out["created"] == expected + [t["name"] for t in starters.TEAMS]
    assert srv.post("/api/studio/starters", {}, token)[1]["created"] == []
    text = srv.skills.studio.render("profile", "usage-optimizer", "claude")
    assert "model: haiku" in text and "### propose-dont-apply" in text


def test_starter_teams_render_their_rosters(srv: Server) -> None:
    studio = srv.skills.studio
    add_starters(studio, "t")
    dev = studio.render("team", "dev-team", "claude")
    for member in ("architect", "implementer", "test-engineer", "code-reviewer", "docs-writer"):
        assert f"- **{member}**:" in dev
    research = studio.render("team", "research", "antigravity")
    assert '  - "./fact-checker.md"' in research
    assert "### safe-git" in studio.render("profile", "implementer", "claude")
    assert "tools:" not in studio.render("profile", "implementer", "claude")  # all tools


def test_starters_skip_names_taken(srv: Server) -> None:
    studio = srv.skills.studio
    studio.save(
        "profile", {"name": "usage-review", "description": "mine", "prompt": "p"}, True, "t"
    )
    assert "usage-review" not in add_starters(studio, "t")
    assert studio.get("profile", "usage-review")["description"] == "mine"


def test_seed_adds_once_and_respects_deletions(srv: Server) -> None:
    studio = srv.skills.studio
    assert "dev-team" in seed(studio)
    studio.delete("team", "dev-team", False, "t")
    assert seed(studio) == []
    assert "dev-team" not in studio.names("team")
    assert add_starters(studio, "t") == ["dev-team"]  # the button brings it back


def test_metrics_endpoint(srv: Server) -> None:
    status, _, report = srv.request("GET", "/api/metrics?by=model")
    assert status == 200 and report["by"] == "model" and "agents" not in report
    assert isinstance(report["groups"], list) and "cost" in report["totals"]
    assert srv.request("GET", "/api/metrics?by=color")[0] == 400
