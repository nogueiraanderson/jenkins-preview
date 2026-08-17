"""Job sets: which jobs publish together, and the order their builds run.

Decision: the producer-to-consumer order is written down as data, not inferred by
parsing pipeline code. Inferring a dependency graph out of Groovy is a tarpit, and a
small table is reviewable by the people who own the jobs.

The tool ships no sets. They live in a JSON file, ideally committed at the root of
the pipelines repo, so sets are versioned and reviewed next to the YAML they
render. Exactly one file is consulted, the first found:

  1. `--sets PATH` on the command line
  2. `$JENKINS_PREVIEW_SETS`
  3. `.jenkins-preview.json` at the root of the git checkout you run from
  4. `~/.config/jenkins-preview/sets.json`  (`XDG_CONFIG_HOME` honoured)

When no file is found, any command that needs a set stops and prints that list.
Every definition passes the same strict validation on load: a broken entry
dies with a cause and a fix before anything talks to Jenkins. Job names become URL
path segments and folder names, so they are held to the same charset as every
other name in this tool.
"""

import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import NoReturn

from .errors import die
from .names import SAFE_NAME

type JobName = str
type StageName = str

CONFIG_ENV = "JENKINS_PREVIEW_SETS"
REPO_CONFIG = ".jenkins-preview.json"
XDG_CONFIG = "~/.config/jenkins-preview/sets.json"

SEARCH_ORDER = (
    "--sets PATH",
    f"${CONFIG_ENV}",
    f"{REPO_CONFIG} at the root of the checkout you run from",
    XDG_CONFIG,
)


# eq=False so the class keeps object identity semantics and stays hashable. With
# eq=True, frozen would synthesise __hash__ from the fields and raise on the
# collection fields.
@dataclass(frozen=True, slots=True, eq=False)
class JobSet:
    """A group of jobs published together, plus the order their builds must run."""

    name: str
    yaml_dir: str
    """Repo-relative directory holding the job definitions."""
    jobs: tuple[JobName, ...]
    """Every job published. Basenames are preserved, never suffixed."""
    stages: Mapping[StageName, tuple[JobName, ...]]
    """Stage name to the jobs it triggers."""
    stage_order: tuple[StageName, ...]
    """Producer stages before consumer stages."""
    consumes: Mapping[StageName, StageName] = field(default_factory=dict)
    """Stage to the stage whose artifact it needs. Enforced by `run`."""

    def stage(self, name: StageName) -> tuple[JobName, ...]:
        if name not in self.stages:
            die(
                f"unknown stage '{name}' for set {self.name}",
                f"choose one of: {', '.join(self.stage_order)}",
            )
        return self.stages[name]

    def next_stage(self, after: StageName) -> StageName | None:
        """The stage that normally follows, so `run` can suggest the next step."""
        order = list(self.stage_order)
        index = order.index(after)
        return order[index + 1] if index + 1 < len(order) else None

    def pick_stage(self, green: set[StageName]) -> StageName | None:
        """The stage `run` should trigger next, given which stages are already green.

        This is what lets `run <folder>` be one verb: the first not-yet-green stage
        whose producer (if any) is green. None means everything is green.
        """
        for stage in self.stage_order:
            if stage in green:
                continue
            required = self.consumes.get(stage)
            if required and required not in green:
                continue
            return stage
        return None


# The live registry. SETS is a read-only view over it, so every importer sees
# updates made by initialize() without holding a mutable reference themselves.
# Empty until initialize() finds a sets file: the tool ships no sets of its own.
_REGISTRY: dict[str, JobSet] = {}
SETS: Mapping[str, JobSet] = MappingProxyType(_REGISTRY)
_config_file: Path | None = None

_REQUIRED_KEYS = frozenset({"yaml_dir", "jobs", "stages", "stage_order"})
_ALLOWED_KEYS = _REQUIRED_KEYS | {"consumes"}


def _bad(where: str, name: str, cause: str, fix: str) -> NoReturn:
    die(f"invalid set '{name}' in {where}: {cause}", fix)


