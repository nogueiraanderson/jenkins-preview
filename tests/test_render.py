"""Staging and rendering (gate G4), including runs of the real JJB.

Staging is pure filesystem work and is tested directly. The render tests run
the actual jenkins-jobs binary this package depends on, per the rendered-
artifact rule: a hand-transcription of what JJB would produce proves nothing
about what it does produce. No network is involved either way.
"""

from pathlib import Path

import pytest

from jenkins_preview.errors import Fail
from jenkins_preview.render import copy_definitions, discover_names, render
from jenkins_preview.sets import JobSet

MINIMAL_JOB = """- job:
    name: alpha-compile
    description: minimal job for render tests
"""

SUPPORT_ONLY = """- defaults:
    name: global
    description: a defaults block every job implicitly references
"""

UNRELATED_JOB = """- job:
    name: unrelated-job
    description: defined in the same directory, not requested
"""

TEMPLATE_ONLY = """- job-template:
    name: alpha-compile
    description: a template nothing instantiates renders nothing
"""


def job_set(jobs: tuple[str, ...] = ("alpha-compile",)) -> JobSet:
    return JobSet(
        name="demo",
        yaml_dir="jjb",
        jobs=jobs,
        stages={"all": jobs},
        stage_order=("all",),
    )


def write_yaml(tmp_path: Path, **files: str) -> Path:
    source = tmp_path / "jjb"
    source.mkdir(exist_ok=True)
    for name, text in files.items():
        (source / f"{name}.yaml").write_text(text)
    return source


PIPELINE_JOB = """- job:
    name: alpha-pipeline
    project-type: pipeline
    pipeline-scm:
      scm:
        - git:
            url: https://github.com/example/repo
            branches:
            - "master"
      script-path: pipe.groovy
"""


MACRO_ONLY = """- builder:
    name: shared-steps
    builders:
      - shell: "echo shared"
"""

MACRO_USER = """- job:
    name: needs-macro
    builders:
      - shared-steps
"""

JOB_AND_VIEW = """- job:
    name: real-job
    description: shares a file with a view
- view:
    name: Some View
    view-type: list
"""


def test_discover_names_maps_every_rendered_job_to_its_root(tmp_path) -> None:
    source = write_yaml(tmp_path, a=MINIMAL_JOB, b=PIPELINE_JOB, c=UNRELATED_JOB)
    names, skipped, unpublishable = discover_names(source)
    assert names == {
        "alpha-compile": "project",
        "alpha-pipeline": "flow-definition",
        "unrelated-job": "project",
    }
    assert skipped == []
    assert unpublishable == []


def test_discover_names_refuses_a_directory_that_renders_nothing(tmp_path) -> None:
    source = write_yaml(tmp_path, a=TEMPLATE_ONLY)
    with pytest.raises(Fail, match="rendered zero jobs"):
        discover_names(source)


def test_discover_names_refuses_a_missing_directory(tmp_path) -> None:
    with pytest.raises(Fail, match="not a directory"):
        discover_names(tmp_path / "nope")


def test_discover_names_skips_and_names_files_with_their_reason(tmp_path) -> None:
    """One broken definition never hides the rest. It is skipped with the JJB
    reason, never a substituted one: the real pxb 2.4 files fail on unescaped
    interpolation, which is fixable, not on some grouping problem."""
    source = write_yaml(tmp_path, a=MINIMAL_JOB, broken="- job:\n    name: '{unresolved}'\n")
    names, skipped, _ = discover_names(source)
    assert "alpha-compile" in names
    assert len(skipped) == 1
    file_name, reason = skipped[0]
    assert file_name == "broken.yaml"
    assert "unresolved" in reason, "the note must carry JJB's own words"
    assert "/tmp" not in reason, "the throwaway staging path is noise"


def test_discover_names_stages_macro_files_as_support(tmp_path) -> None:
    """Every JJB support block carries a name key, so classifying on name lines
    would misfile macros as definitions and wrongly skip the jobs needing them."""
    source = write_yaml(tmp_path, macros=MACRO_ONLY, job=MACRO_USER)
    names, skipped, _ = discover_names(source)
    assert names == {"needs-macro": "project"}
    assert skipped == []


