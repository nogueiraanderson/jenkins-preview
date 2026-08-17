"""Build-time fidelity: what a preview would fetch from OUTSIDE the pinned fork.

The config gates prove what Jenkins stores. They cannot see what the pipeline
script does once it runs: an inline `git url: ...` step, a shared-library load
or a raw-file download fetches code at build time, and when that code comes
from the canonical repo the preview quietly tests production's copy of it. A
green preview then means "green against master", the exact false negative this
tool exists to prevent.

The scan reads each pipeline's script at the pinned checkout and classifies
every fetch it can see:

- `canonical`: fetches a repo the job's own SCM pointed at before the pin, or
  any repo sharing the fork's name under another owner. The direct fidelity
  break. Publication refuses without an explicit acknowledgment.
- `library`: a shared-library load not proven to be the fork at the pinned
  commit (global libraries resolve via controller config, never the fork).
  Refuses without acknowledgment.
- `uninspectable`: a pipeline whose script could not be scanned (unparseable
  config, no scriptPath, path escaping the checkout). Refuses without
  acknowledgment: unscanned must never read as clean.
- `sibling`: a `build job:` or `copyArtifacts projectName:` literal naming a
  job OUTSIDE the published set. Jenkins resolves such names folder-first and
  then falls back towards the root, so an under-curated set can trigger or
  copy from the production job of the same name. Refuses without
  acknowledgment. The usual fix is adding the sibling to the set.
- `missing`: the scriptPath does not exist at the pinned commit. The build
  would die at script load, so this is a hard refusal, not a warning.
- `external`: a fetch of any other repository (a product checkout, a tool clone).
  Disclosed, never blocking: depending on other code is normal, silently
  swapping the code under test is not.

A regex over Groovy is a heuristic and is treated as one: literal URLs are
classified, dynamic ones cannot be followed, and that limit stays documented
rather than hidden.
"""

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

_REPO_URL_LITERAL = re.compile(r"""url\s*:\s*['"](?P<url>(?:https?://|git@|ssh://)[^'"]+)['"]""")
_REMOTE_LITERAL = re.compile(r"""remote\s*:\s*['"](?P<url>(?:https?://|git@|ssh://)[^'"]+)['"]""")
_FETCHED_FILE = re.compile(r"""\b(?:curl|wget)\b[^\n]*?(?P<url>https?://[^\s'"]+)""")
# The dominant in-repo form is `library changelog: false, identifier: "x@ref",
# retriever: modernSCM([...` with arguments between the verb and identifier, so
# the pattern must tolerate them. A bare-word match would see almost none.
_LIBRARY_LOAD = re.compile(r"@Library\b|\bmodernSCM\s*\(|\blibrary\b[^\n]*\bidentifier\s*:")
_LIBRARY_VERSION = re.compile(r"""['"][^'"@]+@(?P<version>[^'"]+)['"]""")
# `build job:` and `copyArtifacts projectName:` live in the script, outside gate
# G7's reach. A literal naming a job outside the published set resolves folder-
# first and then FALLS BACK towards the root, where the production job lives.
_TRIGGER_LITERAL = re.compile(r"""\bbuild\s+job\s*:\s*['"](?P<name>[^'"$]+)['"]""")
_COPY_LITERAL = re.compile(r"""\bprojectName\s*:\s*['"](?P<name>[^'"$]+)['"]""")
_LINE_COMMENT = re.compile(r"^\s*(//|#|\*)")
_INLINE_COMMENT = re.compile(r"(?<!:)//.*$")

BLOCKING_KINDS = ("canonical", "library", "uninspectable", "sibling")
_LIBRARY_WINDOW = 6  # lines a multi-line library declaration may span


@dataclass(frozen=True)
class Escape:
    """One build-time fetch (or scan gap) in a published pipeline script."""

    script: str
    line: int  # 0 means the whole script
    kind: str  # "canonical", "library", "sibling", "uninspectable", "missing" or "external"
    detail: str


def repo_identity(url: str) -> str:
    """One identity for the same repo across https, ssh, scp and raw spellings.

    The host is case-insensitive and lowercased. The path keeps its case, since
    some hosts distinguish it. `.git` and trailing slashes are noise. A
    raw.githubusercontent.com file URL identifies as the repo it serves.
    """
    bare = url.strip().removesuffix("/").removesuffix(".git")
    if match := re.fullmatch(
        r"https?://raw\.githubusercontent\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/.*", bare
    ):
        return f"github.com/{match['owner']}/{match['repo']}"
    if match := re.fullmatch(r"(?:ssh://)?git@(?P<host>[^:/]+)[:/](?P<path>.+)", bare):
        return f"{match['host'].lower()}/{match['path']}"
    if match := re.fullmatch(r"https?://(?P<host>[^/]+)/(?P<path>.+)", bare):
        return f"{match['host'].lower()}/{match['path']}"
    return bare.lower()


def canonical_identities(raw_configs: dict[str, str]) -> set[str]:
    """The repos the jobs' own SCM stanzas point at BEFORE the pin rewrite.

    These are what production fetches. An in-script fetch of one of them is the
    canonical bypass, distinct from a checkout of some unrelated repo. Feed the
    RAW rendered configs: after sanitising, the remotes already read as the fork.
    """
    identities: set[str] = set()
    for xml_text in raw_configs.values():
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            continue
        for url in root.iter("url"):
            if url.text and url.text.strip():
                identities.add(repo_identity(url.text.strip()))
    return identities