def _checked_name(value: object, where: str, name: str, what: str) -> str:
    if not isinstance(value, str) or not SAFE_NAME.fullmatch(value):
        _bad(
            where,
            name,
            f"{what} {value!r} is not a safe name",
            "use only letters, digits, dot, dash and underscore, starting with a "
            "letter or digit, at most 64 characters. These become Jenkins URL path "
            "segments, so the charset is deliberately strict",
        )
    return value


def _checked_yaml_dir(value: object, where: str, name: str) -> str:
    if not isinstance(value, str) or not value:
        _bad(where, name, "yaml_dir must be a non-empty string", 'e.g. "pxb/v2/jenkins"')
    segments = value.split("/")
    if value.startswith("/") or "\\" in value or ".." in segments or "" in segments:
        _bad(
            where,
            name,
            f"yaml_dir {value!r} must be a plain repo-relative path",
            "no leading slash, no backslash, no '..' and no empty segments",
        )
    return value


def _parse_set(name: str, data: object, where: str) -> JobSet:
    _checked_name(name, where, name, "set name")
    if not isinstance(data, dict):
        _bad(
            where,
            name,
            "the definition must be a JSON object",
            "see `jenkins-preview sets --example`",
        )
    if unknown := set(data) - _ALLOWED_KEYS:
        _bad(
            where,
            name,
            f"unknown key(s): {', '.join(sorted(unknown))}",
            f"allowed keys: {', '.join(sorted(_ALLOWED_KEYS))}. A typo here would "
            "otherwise change behaviour silently, so unknown keys are refused",
        )
    if missing := _REQUIRED_KEYS - set(data):
        _bad(
            where,
            name,
            f"missing key(s): {', '.join(sorted(missing))}",
            "see `jenkins-preview sets --example` for a complete definition",
        )

    yaml_dir = _checked_yaml_dir(data["yaml_dir"], where, name)

    jobs_raw = data["jobs"]
    if not isinstance(jobs_raw, list) or not jobs_raw:
        _bad(where, name, "jobs must be a non-empty list", "list every job the set publishes")
    jobs = tuple(_checked_name(j, where, name, "job name") for j in jobs_raw)
    if len(set(jobs)) != len(jobs):
        _bad(where, name, "jobs contains duplicates", "each job may appear once")

    stages_raw = data["stages"]
    if not isinstance(stages_raw, dict) or not stages_raw:
        _bad(where, name, "stages must be a non-empty object", '{"<stage>": ["<job>", ...]}')
    stages: dict[str, tuple[str, ...]] = {}
    for stage_name, stage_jobs in stages_raw.items():
        _checked_name(stage_name, where, name, "stage name")
        if not isinstance(stage_jobs, list) or not stage_jobs:
            _bad(
                where,
                name,
                f"stage '{stage_name}' must list at least one job",
                "give the stage a non-empty list of job names, or remove it",
            )
        for j in stage_jobs:
            if j not in jobs:
                _bad(
                    where,
                    name,
                    f"stage '{stage_name}' references {j!r}, which is not in jobs",
                    "every stage job must also appear in the set's jobs list",
                )
        stages[stage_name] = tuple(stage_jobs)

    order_raw = data["stage_order"]
    if not isinstance(order_raw, list) or not all(isinstance(x, str) for x in order_raw):
        _bad(
            where,
            name,
            "stage_order must be a list of stage names",
            'write it as a JSON array of strings, e.g. ["build", "test"]',
        )
    order = tuple(order_raw)
    if len(set(order)) != len(order) or set(order) != set(stages):
        _bad(
            where,
            name,
            "stage_order must list every stage exactly once",
            f"stages defined: {', '.join(sorted(stages))}",
        )

    consumes_raw = data.get("consumes", {})
    if not isinstance(consumes_raw, dict):
        _bad(where, name, "consumes must be an object", '{"<consumer-stage>": "<producer-stage>"}')
    for consumer, producer in consumes_raw.items():
        if consumer not in stages or producer not in stages:
            _bad(
                where,
                name,
                f"consumes maps '{consumer}' -> '{producer}', but both must be stages",
                f"stages defined: {', '.join(sorted(stages))}",
            )
        if order.index(producer) >= order.index(consumer):
            _bad(
                where,
                name,
                f"stage '{consumer}' consumes '{producer}', which does not come before it",
                "producers must appear earlier in stage_order than their consumers",
            )

    return JobSet(
        name=name,
        yaml_dir=yaml_dir,
        jobs=jobs,
        stages=MappingProxyType(stages),
        stage_order=order,
        consumes=MappingProxyType(dict(consumes_raw)),
    )


