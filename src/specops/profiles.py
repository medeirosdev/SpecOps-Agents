"""Rules, profiles and teams: agents assembled from the skill library, for both CLIs.

* A **rule** is a standing instruction ("never push to main", "answer in Portuguese").
* A **profile** is an agent: a prompt, the rules it follows, the skills it uses, and the model
  it runs on in each CLI.
* A **team** is a lead agent that hands work to member profiles.

They live in ``~/.specops/{rules,profiles,teams}/`` and are *published* as agent definition files,
next to the skill folders of the same agent and scope:

=============  ==========================================  ==================================
agent          global                                      per project
=============  ==========================================  ==================================
Claude Code    ``~/.claude/agents/<name>.md``              ``<project>/.claude/agents``
Antigravity    ``~/.gemini/config/agents/<name>.md``       ``<project>/.agents/agents``
=============  ==========================================  ==================================

Both read a markdown file with YAML frontmatter (``name``, ``description``, ``model``, ...) whose
body is the agent's prompt. Rules are written into that body, so they need no file of their own;
a profile's skills are published from the library to the same place as the profile, and a team's
members are published along with it.

An agent definition is a single file, so what SpecOps wrote is recorded in a
``.specops-agents.json`` manifest in each folder, with the hash of each file. As with skills, a
file that isn't in the manifest is never touched, and one edited since it was published is only
overwritten or removed when explicitly forced.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from pathlib import Path
from typing import Any

from .skills import (
    MAX_BODY,
    MAX_DESCRIPTION,
    NAME,
    Library,
    SkillError,
    Target,
    _hash,
    _write_atomic,
    parse_skill_md,
    project_targets,
    render_skill_md,
)

KINDS = ("rule", "profile", "team")
AGENT_KINDS = ("profile", "team")
MANIFEST = ".specops-agents.json"
MAX_ITEMS = 64
MODEL = re.compile(r"^[A-Za-z0-9][\w.:\[\]-]{0,79}$")
# Claude Code tool names, with an optional argument pattern: Read, Bash(git log:*), mcp__x__y.
TOOL = re.compile(r"^[A-Za-z][\w.-]{0,99}(\([^(),\n]{1,200}\))?$")
CLIS = ("claude", "antigravity")
STATUS_NOTE = {"conflict": "another agent has this name", "modified": "edited outside SpecOps"}

DELEGATE = {
    "claude": (
        "Hand work to these agents with the Agent tool, passing the member's name as "
        "`subagent_type`. Give each one a self-contained task, run independent tasks in "
        "parallel, and combine what they report."
    ),
    "antigravity": (
        "Hand work to these subagents with `invoke_subagent`. Give each one a self-contained "
        "task, run independent tasks in parallel, and combine what they report."
    ),
}


def agent_target(skills: Target) -> Target:
    """Where agent definitions go for the agent and scope of a skills folder: right beside it."""
    return Target(skills.agent, skills.scope, skills.root.parent / "agents", skills.project)


def _frontmatter(fields: list[tuple[str, Any]]) -> list[str]:
    lines = ["---"]
    for key, value in fields:
        if value in ("", None, []):
            continue
        if isinstance(value, list):
            # JSON strings are valid YAML double-quoted scalars.
            lines += [f"{key}:", *(f"  - {json.dumps(v)}" for v in value)]
        elif key == "description":
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        else:
            lines.append(f"{key}: {value}")
    return [*lines, "---", ""]


def _text(value: Any, label: str, limit: int, required: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise SkillError(f"{label} must be text")
    if required and not value.strip():
        raise SkillError(f"{label} is required")
    if len(value) > limit if limit == MAX_DESCRIPTION else len(value.encode()) > limit:
        size = f"{limit} characters" if limit == MAX_DESCRIPTION else f"{limit // 1024} KB"
        raise SkillError(f"{label} is longer than {size}")
    if "\x00" in value:
        raise SkillError(f"{label} contains a NUL character")
    return value


def _names(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if (
        not isinstance(value, list)
        or len(value) > MAX_ITEMS
        or not all(isinstance(v, str) and NAME.match(v) for v in value)
    ):
        raise SkillError(f"{label}: a list of up to {MAX_ITEMS} names")
    return list(dict.fromkeys(value))


def _models(value: Any) -> dict[str, str]:
    value = value or {}
    if not isinstance(value, dict):
        raise SkillError("model: one per agent, e.g. {claude: haiku, antigravity: flash}")
    out = {}
    for cli in CLIS:
        model = value.get(cli) or ""
        if not isinstance(model, str) or (model and not MODEL.match(model)):
            raise SkillError(f"{cli} model: a model name such as sonnet, haiku or flash")
        out[cli] = model
    return out


class Studio:
    """Rules, profiles and teams kept beside a skill :class:`~specops.skills.Library`.

    It shares the library's home, projects, lock and audit log, and publishes a profile's skills
    through it.
    """

    def __init__(self, library: Library) -> None:
        self.lib = library
        self.dirs = {k: library.home / f"{k}s" for k in KINDS}

    # ------------------------------------------------------------ storage
    def _path(self, kind: str, name: str) -> Path:
        if kind not in KINDS:
            raise SkillError("unknown kind")
        if not NAME.match(name or ""):
            raise SkillError(f"invalid {kind} name")
        path = self.dirs[kind] / (f"{name}.md" if kind == "rule" else f"{name}.json")
        if path.is_symlink():
            raise SkillError(f"the library file for this {kind} is a symlink", 409)
        return path

    def names(self, kind: str) -> list[str]:
        suffix = ".md" if kind == "rule" else ".json"
        with contextlib.suppress(OSError):
            return sorted(
                p.stem
                for p in self.dirs[kind].iterdir()
                if p.suffix == suffix and NAME.match(p.stem) and p.is_file() and not p.is_symlink()
            )
        return []

    def get(self, kind: str, name: str) -> dict[str, Any]:
        path = self._path(kind, name)
        try:
            raw = path.read_bytes()
        except OSError:
            raise SkillError(f"no such {kind}: {name}", 404) from None
        updated = path.stat().st_mtime
        if kind == "rule":
            meta, body = parse_skill_md(raw.decode("utf-8", "replace"))
            return {
                "name": name,
                "description": meta.get("description", ""),
                "body": body,
                "updated": updated,
            }
        try:
            data = json.loads(raw)
        except ValueError:
            raise SkillError(f"{path} is not valid JSON", 500) from None
        data = data if isinstance(data, dict) else {}
        item = {
            "name": name,
            "description": str(data.get("description") or ""),
            "prompt": str(data.get("prompt") or ""),
            "model": {cli: str((data.get("model") or {}).get(cli) or "") for cli in CLIS},
            "updated": updated,
        }
        if kind == "profile":
            item |= {k: [str(v) for v in data.get(k) or []] for k in ("rules", "skills", "tools")}
        else:
            item["members"] = [str(v) for v in data.get("members") or []]
        return item

    def _validate(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        name = data.get("name")
        if not isinstance(name, str) or not NAME.match(name):
            raise SkillError("name: 1-64 characters, lowercase letters, digits and dashes")
        desc = _text(data.get("description"), "description", MAX_DESCRIPTION)
        if kind == "rule":
            body = _text(data.get("body"), "the rule's text", MAX_BODY)
            return {"name": name, "description": desc, "body": body}
        item: dict[str, Any] = {
            "name": name,
            "description": desc,
            "prompt": _text(data.get("prompt"), "prompt", MAX_BODY, required=kind == "profile"),
            "model": _models(data.get("model")),
        }
        if kind == "profile":
            item["rules"] = _names(data.get("rules"), "rules")
            item["skills"] = _names(data.get("skills"), "skills")
            tools = data.get("tools") or []
            if (
                not isinstance(tools, list)
                or len(tools) > MAX_ITEMS
                or not all(isinstance(t, str) and TOOL.match(t.strip()) for t in tools)
            ):
                raise SkillError("tools: tool names such as Read, Grep or Bash(git log:*)")
            item["tools"] = list(dict.fromkeys(t.strip() for t in tools))
            missing = [r for r in item["rules"] if r not in self.names("rule")]
            if missing:
                raise SkillError(f"no such rule: {', '.join(missing)}")
            missing = [s for s in item["skills"] if s not in self.lib.names()]
            if missing:
                raise SkillError(f"no such skill in the library: {', '.join(missing)}")
        else:
            item["members"] = _names(data.get("members"), "members")
            if not item["members"]:
                raise SkillError("a team needs at least one member profile")
            if name in item["members"]:
                raise SkillError("a team can't be its own member")
            missing = [m for m in item["members"] if m not in self.names("profile")]
            if missing:
                raise SkillError(f"no such profile: {', '.join(missing)}")
        return item

    def _write(self, kind: str, item: dict[str, Any]) -> None:
        path = self._path(kind, item["name"])
        if kind == "rule":
            _write_atomic(path, render_skill_md(item["name"], item["description"], item["body"]))
        else:
            fields = {k: v for k, v in item.items() if k != "name"}
            _write_atomic(path, json.dumps(fields, indent=2, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------ rendering
    def render(self, kind: str, name: str, cli: str) -> str:
        """The agent definition file for ``cli`` ("claude" or "antigravity")."""
        item = self.get(kind, name)
        fields: list[tuple[str, Any]] = [
            ("name", name),
            ("description", item["description"]),
            ("model", item["model"].get(cli, "")),
        ]
        body = [f"# {name}", ""]
        if item["prompt"].strip():
            body += [item["prompt"].strip(), ""]
        if kind == "profile":
            if cli == "claude":
                fields += [("tools", ", ".join(item["tools"])), ("skills", item["skills"])]
            rules = [self.get("rule", r) for r in item["rules"] if r in self.names("rule")]
            if rules:
                body += ["## Rules", "", "Always follow these rules.", ""]
                for rule in rules:
                    body += [f"### {rule['name']}", "", rule["body"].strip(), ""]
        else:
            members = [self.get("profile", m) for m in item["members"]]
            if cli == "antigravity":
                fields.append(("agents", [f"./{m['name']}.md" for m in members]))
            body += ["## Your team", "", DELEGATE[cli], ""]
            body += [f"- **{m['name']}**: {m['description']}" for m in members]
            body.append("")
        return "\n".join(_frontmatter(fields) + body).rstrip() + "\n"

    # ------------------------------------------------------------ publishing
    def targets(self, project: str | None = None) -> list[Target]:
        return [agent_target(t) for t in self.lib.targets(project)]

    def all_targets(self) -> list[Target]:
        found = self.targets()
        for project in self.lib.projects():
            found += [agent_target(t) for t in project_targets(project)]
        return found

    def target(self, key: str) -> Target:
        return agent_target(self.lib.target(key))

    @staticmethod
    def _manifest(root: Path) -> dict[str, Any]:
        try:
            data = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _mark(self, root: Path, name: str, entry: dict[str, Any] | None) -> None:
        manifest = self._manifest(root)
        if entry is None:
            manifest.pop(name, None)
        else:
            manifest[name] = entry
        _write_atomic(root / MANIFEST, json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    def status(self, kind: str, name: str, target: Target) -> str:
        """``off``, ``on``, ``stale`` (library changed since), ``modified`` (file edited
        elsewhere) or ``conflict`` (an agent with this name that isn't ours)."""
        dest = target.root / f"{name}.md"
        if dest.is_symlink():
            return "conflict"
        if not dest.exists():
            return "off"
        entry = self._manifest(target.root).get(name)
        if not isinstance(entry, dict) or entry.get("kind") != kind:
            return "conflict"
        try:
            current = _hash(dest.read_bytes())
        except OSError:
            return "modified"
        if current != entry.get("hash"):
            return "modified"
        try:
            expected = _hash(self.render(kind, name, target.agent).encode())
        except SkillError:
            return "stale"
        return "on" if expected == entry.get("hash") else "stale"

    def published(self, kind: str, name: str) -> list[dict[str, Any]]:
        rows = []
        for t in self.all_targets():
            state = self.status(kind, name, t)
            if t.scope == "global" or state != "off":
                rows.append({**t.to_dict(), "status": state})
        return rows

    def _publish(self, kind: str, name: str, target: Target, key: str, actor: str) -> list[str]:
        """Write the agent file (and what it needs). Returns notes on what was left alone."""
        notes: list[str] = []
        item = self.get(kind, name)
        if kind == "team":
            for member in item["members"]:
                state = self.status("profile", member, target)
                if state in ("off", "stale"):
                    notes += self._publish("profile", member, target, key, actor)
                elif state in ("conflict", "modified"):
                    notes.append(f"profile {member}: left alone ({STATUS_NOTE[state]})")
        else:
            for skill in item["skills"]:
                try:
                    self.lib.set_published(skill, key, True, False, actor)
                except SkillError as err:
                    notes.append(f"skill {skill}: {err}")
        text = self.render(kind, name, target.agent)
        _write_atomic(target.root / f"{name}.md", text)
        entry = {
            "kind": kind,
            "hash": _hash(text.encode()),
            "published": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "note": "Managed by SpecOps Agents. Edit it there, or remove this entry to take over.",
        }
        self._mark(target.root, name, entry)
        return notes

    def set_published(
        self, kind: str, name: str, key: str, on: bool, force: bool, actor: str
    ) -> dict[str, Any]:
        if kind not in AGENT_KINDS:
            raise SkillError("only profiles and teams are published")
        with self.lib._lock:
            self.get(kind, name)
            target = self.target(key)
            dest = target.root / f"{name}.md"
            state = self.status(kind, name, target)
            if state == "conflict":
                raise SkillError(
                    f"{dest} already exists and wasn't published by SpecOps; it is left alone", 409
                )
            if state == "modified" and not force:
                raise SkillError(
                    f"{dest} was edited outside SpecOps since it was published; confirm to "
                    + ("overwrite" if on else "remove")
                    + " it",
                    409,
                )
            notes: list[str] = []
            if on:
                notes = self._publish(kind, name, target, key, actor)
            elif state != "off":
                dest.unlink(missing_ok=True)
                self._mark(target.root, name, None)
            self._audit(actor, "publish" if on else "unpublish", kind, name, target=key)
            return self.describe(kind, name) | {"notes": notes}

    def _live(self) -> list[tuple[str, str, Target]]:
        """Every (kind, name, target) currently published and in sync."""
        return [
            (kind, name, t)
            for kind in AGENT_KINDS
            for name in self.names(kind)
            for t in self.all_targets()
            if self.status(kind, name, t) == "on"
        ]

    # ------------------------------------------------------------ changes
    def save(self, kind: str, data: dict[str, Any], create: bool, actor: str) -> dict[str, Any]:
        item = self._validate(kind, data)
        name = item["name"]
        with self.lib._lock:
            exists = self._path(kind, name).is_file()
            if create and exists:
                raise SkillError(f"a {kind} with this name already exists", 409)
            if not create and not exists:
                raise SkillError(f"no such {kind}", 404)
            if create and kind in AGENT_KINDS:
                other = "team" if kind == "profile" else "profile"
                if name in self.names(other):
                    raise SkillError(f"a {other} is already called {name}", 409)
            before = self._live()
            self._write(kind, item)
            # Keep every published copy that was in sync, in sync (a rule change reaches every
            # profile that follows it, a profile's description every team it is in).
            synced = []
            for k, n, t in before:
                if self.status(k, n, t) == "stale":
                    key = t.key
                    self._publish(k, n, t, key, actor)
                    synced.append(f"{k}:{n}:{key}")
            self._audit(actor, "create" if create else "update", kind, name, synced=synced)
            return self.describe(kind, name)

    def users(self, kind: str, name: str) -> list[str]:
        """What depends on this rule (profiles) or profile (teams)."""
        if kind == "rule":
            return [p for p in self.names("profile") if name in self.get("profile", p)["rules"]]
        if kind == "profile":
            return [t for t in self.names("team") if name in self.get("team", t)["members"]]
        return []

    def delete(self, kind: str, name: str, force: bool, actor: str) -> None:
        with self.lib._lock:
            self.get(kind, name)
            users = self.users(kind, name)
            if users:
                what = "profiles" if kind == "rule" else "teams"
                raise SkillError(f"still used by {what}: {', '.join(users)}", 409)
            removed = []
            if kind in AGENT_KINDS:
                copies = [r for r in self.published(kind, name) if r["status"] != "off"]
                if any(r["status"] == "modified" for r in copies) and not force:
                    raise SkillError(
                        "a published copy was edited outside SpecOps; confirm to delete it too", 409
                    )
                for row in copies:
                    if row["status"] != "conflict":
                        root = Path(row["root"])
                        (root / f"{name}.md").unlink(missing_ok=True)
                        self._mark(root, name, None)
                        removed.append(row["key"])
            self._path(kind, name).unlink()
            self._audit(actor, "delete", kind, name, removed=removed)

    # ------------------------------------------------------------ reads
    def describe(self, kind: str, name: str) -> dict[str, Any]:
        item = self.get(kind, name)
        if kind in AGENT_KINDS:
            item["targets"] = self.published(kind, name)
        if kind != "team":
            item["used_by"] = self.users(kind, name)
        return item

    def list(self) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for kind in KINDS:
            out[f"{kind}s"] = []
            for name in self.names(kind):
                with contextlib.suppress(SkillError):
                    out[f"{kind}s"].append(self.describe(kind, name))
        return out

    def agent_names(self) -> dict[str, str]:
        """``{agent name: "profile" | "team"}``, to recognise them in transcripts."""
        return {n: kind for kind in AGENT_KINDS for n in self.names(kind)}

    def _audit(self, actor: str, action: str, kind: str, name: str, **detail: Any) -> None:
        self.lib._audit(actor, action, name, kind=kind, **detail)
