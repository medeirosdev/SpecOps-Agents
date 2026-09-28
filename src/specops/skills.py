"""A skill library shared by Claude Code and Antigravity.

Both read the same format: a folder holding a ``SKILL.md`` whose YAML frontmatter has a ``name``
and a ``description``. Skills live once, in ``~/.specops/skills/<name>/``, and are *published* by
copying that folder into the directories each agent scans:

=============  =======================================  ==============================
agent          global                                   per project
=============  =======================================  ==============================
Claude Code    ``~/.claude/skills/<name>``              ``<project>/.claude/skills``
Antigravity    ``~/.gemini/config/skills/<name>``       ``<project>/.agents/skills``
=============  =======================================  ==============================

Every published copy carries a ``.specops-skill.json`` marker with the hash of what was written.
It is how a copy is recognised as ours: folders without it are never modified or deleted, and a
copy edited by hand since publishing is only overwritten or removed when explicitly forced.
Every change is appended to ``~/.specops/audit.log``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MARKER = ".specops-skill.json"
SKILL_FILE = "SKILL.md"
MAX_DESCRIPTION = 1024
MAX_BODY = 256 * 1024
MAX_IMPORT_BYTES = 5 * 1024 * 1024
AUDIT_KEEP = 200


class SkillError(Exception):
    """A request that can't be done; the message is safe to show to the user."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def specops_home() -> Path:
    env = os.environ.get("SPECOPS_HOME")
    return Path(env).expanduser() if env else Path.home() / ".specops"


def claude_home() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


@dataclass(frozen=True)
class Target:
    """A directory one agent scans for skills."""

    agent: str  # "claude" | "antigravity"
    scope: str  # "global" | "project"
    root: Path
    project: str = ""

    @property
    def key(self) -> str:
        return f"{self.scope}:{self.agent}" + (f":{self.project}" if self.project else "")

    def to_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "agent": self.agent,
            "scope": self.scope,
            "project": self.project,
            "root": str(self.root),
        }


def global_targets() -> list[Target]:
    return [
        Target("claude", "global", claude_home() / "skills"),
        Target("antigravity", "global", Path.home() / ".gemini" / "config" / "skills"),
    ]


def project_targets(project: str) -> list[Target]:
    base = Path(project)
    return [
        Target("claude", "project", base / ".claude" / "skills", project),
        Target("antigravity", "project", base / ".agents" / "skills", project),
    ]


# ------------------------------------------------------------------ SKILL.md
def parse_skill_md(text: str) -> tuple[dict[str, str], str]:
    """Split a ``SKILL.md`` into its frontmatter (top-level scalars only) and its body."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        return {}, text
    meta: dict[str, str] = {}
    i = 1
    while i < end:
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", lines[i])
        i += 1
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if value[:1] in (">", "|"):  # block scalar: the indented lines that follow
            block = []
            while i < end and (not lines[i].strip() or lines[i][:1] in " \t"):
                block.append(lines[i].strip())
                i += 1
            joiner = " " if value.startswith(">") else "\n"
            value = joiner.join(b for b in block if b) if joiner == " " else "\n".join(block)
        elif value[:1] == '"':
            with contextlib.suppress(ValueError):
                value = json.loads(value)
        elif value[:1] == "'" and value.endswith("'"):
            value = value[1:-1].replace("''", "'")
        meta[key] = str(value).strip()
    body = "\n".join(lines[end + 1 :]).strip("\n")
    return meta, body


def render_skill_md(name: str, description: str, body: str) -> str:
    # A JSON string is a valid YAML double-quoted scalar, so any description round-trips.
    desc = json.dumps(description, ensure_ascii=False)
    return f"---\nname: {name}\ndescription: {desc}\n---\n\n{body.strip()}\n"


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate(name: str, description: str, body: str) -> None:
    if not isinstance(name, str) or not NAME.match(name):
        raise SkillError("name: 1-64 characters, lowercase letters, digits and dashes")
    if not isinstance(description, str) or not description.strip():
        raise SkillError("description is required: it's how agents decide to use the skill")
    if len(description) > MAX_DESCRIPTION:
        raise SkillError(f"description is longer than {MAX_DESCRIPTION} characters")
    if not isinstance(body, str) or not body.strip():
        raise SkillError("the skill needs instructions")
    if len(body.encode()) > MAX_BODY:
        raise SkillError(f"instructions are larger than {MAX_BODY // 1024} KB")
    if "\x00" in description + body:
        raise SkillError("text contains a NUL character")


# ------------------------------------------------------------------ filesystem helpers
def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _skip_links(folder: str, names: list[str]) -> list[str]:
    """``copytree`` ignore hook: never copy symlinks (they could point at secrets) or markers."""
    return [n for n in names if n == MARKER or os.path.islink(os.path.join(folder, n))]


def _copy_dir(src: Path, dest: Path, marker: dict[str, Any]) -> None:
    """Replace ``dest`` with a copy of ``src`` plus a marker, as atomically as the OS allows."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{dest.name}.", dir=dest.parent))
    try:
        for item in src.iterdir():
            if item.is_symlink() or item.name == MARKER:
                continue
            if item.is_dir():
                shutil.copytree(item, tmp / item.name, ignore=_skip_links)
            else:
                shutil.copy2(item, tmp / item.name)
        (tmp / MARKER).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
        if dest.exists():
            old = dest.with_name(f".{dest.name}.old-{os.getpid()}-{time.monotonic_ns()}")
            dest.rename(old)
            tmp.rename(dest)
            shutil.rmtree(old, ignore_errors=True)
        else:
            tmp.rename(dest)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


