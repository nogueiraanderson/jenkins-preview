"""The sets loader: no shipped sets, file discovery, merge-free load, validation."""

import json
import subprocess
from pathlib import Path

import pytest

from jenkins_preview.cli import _prescan_sets
from jenkins_preview.errors import Fail
from jenkins_preview.sets import (
    CONFIG_ENV,
    SETS,
    config_file,
    initialize,
    parse_sets_document,
    pick_set,
)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Neutralise every discovery rung, then restore the suite-wide example sets.

    The test process runs inside a git checkout that could legitimately carry a
    .jenkins-preview.json, and the developer may have an XDG config. Both would
    leak into assertions, so cwd moves to an empty non-repo dir and XDG is pointed
    somewhere empty.
    """
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    yield
    # monkeypatch unpatches only after this fixture finishes, so the teardown
    # restore must neutralise every rung itself, then reload the suite sets
    # other test modules (test_gates.py) rely on.
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    from conftest import PXB_SETS_FILE

    initialize(PXB_SETS_FILE)


def valid_set(prefix: str = "demo-job") -> dict:
    return {
        "yaml_dir": "demo/jenkins",
        "jobs": [f"{prefix}-compile", f"{prefix}-test"],
        "stages": {"compile": [f"{prefix}-compile"], "test": [f"{prefix}-test"]},
        "stage_order": ["compile", "test"],
        "consumes": {"test": "compile"},
    }


def write_config(path, sets: dict) -> str:
    path.write_text(json.dumps({"sets": sets}))
    return str(path)


# --------------------------------------------------------------------------- #
# nothing ships with the tool
# --------------------------------------------------------------------------- #


def test_no_file_means_no_sets():
    initialize(None)
    assert dict(SETS) == {}
    assert config_file() is None


def test_pick_set_without_a_file_names_every_search_location():
    initialize(None)
    with pytest.raises(Fail, match="no sets file found") as caught:
        pick_set("pxb-8.1")
    message = str(caught.value)
    assert "--sets PATH" in message
    assert CONFIG_ENV in message
    assert ".jenkins-preview.json at the root of the checkout" in message
    assert "~/.config/jenkins-preview/sets.json" in message
    assert "sets --example" in message


def test_pick_set_with_an_empty_file_points_at_that_file(tmp_path):
    path = write_config(tmp_path / "sets.json", {})
    initialize(path)
    with pytest.raises(Fail, match="defines no sets"):
        pick_set("pxb-8.1")


def test_the_file_defines_exactly_its_sets(tmp_path):
    path = write_config(tmp_path / "sets.json", {"my-set": valid_set()})
    initialize(path)
    assert set(SETS) == {"my-set"}
    assert str(config_file()) == path


def test_the_pxb_fixture_round_trips_and_carries_the_pxb_sets():
    from conftest import PXB_SETS_FILE

    parsed = parse_sets_document(json.loads(Path(PXB_SETS_FILE).read_text()), "fixture")
    assert set(parsed) == {"pxb-8.0", "pxb-8.1", "pxb-9.x"}
    job_set = parsed["pxb-8.1"]
    assert job_set.yaml_dir == "pxb/v2/jenkins"
    assert len(job_set.jobs) == 6
    assert job_set.stage_order == ("compile", "test")
    assert job_set.consumes["test"] == "compile"
    assert job_set.stages["compile"] == ("percona-xtrabackup-8.1-compile-pipeline",)


# --------------------------------------------------------------------------- #
# discovery precedence
# --------------------------------------------------------------------------- #


def test_explicit_path_beats_env(tmp_path, monkeypatch):
    flag = write_config(tmp_path / "flag.json", {"from-flag": valid_set()})
    env = write_config(tmp_path / "env.json", {"from-env": valid_set()})
    monkeypatch.setenv(CONFIG_ENV, env)
    initialize(flag)
    assert set(SETS) == {"from-flag"}


def test_env_beats_repo_file(tmp_path, monkeypatch):
    env = write_config(tmp_path / "env.json", {"from-env": valid_set()})
    monkeypatch.setenv(CONFIG_ENV, env)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    write_config(tmp_path / ".jenkins-preview.json", {"from-repo": valid_set()})
    initialize(None)
    assert set(SETS) == {"from-env"}


def test_repo_file_is_found_from_anywhere_inside_the_checkout(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    write_config(tmp_path / ".jenkins-preview.json", {"from-repo": valid_set()})
    inner = tmp_path / "deep" / "inside"
    inner.mkdir(parents=True)
    monkeypatch.chdir(inner)
    initialize(None)
    assert set(SETS) == {"from-repo"}


def test_xdg_file_is_the_last_rung(tmp_path):
    xdg_file = tmp_path / "xdg" / "jenkins-preview" / "sets.json"
    xdg_file.parent.mkdir(parents=True)
    write_config(xdg_file, {"from-xdg": valid_set()})
    initialize(None)
    assert set(SETS) == {"from-xdg"}


def test_missing_explicit_path_dies(tmp_path):
    with pytest.raises(Fail, match="sets file not found"):
        initialize(str(tmp_path / "nope.json"))


def test_env_pointing_at_a_missing_file_dies(tmp_path, monkeypatch):
    monkeypatch.setenv(CONFIG_ENV, str(tmp_path / "gone.json"))
    with pytest.raises(Fail, match="points at a missing file"):
        initialize(None)


def test_broken_json_dies_with_the_path(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text('{"sets": ')
    with pytest.raises(Fail, match="not valid JSON"):
        initialize(str(path))


def test_an_empty_file_counts_as_no_config(tmp_path):
    """`sets --example > .jenkins-preview.json` truncates the file before the tool
    starts. The zero-byte file discovery then finds must not kill that command."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".jenkins-preview.json").write_text("")
    initialize(None)
    assert config_file() is None
    assert dict(SETS) == {}