def test_discover_names_refuses_a_symlink_escape(tmp_path) -> None:
    outside = tmp_path / "outside.yaml"
    outside.write_text(MINIMAL_JOB)
    source = write_yaml(tmp_path, a=UNRELATED_JOB)
    (source / "linked.yaml").symlink_to(outside)
    with pytest.raises(Fail, match="escapes the yaml directory"):
        discover_names(source)


def test_discover_names_refuses_an_include_tag(tmp_path) -> None:
    source = write_yaml(tmp_path, a="- job:\n    name: inc-job\n    builders: !include inc.yaml\n")
    with pytest.raises(Fail, match="!include"):
        discover_names(source)


def test_discover_names_refuses_a_job_defined_twice(tmp_path) -> None:
    """Two files defining one job would let the draft reference whichever
    renders last, the same refusal copy_definitions makes at publish."""
    source = write_yaml(tmp_path, one=MINIMAL_JOB, two=MINIMAL_JOB)
    with pytest.raises(Fail, match="defined more than once"):
        discover_names(source)


def test_discover_names_leaves_out_views_and_unsafe_names(tmp_path) -> None:
    """A view is not a job and 'Some View' is not a safe name. Both are
    reported, neither aborts the draft, and neither reaches `jobs`."""
    source = write_yaml(tmp_path, mixed=JOB_AND_VIEW)
    names, _, unpublishable = discover_names(source)
    assert names == {"real-job": "project"}
    assert len(unpublishable) == 1
    assert "Some View" in unpublishable[0]


def test_discover_names_reports_a_missing_jjb_not_broken_yaml(tmp_path, monkeypatch) -> None:
    """A missing renderer must say so, not blame every file in the directory."""
    from jenkins_preview import render as render_module

    def gone() -> str:
        raise Fail("jenkins-jobs (JJB) not found in this environment", "install the tool with uv")

    monkeypatch.setattr(render_module, "jjb_binary", gone)
    source = write_yaml(tmp_path, a=MINIMAL_JOB)
    with pytest.raises(Fail, match="not found in this environment"):
        discover_names(source)


def test_discover_names_ignores_a_directory_named_like_yaml(tmp_path) -> None:
    source = write_yaml(tmp_path, a=MINIMAL_JOB)
    (source / "sub.yaml").mkdir()
    names, _, _ = discover_names(source)
    assert names == {"alpha-compile": "project"}


def multijob_referencing(name: str, target: str) -> str:
    return f"""- job:
    name: {name}
    project-type: multijob
    builders:
      - multijob:
          name: phase-one
          projects:
            - name: {target}
"""


def test_discover_names_leaves_out_jobs_referencing_outside_the_draft(tmp_path) -> None:
    """The same scan gate G7 runs at publish: a job whose config references a
    name that never rendered can never publish, so the draft leaves it out
    with the reason instead of printing a file `up` refuses."""
    source = write_yaml(tmp_path, a=MINIMAL_JOB, m=multijob_referencing("chain-multijob", "ghost"))
    names, _, unpublishable = discover_names(source)
    assert names == {"alpha-compile": "project"}
    assert any("chain-multijob" in entry and "ghost" in entry for entry in unpublishable)


def test_discover_names_reference_filter_reaches_a_fixed_point(tmp_path) -> None:
    """Dropping a job can strand a job that referenced it, all the way down."""
    source = write_yaml(
        tmp_path,
        a=MINIMAL_JOB,
        outer=multijob_referencing("outer-multijob", "inner-multijob"),
        inner=multijob_referencing("inner-multijob", "ghost"),
    )
    names, _, unpublishable = discover_names(source)
    assert names == {"alpha-compile": "project"}
    assert any("inner-multijob" in entry for entry in unpublishable)
    assert any("outer-multijob" in entry for entry in unpublishable)