def parse_sets_document(doc: object, where: str) -> dict[str, JobSet]:
    """Validate a whole sets file."""
    if not isinstance(doc, dict) or set(doc) != {"sets"} or not isinstance(doc["sets"], dict):
        die(
            f'{where} must be a JSON object with exactly one top-level key, "sets"',
            '{"sets": {"<name>": {...}}}. `jenkins-preview sets --example` prints a valid file',
        )
    return {name: _parse_set(name, data, where) for name, data in doc["sets"].items()}


def _git_toplevel() -> Path | None:
    probe = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False
    )
    top = probe.stdout.strip()
    return Path(top) if probe.returncode == 0 and top else None


def _discover(explicit: str | None) -> Path | None:
    """The one config file to use, or None. An explicitly named file must exist."""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            die(f"sets file not found: {path}", "check the --sets path")
        return path
    if env := os.environ.get(CONFIG_ENV):
        path = Path(env).expanduser()
        if not path.is_file():
            die(
                f"${CONFIG_ENV} points at a missing file: {path}",
                f"fix the path or unset {CONFIG_ENV}",
            )
        return path
    if (top := _git_toplevel()) and (path := top / REPO_CONFIG).is_file():
        return path
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    if (path := xdg / "jenkins-preview" / "sets.json").is_file():
        return path
    return None


def initialize(explicit: str | None = None) -> None:
    """Load the sets from the one discovered file. No file means no sets."""
    global _config_file
    _REGISTRY.clear()
    _config_file = _discover(explicit)
    if _config_file is None:
        return
    try:
        text = _config_file.read_text("utf-8-sig")  # some editors prepend a BOM
    except OSError as exc:
        die(f"cannot read {_config_file}: {exc}", "check the file permissions")
    if not text.strip():
        # `sets --example > .jenkins-preview.json` truncates the file to zero bytes
        # before the tool even starts, so an empty file must mean "not configured
        # yet", never an error. An empty file cannot change behaviour silently.
        _config_file = None
        return
    try:
        doc = json.loads(text, object_pairs_hook=_refuse_duplicate_keys)
    except json.JSONDecodeError as exc:
        die(
            f"{_config_file} is not valid JSON: {exc}",
            "fix the syntax. `jenkins-preview sets --example` prints a valid file",
        )
    _REGISTRY.update(parse_sets_document(doc, str(_config_file)))


def _refuse_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    """JSON keeps the last of two identical keys, so a duplicated set name would
    swap definitions silently. Refused instead, at any nesting depth."""
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            die(
                f"duplicate key {key!r} in the sets file",
                "every name may appear once. Remove the extra entry",
            )
        seen[key] = value
    return seen


def config_file() -> Path | None:
    """The sets file the current registry was loaded from, if any."""
    return _config_file


def pick_set(name: str) -> JobSet:
    if not SETS:
        if _config_file:
            die(
                f"the sets file {_config_file} defines no sets",
                f"add '{name}' to it. `jenkins-preview sets --example` shows the format",
            )
        die(
            "no sets file found, and the tool ships none",
            "create one at your pipelines checkout root: "
            f"`jenkins-preview sets --example > {REPO_CONFIG}`. Locations tried, in "
            "order: " + ", ".join(SEARCH_ORDER),
        )
    if name not in SETS:
        die(
            f"unknown set '{name}' (sets file: {_config_file})",
            f"choose one of: {', '.join(sorted(SETS))}, or add it to the file. "
            "`jenkins-preview sets --example` shows the format",
        )
    return SETS[name]
