"""Rules, profiles and teams: rendering agent files, publishing them, and keeping them in sync."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from specops.profiles import MANIFEST, Studio
from specops.skills import Library, SkillError, Target, parse_skill_md

GLOBAL_CLAUDE = "global:claude"
GLOBAL_AG = "global:antigravity"


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Path]:
    project = tmp_path / "code" / "shop"
    project.mkdir(parents=True)
    return {
        "home": tmp_path / "specops",
        "claude": tmp_path / "claude" / "skills",
        "gemini": tmp_path / "gemini" / "config" / "skills",
        "project": project,
    }


@pytest.fixture
def studio(env: dict[str, Path]) -> Studio:
    lib = Library(
        home=env["home"],
        projects=lambda: [str(env["project"])],
        targets=lambda: [
            Target("claude", "global", env["claude"]),
            Target("antigravity", "global", env["gemini"]),
        ],
    )
    lib.save("release-notes", "Write release notes", "1. Read the log.", True, "test")
    s = Studio(lib)
    s.save(
        "rule",
        {"name": "pt-br", "description": "Language", "body": "Answer in Portuguese."},
        True,
        "test",
    )
    s.save(
        "profile",
        {
            "name": "writer",
            "description": "Writes release notes",
            "prompt": "You write release notes.",
            "rules": ["pt-br"],
            "skills": ["release-notes"],
            "tools": ["Read", "Bash(git log:*)"],
            "model": {"claude": "haiku", "antigravity": "flash"},
        },
        True,
        "test",
    )
    return s


def statuses(item: dict) -> dict[str, str]:
    return {t["key"]: t["status"] for t in item["targets"]}


def agent_file(root: Path, name: str) -> tuple[dict[str, str], str]:
    return parse_skill_md((root.parent / "agents" / f"{name}.md").read_text())


def test_profile_renders_for_claude_with_rules_inlined(studio: Studio) -> None:
    text = studio.render("profile", "writer", "claude")
    meta, body = parse_skill_md(text)
    assert meta == {
        "name": "writer",
        "description": "Writes release notes",
        "model": "haiku",
        "tools": "Read, Bash(git log:*)",
        "skills": "",  # a YAML list: the simple parser only reads scalars
    }
    assert '  - "release-notes"' in text
    assert body.startswith("# writer\n\nYou write release notes.")
    assert "### pt-br\n\nAnswer in Portuguese." in body


def test_profile_for_antigravity_has_its_model_and_no_claude_fields(studio: Studio) -> None:
    meta, _ = parse_skill_md(studio.render("profile", "writer", "antigravity"))
    assert meta["model"] == "flash" and "tools" not in meta and "skills" not in meta


def test_publishing_a_profile_publishes_its_skills(studio: Studio, env: dict[str, Path]) -> None:
    out = studio.set_published("profile", "writer", GLOBAL_CLAUDE, True, False, "test")
    assert statuses(out)[GLOBAL_CLAUDE] == "on" and out["notes"] == []
    meta, _ = agent_file(env["claude"], "writer")
    assert meta["name"] == "writer"
    assert (env["claude"] / "release-notes" / "SKILL.md").is_file()
    manifest = json.loads((env["claude"].parent / "agents" / MANIFEST).read_text())
    assert manifest["writer"]["kind"] == "profile"


def test_changing_a_rule_updates_published_profiles(studio: Studio, env: dict[str, Path]) -> None:
    studio.set_published("profile", "writer", GLOBAL_AG, True, False, "test")
    studio.save(
        "rule", {"name": "pt-br", "description": "Language", "body": "Use pt-BR."}, False, "t"
    )
    _, body = agent_file(env["gemini"], "writer")
    assert "Use pt-BR." in body
    assert statuses(studio.describe("profile", "writer"))[GLOBAL_AG] == "on"


def test_unpublished_copies_are_not_resynced(studio: Studio, env: dict[str, Path]) -> None:
    studio.save("rule", {"name": "pt-br", "description": "Language", "body": "Other."}, False, "t")
    assert not (env["claude"].parent / "agents" / "writer.md").exists()


def test_team_publishes_its_members_and_lists_them(studio: Studio, env: dict[str, Path]) -> None:
    team = {
        "name": "docs",
        "description": "Docs squad",
        "prompt": "Ship docs.",
        "members": ["writer"],
    }
    studio.save("team", team, True, "test")
    studio.set_published("team", "docs", GLOBAL_AG, True, False, "test")
    text = (env["gemini"].parent / "agents" / "docs.md").read_text()
    assert '  - "./writer.md"' in text and "- **writer**: Writes release notes" in text
    assert "invoke_subagent" in text
    assert (env["gemini"].parent / "agents" / "writer.md").is_file()
    claude = studio.render("team", "docs", "claude")
    assert "subagent_type" in claude and "agents:" not in claude


def test_profile_and_team_share_one_namespace(studio: Studio) -> None:
    studio.save("profile", {"name": "editor", "description": "d", "prompt": "p"}, True, "t")
    with pytest.raises(SkillError, match="already called"):
        studio.save(
            "team", {"name": "writer", "description": "x", "members": ["editor"]}, True, "t"
        )


def test_references_must_exist(studio: Studio) -> None:
    base = {"name": "p2", "description": "d", "prompt": "p"}
    with pytest.raises(SkillError, match="no such rule"):
        studio.save("profile", base | {"rules": ["nope"]}, True, "t")
    with pytest.raises(SkillError, match="no such skill"):
        studio.save("profile", base | {"skills": ["nope"]}, True, "t")
    with pytest.raises(SkillError, match="no such profile"):
        studio.save("team", {"name": "t", "description": "d", "members": ["nope"]}, True, "t")
    with pytest.raises(SkillError, match="tools"):
        studio.save("profile", base | {"tools": ["Bash, rm"]}, True, "t")
    with pytest.raises(SkillError, match="model"):
        studio.save("profile", base | {"model": {"claude": "x\ny"}}, True, "t")


def test_in_use_items_cant_be_deleted(studio: Studio) -> None:
    with pytest.raises(SkillError, match="still used by profiles: writer"):
        studio.delete("rule", "pt-br", False, "t")
    studio.save("team", {"name": "docs", "description": "d", "members": ["writer"]}, True, "t")
    with pytest.raises(SkillError, match="still used by teams: docs"):
        studio.delete("profile", "writer", False, "t")


def test_never_touches_an_agent_it_did_not_write(studio: Studio, env: dict[str, Path]) -> None:
    agents = env["claude"].parent / "agents"
    agents.mkdir(parents=True)
    (agents / "writer.md").write_text("mine")
    assert statuses(studio.describe("profile", "writer"))[GLOBAL_CLAUDE] == "conflict"
    with pytest.raises(SkillError, match="left alone"):
        studio.set_published("profile", "writer", GLOBAL_CLAUDE, True, False, "t")
    assert (agents / "writer.md").read_text() == "mine"


def test_hand_edits_need_force(studio: Studio, env: dict[str, Path]) -> None:
    studio.set_published("profile", "writer", GLOBAL_CLAUDE, True, False, "t")
    path = env["claude"].parent / "agents" / "writer.md"
    path.write_text(path.read_text() + "\nmore")
    assert statuses(studio.describe("profile", "writer"))[GLOBAL_CLAUDE] == "modified"
    with pytest.raises(SkillError, match="edited outside"):
        studio.set_published("profile", "writer", GLOBAL_CLAUDE, False, False, "t")
    with pytest.raises(SkillError, match="edited outside"):
        studio.delete("profile", "writer", False, "t")
    studio.delete("profile", "writer", True, "t")
    assert not path.exists()


def test_project_targets_only_for_known_projects(studio: Studio, env: dict[str, Path]) -> None:
    key = f"project:claude:{env['project']}"
    studio.set_published("profile", "writer", key, True, False, "t")
    assert (env["project"] / ".claude" / "agents" / "writer.md").is_file()
    assert (env["project"] / ".claude" / "skills" / "release-notes").is_dir()
    with pytest.raises(SkillError, match="unknown project"):
        studio.set_published("profile", "writer", "project:claude:/etc", True, False, "t")


def test_unpublish_removes_file_and_manifest_entry(studio: Studio, env: dict[str, Path]) -> None:
    studio.set_published("profile", "writer", GLOBAL_CLAUDE, True, False, "t")
    out = studio.set_published("profile", "writer", GLOBAL_CLAUDE, False, False, "t")
    agents = env["claude"].parent / "agents"
    assert statuses(out)[GLOBAL_CLAUDE] == "off" and not (agents / "writer.md").exists()
    assert "writer" not in json.loads((agents / MANIFEST).read_text())


def test_list_and_audit(studio: Studio) -> None:
    listing = studio.list()
    assert [r["name"] for r in listing["rules"]] == ["pt-br"]
    assert listing["rules"][0]["used_by"] == ["writer"]
    assert [p["name"] for p in listing["profiles"]] == ["writer"]
    assert studio.agent_names() == {"writer": "profile"}
    kinds = {(a.get("kind"), a["action"]) for a in studio.lib.audit()}
    assert ("profile", "create") in kinds and ("rule", "create") in kinds