def test_discover_names_leaves_out_foldered_names(tmp_path) -> None:
    """JJB renders `name: nested/job` as a nested directory. Reported, not a
    crash and not a draft entry."""
    source = write_yaml(tmp_path, a=MINIMAL_JOB, b="- job:\n    name: nested/job\n")
    names, _, unpublishable = discover_names(source)
    assert names == {"alpha-compile": "project"}
    assert any("nested" in entry for entry in unpublishable)


def test_copy_definitions_takes_definitions_and_support_only(tmp_path) -> None:
    source = write_yaml(tmp_path, wanted=MINIMAL_JOB, support=SUPPORT_ONLY, unrelated=UNRELATED_JOB)
    staging = tmp_path / "staging"
    copy_definitions(source, ("alpha-compile",), staging)
    staged = {path.name for path in staging.iterdir()}
    assert staged == {"wanted.yaml", "support.yaml"}, (
        "an unrelated definition must never be staged, or its breakage blocks the set"
    )


def test_copy_definitions_dies_when_a_requested_job_is_undefined(tmp_path) -> None:
    source = write_yaml(tmp_path, wanted=MINIMAL_JOB)
    with pytest.raises(Fail, match="not defined anywhere"):
        copy_definitions(source, ("alpha-compile", "ghost-job"), tmp_path / "staging")


def test_render_dies_when_the_yaml_dir_is_missing(tmp_path) -> None:
    with pytest.raises(Fail, match="does not exist at this ref"):
        render(tmp_path, job_set(), tmp_path / "out")


def test_render_produces_config_xml_with_the_real_jjb(tmp_path) -> None:
    write_yaml(tmp_path, wanted=MINIMAL_JOB)
    rendered = render(tmp_path, job_set(), tmp_path / "out")
    assert set(rendered) == {"alpha-compile"}
    assert "minimal job for render tests" in rendered["alpha-compile"]
    assert rendered["alpha-compile"].lstrip().startswith("<?xml")


def test_render_dies_when_jjb_succeeds_but_renders_nothing(tmp_path) -> None:
    """JJB exits 0 for a template nothing instantiates. The name regex staged the
    file, so the gap must be caught after the render, per gate G4."""
    write_yaml(tmp_path, wanted=TEMPLATE_ONLY)
    with pytest.raises(Fail, match="did not render at this ref"):
        render(tmp_path, job_set(), tmp_path / "out")


def test_render_dies_when_jjb_fails(tmp_path) -> None:
    """Brokenness in an UNSTAGED file is invisible by design (the staging test
    above), so the invalid YAML must sit in the staged file itself."""
    write_yaml(tmp_path, wanted=MINIMAL_JOB + "    builders: [\n")
    with pytest.raises(Fail, match="JJB failed to render"):
        render(tmp_path, job_set(), tmp_path / "out")


def test_a_job_defined_in_two_files_is_refused(tmp_path) -> None:
    """Staging one of the two would silently render whichever file sorts last."""
    write_yaml(tmp_path, alpha=MINIMAL_JOB, beta=MINIMAL_JOB)
    with pytest.raises(Fail, match=r"defined more than once.*alpha\.yaml and beta\.yaml"):
        copy_definitions(tmp_path / "jjb", ("alpha-compile",), tmp_path / "staging")


def test_an_include_tag_is_refused(tmp_path) -> None:
    """JJB resolves !include relative to the staged copy, where the target is
    absent, so the render would quietly drop content."""
    write_yaml(
        tmp_path,
        wanted=MINIMAL_JOB
        + "    builders:\n      - shell: !include-raw-verbatim: scripts/build.sh\n",
    )
    with pytest.raises(Fail, match="include tag"):
        copy_definitions(tmp_path / "jjb", ("alpha-compile",), tmp_path / "staging")


def test_a_symlinked_definition_escaping_the_directory_is_refused(tmp_path) -> None:
    outside = tmp_path / "outside.yaml"
    outside.write_text(MINIMAL_JOB)
    source = write_yaml(tmp_path, decoy=UNRELATED_JOB)
    (source / "linked.yaml").symlink_to(outside)
    with pytest.raises(Fail, match="escapes the yaml directory"):
        copy_definitions(source, ("alpha-compile",), tmp_path / "staging")
