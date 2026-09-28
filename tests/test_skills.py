"""The skill library: saving, publishing to agent folders, and never touching what isn't ours."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from specops.skills import MARKER, Library, SkillError, Target, parse_skill_md, render_skill_md

BODY = "# Release notes\n\n1. Read the git log.\n2. Group changes by area."


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
def lib(env: dict[str, Path]) -> Library:
    return Library(
        home=env["home"],
        projects=lambda: [str(env["project"]), "relative/path", str(env["project"] / "missing")],
        targets=lambda: [
            Target("claude", "global", env["claude"]),
            Target("antigravity", "global", env["gemini"]),
        ],
    )


def statuses(skill: dict) -> dict[str, str]:
    return {t["key"]: t["status"] for t in skill["targets"]}


def test_render_and_parse_round_trip() -> None:
    desc = 'Use when asked for "release notes":\nsummarise the log.'
    meta, body = parse_skill_md(render_skill_md("notes", desc, BODY))
    assert meta == {"name": "notes", "description": desc} and body == BODY


def test_parses_block_scalars_from_other_tools() -> None:
    text = (
        "---\nname: xlsx\ndescription: >-\n  Use for spreadsheets.\n  Not for docs.\n"
        "license: MIT\n---\n\nBody"
    )
    meta, body = parse_skill_md(text)
    assert meta["description"] == "Use for spreadsheets. Not for docs." and meta["license"] == "MIT"
    assert body == "Body"


@pytest.mark.parametrize("name", ["", "Bad", "../etc", "a/b", "-x", "x" * 65, "white space"])
def test_rejects_bad_names(lib: Library, name: str) -> None:
    with pytest.raises(SkillError):
        lib.save(name, "desc", BODY, True, "test")


def test_validation(lib: Library) -> None:
    with pytest.raises(SkillError, match="description"):
        lib.save("notes", " ", BODY, True, "test")
    with pytest.raises(SkillError, match="instructions"):
        lib.save("notes", "desc", "", True, "test")
    with pytest.raises(SkillError, match="KB"):
        lib.save("notes", "desc", "x" * 300_000, True, "test")


def test_create_update_and_duplicates(lib: Library, env: dict[str, Path]) -> None:
    skill = lib.save("notes", "Write release notes", BODY, True, "test")
    assert skill["name"] == "notes" and skill["body"] == BODY
    assert (env["home"] / "skills" / "notes" / "SKILL.md").is_file()
    with pytest.raises(SkillError) as err:
        lib.save("notes", "again", BODY, True, "test")
    assert err.value.status == 409
    with pytest.raises(SkillError) as err:
        lib.save("other", "desc", BODY, False, "test")
    assert err.value.status == 404
    assert [s["name"] for s in lib.list()] == ["notes"]


def test_publish_sync_and_unpublish(lib: Library, env: dict[str, Path]) -> None:
    lib.save("notes", "Write release notes", BODY, True, "test")
    skill = lib.set_published("notes", "global:claude", True, False, "test")
    dest = env["claude"] / "notes"
    assert statuses(skill) == {"global:claude": "on", "global:antigravity": "off"}
    assert json.loads((dest / MARKER).read_text())["source"] == "specops"

    lib.save("notes", "Write release notes", BODY + "\n3. Link PRs.", False, "test")
    assert "Link PRs" in (dest / "SKILL.md").read_text()  # the copy followed the edit
    assert statuses(lib.list()[0])["global:claude"] == "on"

    lib.set_published("notes", "global:claude", False, False, "test")
    assert not dest.exists()
    actions = [a["action"] for a in lib.audit()]
    assert actions == ["unpublish", "update", "publish", "create"]


def test_copy_edited_elsewhere_needs_force(lib: Library, env: dict[str, Path]) -> None:
    lib.save("notes", "Write release notes", BODY, True, "test")
    lib.set_published("notes", "global:antigravity", True, False, "test")
    copy = env["gemini"] / "notes" / "SKILL.md"
    copy.write_text(copy.read_text() + "\nhand edit\n")
    assert statuses(lib.list()[0])["global:antigravity"] == "modified"

    lib.save("notes", "Write release notes", BODY + "\nmore", False, "test")
    assert "hand edit" in copy.read_text()  # an edited copy isn't silently overwritten
    with pytest.raises(SkillError) as err:
        lib.set_published("notes", "global:antigravity", True, False, "test")
    assert err.value.status == 409
    lib.set_published("notes", "global:antigravity", True, True, "test")
    assert "hand edit" not in copy.read_text() and "more" in copy.read_text()


def test_never_touches_foreign_skills(lib: Library, env: dict[str, Path]) -> None:
    foreign = env["claude"] / "notes"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("---\nname: notes\ndescription: mine\n---\nmine")
    lib.save("notes", "Write release notes", BODY, True, "test")
    assert statuses(lib.list()[0])["global:claude"] == "conflict"
    with pytest.raises(SkillError, match="left alone"):
        lib.set_published("notes", "global:claude", True, True, "test")
    lib.delete("notes", True, "test")
    assert (foreign / "SKILL.md").read_text().endswith("mine")


def test_symlinked_destination_is_a_conflict(
    lib: Library, env: dict[str, Path], tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    env["claude"].mkdir(parents=True)
    (env["claude"] / "notes").symlink_to(elsewhere)
    lib.save("notes", "Write release notes", BODY, True, "test")
    with pytest.raises(SkillError):
        lib.set_published("notes", "global:claude", True, True, "test")
    assert list(elsewhere.iterdir()) == []


def test_projects(lib: Library, env: dict[str, Path]) -> None:
    project = str(env["project"])
    assert lib.projects() == [project]  # relative and missing paths are dropped
    lib.save("notes", "Write release notes", BODY, True, "test")
    for agent in ("claude", "antigravity"):
        lib.set_published("notes", f"project:{agent}:{project}", True, False, "test")
    assert (env["project"] / ".claude" / "skills" / "notes" / "SKILL.md").is_file()
    assert (env["project"] / ".agents" / "skills" / "notes" / "SKILL.md").is_file()
    assert statuses(lib.list()[0])[f"project:claude:{project}"] == "on"
    with pytest.raises(SkillError, match="unknown project"):
        lib.set_published("notes", "project:claude:/etc", True, False, "test")

    lib.delete("notes", False, "test")
    assert not (env["project"] / ".claude" / "skills" / "notes").exists()
    assert not (env["home"] / "skills" / "notes").exists()


def test_home_is_never_a_project(env: dict[str, Path]) -> None:
    lib = Library(home=env["home"], projects=lambda: [str(Path.home()), "/"])
    assert lib.projects() == []


def test_import_external(lib: Library, env: dict[str, Path], tmp_path: Path) -> None:
    src = env["claude"] / "review"
    (src / "scripts").mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: review\ndescription: Review a PR\n---\nSteps")
    (src / "scripts" / "run.sh").write_text("echo hi")
    secret = tmp_path / "secret.txt"
    secret.write_text("do not copy")
    (src / "link").symlink_to(secret)
    (src / "scripts" / "nested-link").symlink_to(secret)

    assert [x["name"] for x in lib.external()] == ["review"]
    skill = lib.import_skill("global:claude", "review", "test")
    assert skill["description"] == "Review a PR"
    copied = env["home"] / "skills" / "review"
    assert (copied / "scripts" / "run.sh").is_file()
    assert not (copied / "link").exists() and not (copied / "scripts" / "nested-link").exists()
    assert statuses(skill)["global:claude"] == "conflict"  # the original stays theirs
    with pytest.raises(SkillError):
        lib.import_skill("global:claude", "review", "test")
