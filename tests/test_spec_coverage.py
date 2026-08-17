"""docs/SPEC.md and the suite move in lockstep, mechanically.

A behaviour without a cited test is a promise nobody keeps. A test no spec row
claims is behaviour nobody documented. Both directions fail here, so the spec
cannot rot the way prose does.
"""

import re
from pathlib import Path

SPEC = Path(__file__).parent.parent / "docs" / "SPEC.md"
TESTS_DIR = Path(__file__).parent

CITATION = re.compile(r"(test_\w+\.py)::(test_\w+)")
DEFINITION = re.compile(r"^def (test_\w+)", re.MULTILINE)


def _actual() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        for match in DEFINITION.finditer(path.read_text()):
            found.add((path.name, match.group(1)))
    return found


def _cited() -> set[tuple[str, str]]:
    return {(file, name) for file, name in CITATION.findall(SPEC.read_text())}


def test_every_spec_citation_exists() -> None:
    ghosts = _cited() - _actual()
    assert not ghosts, f"spec cites tests that do not exist: {sorted(ghosts)}"


def test_every_test_is_claimed_by_the_spec() -> None:
    unclaimed = _actual() - _cited()
    assert not unclaimed, (
        f"tests no spec row claims (add them to docs/SPEC.md): {sorted(unclaimed)}"
    )


def test_the_spec_has_no_placeholders() -> None:
    text = SPEC.read_text()
    for token in ("TBD", "TODO", "GAP", "???"):
        assert token not in text, f"placeholder {token!r} in docs/SPEC.md. Placeholders are refused"