def test_sets_example_survives_a_broken_config(tmp_path, capsys):
    """--example is the repair path for the very file whose load just failed,
    so it must draft anyway."""
    from jenkins_preview.cli import main

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    jjb = tmp_path / "ci" / "jjb"
    jjb.mkdir(parents=True)
    (jjb / "jobs.yaml").write_text("- job:\n    name: repair-me\n")
    (tmp_path / ".jenkins-preview.json").write_text("{broken")
    assert main(["sets", "--example"]) == 0
    assert '"repair-me"' in capsys.readouterr().out
    assert main(["sets"]) == 1


def test_sets_listing_without_a_file_prints_the_locations_and_fails(capsys):
    from jenkins_preview.cli import main

    assert main(["sets"]) == 1
    out = capsys.readouterr().out
    assert "no sets file found" in out
    assert ".jenkins-preview.json" in out
    assert "sets --example" in out


# --------------------------------------------------------------------------- #
# validation, one rule per test
# --------------------------------------------------------------------------- #


def reject(sets_value, match: str):
    with pytest.raises(Fail, match=match):
        parse_sets_document({"sets": sets_value}, "test-file")


def test_document_must_have_exactly_the_sets_key():
    with pytest.raises(Fail, match="exactly one top-level key"):
        parse_sets_document({"sets": {}, "extra": 1}, "test-file")
    with pytest.raises(Fail, match="exactly one top-level key"):
        parse_sets_document(["not", "an", "object"], "test-file")


def test_set_name_must_be_safe():
    reject({"../evil": valid_set()}, "not a safe name")


def test_unknown_keys_are_refused():
    bad = valid_set() | {"stage_orders": []}
    reject({"x": bad}, "unknown key")


def test_missing_keys_are_refused():
    bad = valid_set()
    del bad["stages"]
    reject({"x": bad}, "missing key")


def test_yaml_dir_traversal_is_refused():
    for evil in ("../outside", "/etc", "a//b", "a\\b"):
        bad = valid_set() | {"yaml_dir": evil}
        reject({"x": bad}, "repo-relative")


def test_job_names_must_be_safe():
    bad = valid_set()
    bad["jobs"] = ["ok-job", "job/with/slash"]
    reject({"x": bad}, "not a safe name")


def test_duplicate_jobs_are_refused():
    bad = valid_set()
    bad["jobs"] = ["twice", "twice"]
    bad["stages"] = {"compile": ["twice"]}
    bad["stage_order"] = ["compile"]
    bad["consumes"] = {}
    reject({"x": bad}, "duplicates")


def test_stage_job_must_be_in_jobs():
    bad = valid_set()
    bad["stages"]["compile"] = ["not-published"]
    reject({"x": bad}, "not in jobs")


