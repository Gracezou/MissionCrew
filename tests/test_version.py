"""MissionCrew 版本号的单一来源与构建元数据契约。"""
from __future__ import annotations

import ast
import importlib.metadata
import re
import tomllib
from pathlib import Path

from packaging.version import Version

from missioncrew import __version__


ROOT = Path(__file__).resolve().parents[1]
SEMVER_PEP440_RE = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:rc[1-9]\d*)?")


def test_version_is_semver_and_pep440_compatible():
    assert SEMVER_PEP440_RE.fullmatch(__version__)
    assert str(Version(__version__)) == __version__


def test_pyproject_reads_dynamic_version_from_package():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "version" not in config["project"]
    assert "version" in config["project"]["dynamic"]
    assert config["tool"]["hatch"]["version"]["path"] == "missioncrew/__init__.py"


def test_package_version_is_not_duplicated_in_source():
    duplicates = []
    package_root = ROOT / "missioncrew"
    for path in package_root.rglob("*.py"):
        if path == package_root / "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(isinstance(node, ast.Constant) and node.value == __version__
               for node in ast.walk(tree)):
            duplicates.append(path.relative_to(ROOT).as_posix())

    assert duplicates == []


def test_installed_distribution_uses_package_version():
    assert importlib.metadata.version("missioncrew") == __version__
