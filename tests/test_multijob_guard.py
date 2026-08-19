"""The multijob collision gate must fire on a collision and repair a strip.

The multijob plugin's delete listener matches deleted items by SHORT name
against top-level MultiJobProject phase references (folder-blind), so deleting
a same-named preview copy rewrites production. These tests need no network:
a fake client returns canned configs and records restores.
"""

import pytest

from jenkins_preview.client import assert_restore_path
from jenkins_preview.errors import Fail
from jenkins_preview.multijob import (
    MULTIJOB_CLASS,
    assert_no_phase_collisions,
    collision_snapshot,
    phase_references,
    repair_stripped_projects,
)


def multijob_xml(*phases: tuple[str, str]) -> str:
    builders = "".join(
        f"""
    <com.tikal.jenkins.plugins.multijob.MultiJobBuilder>
      <phaseName>{phase}</phaseName>
      <continuationCondition>SUCCESSFUL</continuationCondition>
      <phaseJobs>
        <com.tikal.jenkins.plugins.multijob.PhaseJobsConfig>
          <jobName>{job}</jobName>
        </com.tikal.jenkins.plugins.multijob.PhaseJobsConfig>
      </phaseJobs>
    </com.tikal.jenkins.plugins.multijob.MultiJobBuilder>"""
        for phase, job in phases
    )
    return f"""<?xml version='1.1' encoding='UTF-8'?>
<com.tikal.jenkins.plugins.multijob.MultiJobProject>
  <builders>{builders}
  </builders>
</com.tikal.jenkins.plugins.multijob.MultiJobProject>"""


WIPED = multijob_xml()
INTACT = multijob_xml(("compile", "widget-compile"), ("test", "widget-test"))


class FakeJenkins:
    """get_json lists jobs, get_text serves configs, restores are recorded."""

    def __init__(self, jobs: dict[str, tuple[str, str]]) -> None:
        # name -> (_class, config.xml). Config None-like "" means HTTP 403.
        self.jobs = jobs
        self.restored: list[str] = []

    def get_json(self, path: str, tree: str | None = None) -> dict:
        assert path == ""
        return {"jobs": [{"name": name, "_class": cls} for name, (cls, _) in self.jobs.items()]}

    def get_text(self, path: str) -> tuple[int, str]:
        name = path.removeprefix("/job/").removesuffix("/config.xml")
        cls_config = self.jobs.get(name)
        if cls_config is None or not cls_config[1]:
            return 403, ""
        return 200, cls_config[1]

    def restore_job_config(self, job: str, config_xml: str) -> None:
        self.restored.append(job)
        self.jobs[job] = (self.jobs[job][0], config_xml)


def test_collision_refused_naming_both_sides() -> None:
    jenkins = FakeJenkins({"widget-multijob": (MULTIJOB_CLASS, INTACT)})
    with pytest.raises(Fail) as caught:
        assert_no_phase_collisions(jenkins, ["widget-compile", "unrelated"])
    assert "widget-multijob" in str(caught.value)
    assert "widget-compile" in str(caught.value)
    assert "unrelated" not in str(caught.value).split("references:")[1].split(";")[0]


def test_no_multijobs_is_quiet() -> None:
    jenkins = FakeJenkins({"plain-job": ("hudson.model.FreeStyleProject", "")})
    assert_no_phase_collisions(jenkins, ["widget-compile"])
    assert phase_references(jenkins) == {}


def test_disjoint_names_pass() -> None:
    jenkins = FakeJenkins({"widget-multijob": (MULTIJOB_CLASS, INTACT)})
    assert_no_phase_collisions(jenkins, ["other-a", "other-b"])


def test_unreadable_config_fails_closed() -> None:
    jenkins = FakeJenkins({"widget-multijob": (MULTIJOB_CLASS, "")})
    with pytest.raises(Fail) as caught:
        assert_no_phase_collisions(jenkins, ["anything"])
    assert "HTTP 403" in str(caught.value)


def test_repair_restores_a_stripped_project() -> None:
    jenkins = FakeJenkins({"widget-multijob": (MULTIJOB_CLASS, INTACT)})
    snapshot = collision_snapshot(jenkins, ["widget-compile"])
    assert set(snapshot) == {"widget-multijob"}
    jenkins.jobs["widget-multijob"] = (MULTIJOB_CLASS, WIPED)  # the listener struck
    assert repair_stripped_projects(jenkins, snapshot) == ["widget-multijob"]
    assert jenkins.restored == ["widget-multijob"]
    assert jenkins.jobs["widget-multijob"][1] == INTACT


def test_repair_never_writes_when_nothing_changed() -> None:
    jenkins = FakeJenkins({"widget-multijob": (MULTIJOB_CLASS, INTACT)})
    snapshot = collision_snapshot(jenkins, ["widget-compile"])
    assert repair_stripped_projects(jenkins, snapshot) == []
    assert jenkins.restored == []


def test_snapshot_skips_unaffected_projects() -> None:
    jenkins = FakeJenkins({"widget-multijob": (MULTIJOB_CLASS, INTACT)})
    assert collision_snapshot(jenkins, ["other"]) == {}


def test_restore_gate_accepts_only_one_top_level_config() -> None:
    assert_restore_path("/job/widget-multijob/config.xml", "widget-multijob")
    with pytest.raises(Fail):
        assert_restore_path("/job/a/job/b/config.xml", "b")
    with pytest.raises(Fail):
        assert_restore_path("/job/widget-multijob/doDelete", "widget-multijob")
    with pytest.raises(Fail):
        assert_restore_path("/job/../config.xml", "..")