def test_stage_order_must_cover_every_stage():
    bad = valid_set()
    bad["stage_order"] = ["compile"]
    reject({"x": bad}, "every stage exactly once")


def test_consumes_must_reference_stages():
    bad = valid_set()
    bad["consumes"] = {"test": "package"}
    reject({"x": bad}, "both must be stages")


def test_consumer_cannot_precede_its_producer():
    bad = valid_set()
    bad["stage_order"] = ["test", "compile"]
    reject({"x": bad}, "does not come before")


# --------------------------------------------------------------------------- #
# the pxb fixture the suite is parametrised over
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# the --sets pre-scan
# --------------------------------------------------------------------------- #


def test_prescan_finds_the_flag_in_both_spellings():
    assert _prescan_sets(["up", "--sets", "a.json"]) == "a.json"
    assert _prescan_sets(["--sets=b.json", "up"]) == "b.json"
    assert _prescan_sets(["up", "--set", "pxb-8.1"]) is None
    assert _prescan_sets(["--sets", "a.json", "up", "--sets=b.json"]) == "b.json"


# --------------------------------------------------------------------------- #
# --example drafts through discovery
# --------------------------------------------------------------------------- #


def _checkout_with(tmp_path: Path, dirs: dict[str, str]) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for rel, text in dirs.items():
        target = tmp_path / rel
        target.mkdir(parents=True, exist_ok=True)
        (target / "jobs.yaml").write_text(text)
    return tmp_path


def _demo_job(name: str) -> str:
    return f"- job:\n    name: {name}\n    description: autodetect fixture\n"


def test_example_flag_drafts_from_the_checkout(tmp_path, monkeypatch, capsys):
    """--example is discovery with the directory found for the user: same
    machinery, no document of its own, nothing hardcoded in this package."""
    from jenkins_preview.cli import main

    _checkout_with(tmp_path, {"ci/jjb": _demo_job("demo-solo")})
    monkeypatch.chdir(tmp_path)
    assert main(["sets", "--example"]) == 0
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document["sets"]["ci-jjb"]["jobs"] == ["demo-solo"]
    assert "yaml dir: ci/jjb" in captured.err


def test_example_flag_refuses_outside_a_checkout(tmp_path, monkeypatch, capsys):
    from jenkins_preview.cli import main

    monkeypatch.chdir(tmp_path)
    assert main(["sets", "--example"]) == 1
    assert "not a git checkout" in capsys.readouterr().err


def test_example_refuses_a_checkout_without_definitions(tmp_path, monkeypatch, capsys):
    """Running --example in the wrong repo (the tool's own clone, a dotfiles
    repo) must say the checkout is wrong, not send the user hunting for a
    directory with --discover that cannot exist there."""
    from jenkins_preview.cli import main

    _checkout_with(tmp_path, {})
    monkeypatch.chdir(tmp_path)
    assert main(["sets", "--example"]) == 1
    err = capsys.readouterr().err
    assert "no JJB job definitions found" in err
    assert "not your pipelines clone" in err


def test_example_yaml_dir_narrows_by_cwd(tmp_path, monkeypatch):
    from jenkins_preview.commands import _example_yaml_dir

    _checkout_with(tmp_path, {"pxb/jjb": _demo_job("pxb-a"), "pxc/jjb": _demo_job("pxc-a")})
    monkeypatch.chdir(tmp_path / "pxb")
    assert _example_yaml_dir(tmp_path) == ("pxb/jjb", "under your working directory")


def test_example_yaml_dir_narrows_by_branch(tmp_path, monkeypatch):
    """From the checkout root the branch's own edits pick the directory: the
    developer testing a pipeline change never has to name what they changed.
    The edit counts uncommitted, before `git add` even, because that is the
    state a developer mid-change is actually in."""
    from jenkins_preview.commands import _example_yaml_dir

    _checkout_with(tmp_path, {"pxb/jjb": _demo_job("pxb-a"), "pxc/jjb": _demo_job("pxc-a")})
    git = ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    subprocess.run([*git, "update-ref", "refs/remotes/origin/master", "HEAD"], check=True)
    (tmp_path / "pxb" / "jjb" / "jobs.yaml").write_text(_demo_job("pxb-edited"))
    monkeypatch.chdir(tmp_path)
    assert _example_yaml_dir(tmp_path) == ("pxb/jjb", "this branch edits it")


