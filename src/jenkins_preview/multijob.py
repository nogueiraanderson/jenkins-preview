"""Guard against the multijob plugin's folder-blind delete listener.

jenkins-multijob-plugin ships an ItemListener (MultiJobListener) whose
onDeleted matches the deleted item's SHORT name against every top-level
MultiJobProject's phase references, strips the matching phases, drops a
builder that emptied, and saves. It never looks at the full path, so deleting
a preview COPY inside /previews rewrites the production job that shares the
name. onRenamed has the same blindness and rewrites the reference instead,
which is why the repair below re-posts a snapshot and never renames anything.

Publishing same-named copies is harmless; DELETING them is what mutates
production. Two defences:

- `assert_no_phase_collisions` refuses an `up` whose set names collide with a
  live phase reference, so no new preview can arm the listener.
- `collision_snapshot` + `repair_stripped_projects` bracket the teardown
  delete: previews published before this guard existed can still collide, so
  the affected projects are captured first and restored if the delete
  stripped them.

Every check fails closed: a config this tool cannot read is a collision it
cannot rule out.
"""

import xml.etree.ElementTree as ET
from collections.abc import Iterable

from .errors import die, say

MULTIJOB_CLASS = "com.tikal.jenkins.plugins.multijob.MultiJobProject"
_PHASE_JOB_TAG = "com.tikal.jenkins.plugins.multijob.PhaseJobsConfig"


def _phase_pairs(config_xml: str, job: str) -> set[tuple[str, str]]:
    """(phaseName, referenced jobName) pairs of one multijob config."""
    try:
        root = ET.fromstring(config_xml)
    except ET.ParseError as exc:
        die(
            f"cannot parse the config of multijob {job}: {exc}",
            "refusing to continue: a collision with it cannot be ruled out",
        )
    pairs: set[tuple[str, str]] = set()
    for builder in root.iter():
        if not builder.tag.endswith("MultiJobBuilder"):
            continue
        phase = (builder.findtext("phaseName") or "").strip()
        for config in builder.iter(_PHASE_JOB_TAG):
            name = (config.findtext("jobName") or "").strip()
            if name:
                pairs.add((phase, name))
    return pairs


def _top_level_multijobs(jenkins) -> list[str]:
    data = jenkins.get_json("", tree="jobs[name,_class]")
    return [job["name"] for job in data.get("jobs", []) if job.get("_class") == MULTIJOB_CLASS]


def _config_of(jenkins, job: str) -> str:
    status, body = jenkins.get_text(f"/job/{job}/config.xml")
    if status != 200:
        die(
            f"cannot read the config of multijob {job} (HTTP {status})",
            "the collision check needs it. Ask a Jenkins admin, or run against "
            "a master without MultiJob projects",
        )
    return body


def phase_references(jenkins) -> dict[str, set[str]]:
    """Job names referenced by each top-level MultiJobProject's phases."""
    refs: dict[str, set[str]] = {}
    for job in _top_level_multijobs(jenkins):
        pairs = _phase_pairs(_config_of(jenkins, job), job)
        if pairs:
            refs[job] = {name for _, name in pairs}
    return refs


def assert_no_phase_collisions(jenkins, names: Iterable[str]) -> None:
    """Refuse to publish copies whose deletion would strip production phases."""
    wanted = set(names)
    collisions = [
        f"{job}: {', '.join(sorted(wanted & referenced))}"
        for job, referenced in sorted(phase_references(jenkins).items())
        if wanted & referenced
    ]
    if collisions:
        listed = "; ".join(collisions)
        die(
            f"the set collides with live MultiJob phase references: {listed}",
            "the multijob plugin strips a phase whenever ANY job with that short "
            "name is deleted, even a copy inside /previews, so tearing this "
            "preview down would rewrite those production jobs. Rename the jobs "
            "in the set, or preview them on a master without MultiJob projects",
        )


def collision_snapshot(jenkins, names: Iterable[str]) -> dict[str, str]:
    """Configs of every top-level multijob a deletion of `names` could strip."""
    wanted = set(names)
    snapshot: dict[str, str] = {}
    for job, referenced in phase_references(jenkins).items():
        if wanted & referenced:
            snapshot[job] = _config_of(jenkins, job)
    return snapshot


def repair_stripped_projects(jenkins, snapshot: dict[str, str]) -> list[str]:
    """Restore every snapshotted multijob whose phases the delete stripped."""
    repaired: list[str] = []
    for job, config_xml in sorted(snapshot.items()):
        before = _phase_pairs(config_xml, job)
        after = _phase_pairs(_config_of(jenkins, job), job)
        if after == before:
            continue
        lost = ", ".join(sorted(name for _, name in before - after))
        say(f"NOTE   the delete stripped phases from {job} (multijob plugin bug): {lost}")
        jenkins.restore_job_config(job, config_xml)
        restored = _phase_pairs(_config_of(jenkins, job), job)
        if restored != before:
            die(
                f"restoring {job} did not bring its phases back",
                f"restore it by hand from jobConfigHistory, expected phases: {sorted(before)}",
            )
        say(f"repaired {job}: {len(before)} phase references restored")
        repaired.append(job)
    return repaired
