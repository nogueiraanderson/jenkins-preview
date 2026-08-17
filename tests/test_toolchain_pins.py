"""The toolchain pins live in three files. Only a test keeps them in lockstep.

ruff drifted once under `@latest` and broke main on a docs-only commit, so the
version is pinned in ci.yml, justfile and .pre-commit-config.yaml. A comment
asking to bump them together is a wish. This is the gate.
"""

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent


def versions_of(pattern: str, text: str) -> set[str]:
    return set(re.findall(pattern, text))


def test_ruff_version_is_one_across_ci_justfile_and_precommit() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    justfile = (ROOT / "justfile").read_text()
    precommit = (ROOT / ".pre-commit-config.yaml").read_text()

    found = {
        "ci.yml": versions_of(r"ruff@(\d+\.\d+\.\d+)", ci),
        "justfile": versions_of(r"ruff@(\d+\.\d+\.\d+)", justfile),
        ".pre-commit-config.yaml": versions_of(r"ruff@(\d+\.\d+\.\d+)", precommit),
    }
    for name, versions in found.items():
        assert len(versions) == 1, f"{name} pins ruff at {sorted(versions)}, expected exactly one"
    assert len(set.union(*found.values())) == 1, f"ruff versions diverge: {found}"


def test_uv_version_is_one_across_ci_and_precommit() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    precommit = (ROOT / ".pre-commit-config.yaml").read_text()

    ci_versions = versions_of(r'version: "(\d+\.\d+\.\d+)"', ci)
    hook_rev = versions_of(r"uv-pre-commit\n    rev: (\d+\.\d+\.\d+)", precommit)
    assert len(ci_versions) == 1, f"ci.yml setup-uv pins diverge: {sorted(ci_versions)}"
    assert ci_versions == hook_rev, (
        f"uv pinned {sorted(ci_versions)} in CI, {sorted(hook_rev)} in pre-commit"
    )


def test_every_setup_uv_block_carries_the_pin() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    blocks = re.split(r"- uses: astral-sh/setup-uv", ci)[1:]
    assert blocks, "no setup-uv blocks found"
    for block in blocks:
        head = block.split("- name:", 1)[0]
        assert re.search(r'version: "\d+\.\d+\.\d+"', head), (
            "a setup-uv block without a version input installs whatever uv is latest"
        )


def test_package_version_has_one_value_in_both_sources() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()
    module = (ROOT / "src/jenkins_preview/__init__.py").read_text()
    (from_toml,) = re.findall(r'^version = "([^"]+)"', pyproject, re.M)
    (from_module,) = re.findall(r'^__version__ = "([^"]+)"', module, re.M)
    assert from_toml == from_module, f"pyproject {from_toml} vs __init__ {from_module}"


def test_pytest_invocation_matches_ci() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    precommit = (ROOT / ".pre-commit-config.yaml").read_text()
    justfile = (ROOT / "justfile").read_text()
    assert "uv run --locked --group dev pytest" in ci
    assert "uv run --locked --group dev pytest" in precommit, (
        "the pre-commit pytest hook must run the same uv invocation as CI"
    )
    assert justfile.count("uv run --locked --group dev pytest") == 3, (
        "every justfile pytest recipe must run CI's exact locked invocation"
    )