def test_example_yaml_dir_resolves_nested_candidates_to_the_deepest(tmp_path, monkeypatch):
    """Candidates nest in real checkouts (pxb holds pxb/v2/jenkins). An edit
    deep in one directory must pick that directory, not its whole ancestry."""
    from jenkins_preview.commands import _example_yaml_dir

    _checkout_with(tmp_path, {"pxb": _demo_job("shallow"), "pxb/v2/jjb": _demo_job("deep")})
    git = ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    subprocess.run([*git, "update-ref", "refs/remotes/origin/master", "HEAD"], check=True)
    (tmp_path / "pxb" / "v2" / "jjb" / "jobs.yaml").write_text(_demo_job("deep-edited"))
    monkeypatch.chdir(tmp_path)
    assert _example_yaml_dir(tmp_path) == ("pxb/v2/jjb", "this branch edits it")


def test_example_yaml_dir_ignores_edits_outside_candidates(tmp_path, monkeypatch):
    """An edit in a scripts directory NEXT TO a job directory must list the
    candidates, never guess an ancestor that happens to hold unrelated YAML."""
    from jenkins_preview.commands import _example_yaml_dir

    _checkout_with(tmp_path, {"pxb": _demo_job("shallow"), "pxb/v2/jjb": _demo_job("deep")})
    git = ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    subprocess.run([*git, "update-ref", "refs/remotes/origin/master", "HEAD"], check=True)
    scripts = tmp_path / "pxb" / "v2" / "docker"
    scripts.mkdir(parents=True)
    (scripts / "run-test").write_text("# edited\n")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Fail, match="--discover pxb/v2/jjb"):
        _example_yaml_dir(tmp_path)


def test_example_yaml_dir_lists_candidates_when_ambiguous(tmp_path, monkeypatch):
    from jenkins_preview.commands import _example_yaml_dir

    _checkout_with(tmp_path, {"pxb/jjb": _demo_job("pxb-a"), "pxc/jjb": _demo_job("pxc-a")})
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Fail, match="--discover pxb/jjb") as caught:
        _example_yaml_dir(tmp_path)
    assert "--discover pxc/jjb" in str(caught.value)


# --------------------------------------------------------------------------- #
# --set inference: the file or the checkout answers for an omitted --set
# --------------------------------------------------------------------------- #


def _two_dir_sets(tmp_path) -> str:
    return write_config(
        tmp_path / "two.json",
        {
            "pxb-set": {**valid_set("pxb-job"), "yaml_dir": "pxb/jjb"},
            "pxc-set": {**valid_set("pxc-job"), "yaml_dir": "pxc/jjb"},
        },
    )


def test_infer_set_takes_the_only_one(tmp_path, capsys):
    from jenkins_preview.commands import _infer_set

    initialize(write_config(tmp_path / "one.json", {"solo": valid_set()}))
    assert _infer_set() == "solo"
    assert "set: solo (the only one in the sets file)" in capsys.readouterr().err


def test_infer_set_without_a_file_points_at_example(tmp_path):
    from jenkins_preview.commands import _infer_set

    initialize(None)
    with pytest.raises(Fail, match="sets --example"):
        _infer_set()


def test_infer_set_narrows_by_the_branch(tmp_path, monkeypatch, capsys):
    """With several sets, the one whose directory this branch edits wins, the
    same signal that picks the directory for --example."""
    from jenkins_preview.commands import _infer_set

    initialize(_two_dir_sets(tmp_path))
    _checkout_with(tmp_path, {"pxb/jjb": _demo_job("pxb-a"), "pxc/jjb": _demo_job("pxc-a")})
    git = ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    subprocess.run([*git, "update-ref", "refs/remotes/origin/master", "HEAD"], check=True)
    (tmp_path / "pxc" / "jjb" / "jobs.yaml").write_text(_demo_job("pxc-edited"))
    monkeypatch.chdir(tmp_path)
    assert _infer_set() == "pxc-set"
    assert "set: pxc-set (this branch edits its directory)" in capsys.readouterr().err


def test_infer_set_refuses_when_nothing_singles_one_out(tmp_path, monkeypatch):
    """Two sets on ONE directory (the pxb 8.0/8.1 slice case) cannot be told
    apart by any checkout signal, so the refusal lists them."""
    from jenkins_preview.commands import _infer_set

    initialize(
        write_config(
            tmp_path / "slices.json",
            {"pxb-8.0": valid_set("a"), "pxb-8.1": valid_set("b")},
        )
    )
    _checkout_with(tmp_path, {"demo/jenkins": _demo_job("demo-a")})
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Fail, match=r"pxb-8\.0, pxb-8\.1"):
        _infer_set()


