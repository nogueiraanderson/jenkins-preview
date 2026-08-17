"""Rendering job definitions with Jenkins Job Builder.

Only the requested jobs are rendered. Feeding JJB a whole directory means one
unrelated definition that does not render standalone blocks every other set,
which is a real failure and not a hypothetical one.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .errors import Fail, die
from .names import SAFE_NAME
from .sets import JobSet
from .transform import cross_job_references, publishable_root

SUPPORT_BLOCK = re.compile(
    r"^- (defaults|job-template|macro|builder|wrapper|publisher):", re.MULTILINE
)
# Top-level blocks that render jobs. Support blocks also carry a name: key, so
# matching on name lines alone would classify every macro file as a definition.
DEFINITION_BLOCK = re.compile(r"^- (job|project|job-group):", re.MULTILINE)
JOB_NAME = re.compile(r"^\s+name:\s*['\"]?([^'\"\s]+)", re.MULTILINE)


def jjb_binary() -> str:
    """Locate the jenkins-jobs console script inside this environment."""
    candidate = Path(sys.executable).parent / "jenkins-jobs"
    if candidate.exists():
        return str(candidate)
    if found := shutil.which("jenkins-jobs"):
        return found
    die(
        "jenkins-jobs (JJB) not found in this environment",
        "install the tool with uv so the pinned jenkins-job-builder and setuptools "
        "are present, or run it with `uvx --from . jenkins-preview`",
    )
    raise AssertionError  # unreachable, die() raised; ruff RET503 cannot see NoReturn


def _read_yaml(source: Path, path: Path) -> str:
    """Read one YAML file, refusing what the staging step cannot carry."""
    if source.resolve() not in path.resolve().parents:
        # A symlink here could read (and stage) a file from anywhere on disk.
        die(
            f"{path.name} escapes the yaml directory via a symlink",
            "keep job definitions as plain files inside the directory",
        )
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if "!include" in text:
        # JJB resolves include tags relative to the STAGED copy, where the
        # target does not exist, so the render would quietly drop content.
        die(
            f"{path.name} uses a JJB !include tag, which the copy step cannot carry",
            "inline the included content, or restructure the definition",
        )
    return text


def copy_definitions(source: Path, jobs: tuple[str, ...], dest: Path) -> None:
    """Copy only the definitions for `jobs`, plus any support blocks they need.

    Files are matched by the job names inside them, not by file name, because a
    file name does not always match the job it defines.
    """
    dest.mkdir(parents=True, exist_ok=True)
    wanted = set(jobs)
    owners: dict[str, list[Path]] = {}
    support: list[Path] = []

    for path in sorted(source.glob("*.y*ml")):
        if not path.is_file():
            # A directory named like a YAML file, or a broken symlink.
            continue
        text = _read_yaml(source, path)
        names = set(JOB_NAME.findall(text))
        if hit := names & wanted:
            for job in hit:
                owners.setdefault(job, []).append(path)
        elif SUPPORT_BLOCK.search(text):
            # defaults, macros and templates are needed by whatever references them.
            # A job-template alone renders nothing without a project referencing it.
            support.append(path)

    if duplicated := {job: paths for job, paths in owners.items() if len(paths) > 1}:
        # Staging one of the files would silently render whichever sorts last.
        listing = ", ".join(
            f"'{job}' in {' and '.join(path.name for path in paths)}"
            for job, paths in sorted(duplicated.items())
        )
        die(
            f"defined more than once: {listing}",
            "keep each job in exactly one file, or the preview could render the wrong one",
        )
    defining = {job: paths[0] for job, paths in owners.items()}

    if missing := wanted - set(defining):
        die(
            f"these jobs are not defined anywhere in {source.name} at this ref: "
            f"{', '.join(sorted(missing))} (gate G4)",
            "check the SETS table matches the job definitions on this branch",
        )

    for path in {*defining.values(), *support}:
        shutil.copy2(path, dest / path.name)


def _run_jjb(source: Path, outdir: Path) -> None:
    """One hermetic JJB invocation, shared by render and discovery.

    --conf os.devnull: JJB otherwise honours ~/.config/jenkins_jobs and
    $JJB_CONF, so two machines could render the same commit differently.
    """
    result = subprocess.run(
        [
            jjb_binary(),
            "--conf",
            os.devnull,
            "test",
            "--config-xml",
            "-o",
            str(outdir),
            str(source),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-12:]
        die(
            "JJB failed to render the job definitions at this ref:\n    " + "\n    ".join(tail),
            "a template, macro or defaults block is unresolved at this commit. Fix the "
            "YAML on your branch, then re-run doctor",
        )


def _skip_reason(exc: Fail, file_name: str) -> str:
    """The most informative line of a JJB failure, for a one-line skip note."""
    lines = [line.strip() for line in exc.cause.splitlines() if line.strip()]
    # JJB error lines lead with file:line:col:, the rest is code frame and caret.
    for line in reversed(lines):
        if found := re.search(r"([^/\s]+\.ya?ml):(\d+:\d+: .*)", line):
            # The dropped path prefix names only the throwaway staging directory.
            # The file name is dropped too when it is the probed file itself,
            # which the note already leads with.
            named, rest = found.group(1), found.group(2)
            return rest if named == file_name else f"{named}:{rest}"
    return lines[-1] if lines else "did not render"


def _stage(files: set[Path], tmp: str) -> tuple[Path, Path]:
    """(staged input dir, output dir) for one hermetic JJB run under `tmp`."""
    staged = Path(tmp) / "in"
    staged.mkdir()
    for path in files:
        shutil.copy2(path, staged / path.name)
    return staged, Path(tmp) / "out"


def discover_names(source: Path) -> tuple[dict[str, str], list[tuple[str, str]], list[str]]:
    """What the directory renders: publishable jobs mapped to their config root
    element, files that do not render each with its JJB reason, and rendered
    names the tool cannot publish (views, unsafe names).

    A per-file probe next to the directory's support blocks (defaults, macros,
    templates) decides the skip list, naming each failure, never silently
    dropping it. The job names come from ONE render of every surviving file
    together, exactly what publishing the full draft stages.
    """
    if not source.is_dir():
        die(
            f"{source} is not a directory",
            "pass a repo-relative directory holding JJB YAML, like pxb/v2/jenkins",
        )
    # Resolved once up front: a missing JJB must say so, not masquerade as
    # every file failing its render probe.
    jjb_binary()

    definitions: list[Path] = []
    support: list[Path] = []
    for path in sorted(source.glob("*.y*ml")):
        if not path.is_file():
            # A directory named like a YAML file, or a broken symlink.
            continue
        text = _read_yaml(source, path)
        if DEFINITION_BLOCK.search(text):
            definitions.append(path)
        elif SUPPORT_BLOCK.search(text):
            support.append(path)

    renderable: list[Path] = []
    skipped: list[tuple[str, str]] = []
    owners: dict[str, str] = {}
    duplicated: dict[str, tuple[str, str]] = {}
    for definition in definitions:
        with tempfile.TemporaryDirectory() as tmp:
            staged, outdir = _stage({definition, *support}, tmp)
            try:
                _run_jjb(staged, outdir)
            except Fail as exc:
                skipped.append((definition.name, _skip_reason(exc, definition.name)))
                continue
            renderable.append(definition)
            for entry in sorted(outdir.iterdir()):
                if entry.name in owners:
                    duplicated[entry.name] = (owners[entry.name], definition.name)
                else:
                    owners[entry.name] = definition.name

    if duplicated:
        # The combined render would refuse these too, but with a raw JJB tail.
        listing = ", ".join(
            f"'{name}' in {first} and {second}"
            for name, (first, second) in sorted(duplicated.items())
        )
        die(
            f"defined more than once: {listing}",
            "keep each job in exactly one file, or the preview could render the wrong one",
        )
    if not renderable:
        reasons = "".join(f"\n    {name}: {reason}" for name, reason in skipped)
        die(
            f"{source} rendered zero jobs" + reasons,
            "the directory holds no definition a set could reference (nested "
            "directories are not scanned). If you expected jobs here, check you "
            "stand inside your pipelines clone and the path names its YAML "
            "directory, like pxb/v2/jenkins",
        )

    names: dict[str, str] = {}
    unpublishable: list[str] = []
    parsed: dict[str, ET.Element] = {}
    with tempfile.TemporaryDirectory() as tmp:
        staged, outdir = _stage({*renderable, *support}, tmp)
        try:
            _run_jjb(staged, outdir)
        except Fail as exc:
            # Every file passed its own probe, so this is a cross-file conflict.
            die(
                "the files render alone but not together:\n    "
                + "\n    ".join(exc.cause.splitlines()),
                "two files define a clashing template, macro or defaults block",
            )
        for entry in sorted(outdir.iterdir()):
            config = entry / "config.xml" if entry.is_dir() else entry
            if not config.is_file():
                # A job name with a slash renders as a nested directory.
                unpublishable.append(f"{entry.name!r} (not a flat job name)")
                continue
            element = ET.parse(config).getroot()
            if not publishable_root(element.tag):
                unpublishable.append(f"{entry.name} <{element.tag}>")
            elif not SAFE_NAME.fullmatch(entry.name):
                unpublishable.append(f"{entry.name!r} (not a safe name)")
            else:
                names[entry.name] = element.tag
                parsed[entry.name] = element

    # The same scan gate G7 runs at publish: a job whose config references a
    # name outside the draft can never publish, so it leaves the draft with its
    # reason. Dropping one can strand another, hence the loop to a fixed point.
    while True:
        outside = None
        for name in sorted(names):
            for reference, _tag in cross_job_references(parsed[name]):
                head = reference.strip().split("/", 1)[0]
                if head and head not in names:
                    outside = (name, head)
                    break
            if outside:
                break
        if not outside:
            break
        name, head = outside
        unpublishable.append(f"{name} (references {head}, which is not in this draft)")
        del names[name]

    if not names:
        left_out = f" (left out: {', '.join(unpublishable)})" if unpublishable else ""
        die(
            f"{source} rendered no job this tool can publish{left_out}",
            "only pipeline, freestyle, matrix and multijob configs with safe names "
            "can be proven pinned to your fork",
        )
    return names, skipped, unpublishable


def render(workdir: Path, job_set: JobSet, outdir: Path) -> dict[str, str]:
    """Render the requested jobs to config XML. Gate G4 on any failure."""
    source = workdir / job_set.yaml_dir
    if not source.is_dir():
        die(
            f"{job_set.yaml_dir} does not exist at this ref",
            "check the ref actually contains the job definitions",
        )

    copydir = outdir.parent / "staging"
    copy_definitions(source, job_set.jobs, copydir)

    _run_jjb(copydir, outdir)

    rendered: dict[str, str] = {}
    for job in job_set.jobs:
        config = outdir / job / "config.xml"
        if not config.exists():
            # JJB can exit 0 having written nothing at all, for instance when every
            # staged file was a template with nothing instantiating it.
            produced = (
                sorted(path.name for path in outdir.iterdir() if path.is_dir())
                if outdir.is_dir()
                else []
            )
            die(
                f"job '{job}' did not render at this ref (gate G4)",
                f"JJB produced: {', '.join(produced[:12])}"
                + ("..." if len(produced) > 12 else "")
                + ". A partially published set would hang a consumer stage, so nothing "
                "was published",
            )
        rendered[job] = config.read_text()
    return rendered