# ------------------------------------------------------------------ the library
class Library:
    """Skills under ``home/skills``, and their published copies.

    ``projects`` returns the project directories skills may be published into: only folders
    where an agent session was seen, never an arbitrary path from a request.
    """

    def __init__(
        self,
        home: Path | None = None,
        projects: Callable[[], Iterable[str]] = list,
        targets: Callable[[], list[Target]] = global_targets,
    ) -> None:
        self.home = home or specops_home()
        self.root = self.home / "skills"
        self.audit_path = self.home / "audit.log"
        self._projects = projects
        self._global_targets = targets
        self._lock = threading.RLock()

    # ------------------------------------------------------------ lookups
    def projects(self) -> list[str]:
        home = Path.home().resolve()
        out = set()
        for p in self._projects():
            if not p or not os.path.isabs(p):
                continue
            path = Path(p)
            with contextlib.suppress(OSError):
                resolved = path.resolve()
                # Home or / as a "project" would make the project folders the global ones.
                if path.is_dir() and resolved != home and resolved.parent != resolved:
                    out.add(str(path))
        return sorted(out)

    def targets(self, project: str | None = None) -> list[Target]:
        if project is None:
            return list(self._global_targets())
        if project not in self.projects():
            raise SkillError("unknown project: only folders with an agent session can be used")
        return project_targets(project)

    def target(self, key: str) -> Target:
        scope, _, rest = key.partition(":")
        _agent, _, project = rest.partition(":")
        found = self.targets(project if scope == "project" else None)
        for t in found:
            if t.key == key:
                return t
        raise SkillError("unknown target")

    def _dir(self, name: str) -> Path:
        if not NAME.match(name or ""):
            raise SkillError("invalid skill name")
        path = self.root / name
        if path.is_symlink():
            raise SkillError("the library folder for this skill is a symlink", 409)
        return path

    def _read(self, name: str) -> dict[str, Any]:
        path = self._dir(name) / SKILL_FILE
        try:
            raw = path.read_bytes()
        except OSError:
            raise SkillError("no such skill", 404) from None
        meta, body = parse_skill_md(raw.decode("utf-8", "replace"))
        return {
            "name": name,
            "description": meta.get("description", ""),
            "body": body,
            "hash": _hash(raw),
            "updated": path.stat().st_mtime,
        }

    def names(self) -> list[str]:
        with contextlib.suppress(OSError):
            return sorted(
                p.name
                for p in self.root.iterdir()
                if NAME.match(p.name) and not p.is_symlink() and (p / SKILL_FILE).is_file()
            )
        return []

    def status(self, name: str, target: Target, lib_hash: str | None = None) -> str:
        """``off``, ``on``, ``stale`` (library changed since), ``modified`` (copy edited
        elsewhere), or ``conflict`` (a skill with this name that isn't ours)."""
        dest = target.root / name
        if dest.is_symlink():
            return "conflict"
        if not dest.exists():
            return "off"
        marker = self._marker(dest)
        if marker is None:
            return "conflict"
        try:
            current = _hash((dest / SKILL_FILE).read_bytes())
        except OSError:
            return "modified"
        if current != marker.get("hash"):
            return "modified"
        if lib_hash is not None and marker.get("hash") != lib_hash:
            return "stale"
        return "on"

    @staticmethod
    def _marker(dest: Path) -> dict[str, Any] | None:
        try:
            marker = json.loads((dest / MARKER).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return marker if isinstance(marker, dict) and marker.get("source") == "specops" else None

    def published(self, name: str, lib_hash: str | None) -> list[dict[str, Any]]:
        """Every target where this skill exists, plus the global ones (even when off)."""
        rows = []
        targets = self.targets()
        for project in self.projects():
            targets += project_targets(project)
        for t in targets:
            state = self.status(name, t, lib_hash)
            if t.scope == "global" or state != "off":
                rows.append({**t.to_dict(), "status": state})
        return rows

    def list(self) -> list[dict[str, Any]]:
        out = []
        for name in self.names():
            with contextlib.suppress(SkillError):
                skill = self._read(name)
                skill["targets"] = self.published(name, skill["hash"])
                out.append(skill)
        return out

    def external(self) -> list[dict[str, Any]]:
        """Skills in the agents' global folders that don't come from this library."""
        out = []
        for t in self.targets():
            with contextlib.suppress(OSError):
                for d in sorted(t.root.iterdir()):
                    if d.name.startswith(".") or not (d / SKILL_FILE).is_file():
                        continue
                    if self._marker(d) is not None:
                        continue
                    with contextlib.suppress(OSError):
                        meta, _ = parse_skill_md((d / SKILL_FILE).read_text("utf-8", "replace"))
                        out.append(
                            {
                                "name": d.name,
                                "description": meta.get("description", ""),
                                "target": t.key,
                                "agent": t.agent,
                                "path": str(d),
                                "importable": bool(NAME.match(d.name)),
                            }
                        )
        return out

    # ------------------------------------------------------------ changes
    def save(self, name: str, description: str, body: str, create: bool, actor: str) -> dict:
        validate(name, description, body)
        with self._lock:
            folder = self._dir(name)
            exists = (folder / SKILL_FILE).is_file()
            if create and exists:
                raise SkillError("a skill with this name already exists", 409)
            if not create and not exists:
                raise SkillError("no such skill", 404)
            _write_atomic(folder / SKILL_FILE, render_skill_md(name, description, body))
            skill = self._read(name)
            synced = []
            for row in self.published(name, skill["hash"]):
                if row["status"] == "stale":  # keep every copy that was in sync, in sync
                    self._publish(name, self.target(row["key"]), skill["hash"])
                    synced.append(row["key"])
            self._audit(actor, "create" if create else "update", name, synced=synced)
            skill["targets"] = self.published(name, skill["hash"])
            return skill

    def delete(self, name: str, force: bool, actor: str) -> None:
        with self._lock:
            skill = self._read(name)
            copies = [r for r in self.published(name, skill["hash"]) if r["status"] != "off"]
            blocked = [r for r in copies if r["status"] == "conflict"]
            edited = [r for r in copies if r["status"] == "modified"]
            if edited and not force:
                raise SkillError(
                    "a published copy was edited outside SpecOps; confirm to delete it too", 409
                )
            for row in copies:
                if row not in blocked:
                    shutil.rmtree(Path(row["root"]) / name)
            shutil.rmtree(self._dir(name))
            self._audit(
                actor, "delete", name, removed=[r["key"] for r in copies if r not in blocked]
            )

    def set_published(self, name: str, key: str, on: bool, force: bool, actor: str) -> dict:
        with self._lock:
            skill = self._read(name)
            target = self.target(key)
            dest = target.root / name
            state = self.status(name, target, skill["hash"])
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
            if on:
                self._publish(name, target, skill["hash"])
            elif state != "off":
                shutil.rmtree(dest)
            self._audit(actor, "publish" if on else "unpublish", name, target=key)
            skill["targets"] = self.published(name, skill["hash"])
            return skill

    def _publish(self, name: str, target: Target, lib_hash: str) -> None:
        marker = {
            "source": "specops",
            "name": name,
            "hash": lib_hash,
            "published": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "note": "Managed by SpecOps Agents. Edit it there, or delete this file to take over.",
        }
        _copy_dir(self._dir(name), target.root / name, marker)

    def import_skill(self, key: str, name: str, actor: str) -> dict:
        """Copy an existing (non-SpecOps) skill from a global folder into the library."""
        with self._lock:
            target = self.target(key)
            if not NAME.match(name or ""):
                raise SkillError("this skill's folder name can't be used as a library name")
            src = target.root / name
            if (
                src.is_symlink()
                or not (src / SKILL_FILE).is_file()
                or not _inside(src, target.root)
            ):
                raise SkillError("no such skill to import", 404)
            dest = self._dir(name)
            if dest.exists():
                raise SkillError("the library already has a skill with this name", 409)
            size = sum(
                f.stat().st_size for f in src.rglob("*") if f.is_file() and not f.is_symlink()
            )
            if size > MAX_IMPORT_BYTES:
                raise SkillError("that skill folder is larger than 5 MB", 413)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dest, ignore=_skip_links)
            self._audit(actor, "import", name, source=str(src))
            skill = self._read(name)
            skill["targets"] = self.published(name, skill["hash"])
            return skill

    # ------------------------------------------------------------ audit trail
    def _audit(self, actor: str, action: str, name: str, **detail: Any) -> None:
        entry = {"ts": time.time(), "actor": actor, "action": action, "skill": name, **detail}
        self.home.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def audit(self, limit: int = 50) -> list[dict[str, Any]]:
        try:
            lines = self.audit_path.read_text(encoding="utf-8").splitlines()[-AUDIT_KEEP:]
        except OSError:
            return []
        out = []
        for line in reversed(lines):
            with contextlib.suppress(ValueError):
                out.append(json.loads(line))
            if len(out) >= limit:
                break
        return out