def test_sets_flag_is_accepted_after_any_subcommand(tmp_path, capsys):
    """`--sets` after the subcommand must parse: the pre-scan already honours it
    there, so argparse rejecting the natural spelling was pure friction."""
    from jenkins_preview.cli import main

    path = write_config(tmp_path / "anywhere.json", {"my-set": valid_set()})
    assert main(["sets", "--sets", path]) == 0
    assert "my-set" in capsys.readouterr().out


def test_duplicate_json_keys_are_refused(tmp_path):
    """json.loads keeps the last duplicate key, which would swap a set silently."""
    path = tmp_path / "dup.json"
    path.write_text(
        '{"sets": {"twin": '
        + json.dumps(valid_set())
        + ', "twin": '
        + json.dumps(valid_set("other"))
        + "}}"
    )
    with pytest.raises(Fail, match="duplicate key 'twin'"):
        initialize(str(path))


def test_a_bom_prefixed_sets_file_loads(tmp_path):
    path = tmp_path / "bom.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"sets": {"bommed": valid_set()}}).encode())
    initialize(str(path))
    assert set(SETS) == {"bommed"}


def test_help_and_version_survive_a_broken_discovered_config(tmp_path, monkeypatch, capsys):
    from jenkins_preview.cli import main

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".jenkins-preview.json").write_text("{broken")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as caught:
        main(["--help"])
    assert caught.value.code == 0
    assert "doctor" in capsys.readouterr().out


def test_discover_document_drafts_a_loadable_file(tmp_path) -> None:
    """The draft loads through this tool's own parser, lists every rendered job,
    and stages only the pipeline jobs as the starting point."""
    from jenkins_preview.commands import discover_document

    jjb = tmp_path / "ci" / "jjb"
    jjb.mkdir(parents=True)
    (jjb / "jobs.yaml").write_text(
        """- job:
    name: demo-support
    description: freestyle support job
- job:
    name: demo-pipeline
    project-type: pipeline
    pipeline-scm:
      scm:
        - git:
            url: https://github.com/example/repo
            branches:
            - "master"
      script-path: pipe.groovy
"""
    )
    document = json.loads(discover_document(tmp_path, "ci/jjb"))
    (name, drafted) = next(iter(document["sets"].items()))
    assert name == "ci-jjb"
    assert drafted["jobs"] == ["demo-pipeline", "demo-support"]
    assert drafted["stages"] == {"main": ["demo-pipeline"]}
    assert drafted["stage_order"] == ["main"]


def test_discover_document_accepts_a_trailing_slash(tmp_path) -> None:
    """Tab completion appends one, refusing it would be pure friction."""
    from jenkins_preview.commands import discover_document

    jjb = tmp_path / "ci" / "jjb"
    jjb.mkdir(parents=True)
    (jjb / "jobs.yaml").write_text(_demo_job("slashed"))
    document = json.loads(discover_document(tmp_path, "ci/jjb/"))
    assert document["sets"]["ci-jjb"]["yaml_dir"] == "ci/jjb"


def test_sets_discover_survives_a_broken_config(tmp_path, monkeypatch, capsys):
    """--discover repairs a broken sets file exactly like --example does."""
    from jenkins_preview.cli import main

    _checkout_with(tmp_path, {"ci/jjb": _demo_job("repair-two")})
    (tmp_path / ".jenkins-preview.json").write_text("{broken")
    monkeypatch.chdir(tmp_path)
    assert main(["sets", "--discover", "ci/jjb"]) == 0
    assert '"repair-two"' in capsys.readouterr().out


def test_example_and_discover_refuse_to_combine(capsys):
    from jenkins_preview.cli import main

    with pytest.raises(SystemExit) as caught:
        main(["sets", "--example", "--discover", "ci/jjb"])
    assert caught.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


def test_discover_document_refuses_traversal(tmp_path) -> None:
    from jenkins_preview.commands import discover_document

    with pytest.raises(Fail, match="relative to the checkout root"):
        discover_document(tmp_path, "../evil")
