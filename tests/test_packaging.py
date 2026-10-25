"""Guard against a source checkout and installed release resolving different dependencies."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_requirements_match_package_metadata():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    requirements = {
        line.strip()
        for line in (ROOT / "requirements.txt").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert requirements == set(project["dependencies"])
    assert not any(requirement.startswith(("pytest", "ruff", "build")) for requirement in requirements)