def _pipeline_scripts(configs: dict[str, str]) -> tuple[dict[str, str], list[Escape]]:
    """(job -> scriptPath), plus an uninspectable Escape per pipeline hiding its script."""
    paths: dict[str, str] = {}
    gaps: list[Escape] = []
    for job, xml_text in configs.items():
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            gaps.append(Escape(job, 0, "uninspectable", "config is not parseable XML"))
            continue
        if path := root.findtext(".//scriptPath"):
            paths[job] = path
        elif any("CpsScmFlowDefinition" in (el.get("class") or "") for el in root.iter()):
            gaps.append(Escape(job, 0, "uninspectable", "pipeline config carries no scriptPath"))
    return paths, gaps


def _lines(file: Path) -> list[tuple[int, str]]:
    """Numbered lines with comments removed: full-line, inline //, and /* */ blocks."""
    kept: list[tuple[int, str]] = []
    in_block = False
    for number, text in enumerate(file.read_text(errors="replace").splitlines(), 1):
        if in_block:
            if "*/" not in text:
                continue
            in_block = False
            text = text.split("*/", 1)[1]
        if "/*" in text and "*/" not in text.split("/*", 1)[1]:
            in_block = True
            text = text.split("/*", 1)[0]
        if _LINE_COMMENT.match(text):
            continue
        kept.append((number, _INLINE_COMMENT.sub("", text)))
    return kept


def _urls_in(text: str) -> list[str]:
    urls = [match["url"] for match in _REPO_URL_LITERAL.finditer(text)]
    urls += [match["url"] for match in _REMOTE_LITERAL.finditer(text)]
    urls += [match["url"] for match in _FETCHED_FILE.finditer(text)]
    return urls


def scan(
    configs: dict[str, str],
    workdir: Path,
    fork_url: str,
    *,
    sha: str,
    canonical: set[str],
) -> list[Escape]:
    """Classified build-time fetches for the published jobs' pipeline scripts.

    `configs` should be the RAW rendered configs (see canonical_identities).
    """
    fork = repo_identity(fork_url)
    fork_name = fork.rsplit("/", 1)[-1]
    canon = canonical
    paths, escapes = _pipeline_scripts(configs)
    merged: set[tuple[str, int, str, str]] = set()

    def classify(identity: str) -> str | None:
        if identity == fork:
            return None
        if identity in canon or identity.rsplit("/", 1)[-1] == fork_name:
            # The same repo name under another owner is how the canonical repo
            # (or someone else's fork of it) sneaks into a preview.
            return "canonical"
        return "external"

    for script in sorted(set(paths.values())):
        file = (workdir / script).resolve()
        if workdir.resolve() not in file.parents:
            merged.add((script, 0, "uninspectable", "script path escapes the checkout"))
            continue
        if not file.is_file():
            merged.add((script, 0, "missing", "does not exist at the pinned commit"))
            continue

        lines = _lines(file)
        consumed: set[int] = set()
        for index, (number, text) in enumerate(lines):
            if number in consumed:
                continue
            if _LIBRARY_LOAD.search(text):
                # A declaration spans lines (identifier on one, remote on a
                # later one), so the whole window feeds one Escape anchored at
                # the declaration, and its lines are not re-reported.
                window = [text]
                for later_number, later_text in lines[index + 1 : index + 1 + _LIBRARY_WINDOW]:
                    window.append(later_text)
                    consumed.add(later_number)
                joined = " ".join(window)
                version = _LIBRARY_VERSION.search(joined)
                pinned = bool(version) and version["version"].lower() == sha.lower()
                on_fork = any(repo_identity(url) == fork for url in _urls_in(joined))
                if not (pinned and on_fork):
                    merged.add((script, number, "library", text.strip()[:100]))
                continue
            for url in _urls_in(text):
                if kind := classify(repo_identity(url)):
                    merged.add((script, number, kind, url))
            for match in (*_TRIGGER_LITERAL.finditer(text), *_COPY_LITERAL.finditer(text)):
                name = match["name"]
                head = name.lstrip("/").split("/", 1)[0]
                if name.startswith("/") or head not in configs:
                    merged.add((script, number, "sibling", name))

    escapes += [Escape(*key) for key in sorted(merged)]
    return escapes


def blocking(escapes: list[Escape]) -> list[Escape]:
    """The escapes publication refuses without an explicit acknowledgment."""
    return [escape for escape in escapes if escape.kind in BLOCKING_KINDS]


def blocking_digest(escapes: list[Escape]) -> str:
    """A short stable fingerprint of exactly the blocking escapes.

    Stamped into the folder marker when an acknowledgment is given, so a later
    `sync` can tell "the same escapes the operator saw" from "something new".
    A count cannot: one blocker swapped for another keeps the count identical.
    Line numbers stay out of the fingerprint: editing the script above an
    unchanged fetch moves it without changing what was acknowledged.
    """
    lines = sorted(f"{e.kind}\x1f{e.script}\x1f{e.detail}" for e in blocking(escapes))
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:12]


def missing(escapes: list[Escape]) -> list[Escape]:
    """Scripts absent at the pinned commit: the build dies at load, so no
    acknowledgment can make publishing them meaningful."""
    return [escape for escape in escapes if escape.kind == "missing"]
