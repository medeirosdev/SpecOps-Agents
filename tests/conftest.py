from __future__ import annotations

import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SESSION_ID = "11111111-2222-3333-4444-555555555555"
PROJECT = "-home-you-code-shop"


@pytest.fixture
def projects(tmp_path: Path) -> Path:
    """A writable copy of the fixture ``projects`` directory."""
    root = tmp_path / "projects"
    shutil.copytree(FIXTURES / "projects", root)
    return root


@pytest.fixture
def session_file(projects: Path) -> Path:
    return projects / PROJECT / f"{SESSION_ID}.jsonl"
