"""Orchestration tests for commands.py against an in-memory Jenkins.

The gates have unit tests. Nothing exercised the sequencing that calls them:
publish order, rollback order, teardown refusals, stage picking, reaping. These
tests fake Jenkins at the urllib opener seam, so the real `Jenkins._request`
and both write gates (G1, G11) run untouched on every call.

A known defect gets an xfail test asserting the DESIRED behaviour (none are
open today), and its fix flips the xfail to XPASS(strict).
"""

import argparse
import io
import json
import re
import subprocess
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET

import pytest

from jenkins_preview import cli, commands
from jenkins_preview.client import Jenkins
from jenkins_preview.errors import Fail
from jenkins_preview.folders import folder_marker, view_marker
from jenkins_preview.sets import pick_set

FORK = "https://github.com/someone/jenkins-pipelines"
SHA = "a" * 40
BASE = "https://jenkins.test"

RAW_PIPELINE = """<flow-definition>
  <definition class="org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition">
    <scm class="hudson.plugins.git.GitSCM">
      <userRemoteConfigs>
        <hudson.plugins.git.UserRemoteConfig>
          <url>https://github.com/upstream/jenkins-pipelines</url>
          <refspec>+refs/heads/main:refs/remotes/origin/main</refspec>
        </hudson.plugins.git.UserRemoteConfig>
      </userRemoteConfigs>
      <branches>
        <hudson.plugins.git.BranchSpec><name>master</name></hudson.plugins.git.BranchSpec>
      </branches>
    </scm>
    <lightweight>true</lightweight>
    <scriptPath>pipe.groovy</scriptPath>
  </definition>
  <triggers/>
  <description>original</description>
</flow-definition>"""


class _Response:
    def __init__(self, status: int, body: str = "") -> None:
        self.status = status
        self._body = body.encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeJenkinsServer:
    """In-memory Jenkins reachable only through `open()`, urllib's seam.

    State is plain dicts. Two switches simulate the failure modes the publish
    flow must survive: an enable whose read-back stays disabled, and a root
    view that resolves a job outside the preview folder.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.requests: list[tuple[str, str, bytes, str | None]] = []
        self.folders: dict[str, dict] = {}
        self.views: dict[str, dict] = {}
        self.fail_enable_readback = False
        self.view_lists_stray = False
        self.race_on_create = False
        self.no_previews_root = False
        self.fail_rename = False
        # The delete lands server-side but the response is lost: the ambiguous
        # outcome the swap probes for.
        self.fail_teardown_after_delete = False
        # A full outage: the delete never lands AND every folder read after it
        # fails, so the swap's probe cannot answer either way.
        self.fail_teardown_and_probe = False
        self._outage = False
        # After this many parameter-property reads, the job reads as
        # parameterless: the check-to-trigger race in one switch.
        self.drop_params_after_reads: int | None = None
        self._param_reads = 0

    # -- seeding ------------------------------------------------------------ #

    def seed_folder(
        self,
        name: str,
        description: str,
        jobs: dict[str, dict] | None = None,
    ) -> None:
        self.folders[name] = {"description": description, "jobs": jobs or {}}

    @staticmethod
    def job(
        *,
        building: bool = False,
        last_success: int | None = None,
        parameters: bool | str | list = False,
    ) -> dict:
        return {
            "xml": RAW_PIPELINE,
            "buildable": True,
            "building": building,
            "last_success": last_success,
            "parameters": parameters,
        }

    # -- the urllib seam ---------------------------------------------------- #

    def open(self, request, timeout=None) -> _Response:
        method = request.get_method()
        full = request.full_url.removeprefix(BASE)
        self.calls.append((method, full))
        self.requests.append(
            (method, full, request.data or b"", request.get_header("Content-type"))
        )
        split = urllib.parse.urlsplit(full)
        params = urllib.parse.parse_qs(split.query)
        parts = [urllib.parse.unquote(p) for p in split.path.strip("/").split("/")]
        body = (request.data or b"").decode()
        return self._route(method, parts, params, body)

    def _route(self, method: str, parts: list[str], params: dict, body: str) -> _Response:
        match method, parts:
            case "POST", ["createView"]:
                name = params["name"][0]
                self.views[name] = {"description": ET.fromstring(body).findtext("description")}
                return _Response(200)
            case "POST", ["view", name, "doDelete"]:
                self.views.pop(name, None)
                return _Response(200)
            case "GET", ["view", name, "api", "json"]:
                if (view := self.views.get(name)) is None:
                    return _Response(404)
                return self._json(
                    {"description": view["description"], "jobs": self._view_jobs(name)}
                )
            case "POST", ["job", "previews", "createItem"]:
                name = params["name"][0]
                if self.race_on_create or name in self.folders:
                    # Real Jenkins answers createItem for an existing name with 400.
                    return _Response(400)
                self.folders[name] = {
                    "description": ET.fromstring(body).findtext("description"),
                    "jobs": {},
                }
                return _Response(200)
            case "GET", ["me", "api", "json"]:
                return self._json({"id": "alice"})
            case "GET", ["job", "previews", "api", "json"]:
                if self.no_previews_root:
                    return _Response(404)
                listing = [
                    {"name": name, "description": folder["description"]}
                    for name, folder in self.folders.items()
                ]
                return self._json({"jobs": listing})
            case _, ["job", "previews", "job", folder, *rest]:
                return self._folder(method, folder, rest, params, body)
        return _Response(404)

    def _folder(
        self, method: str, name: str, rest: list[str], params: dict, body: str
    ) -> _Response:
        folder = self.folders.get(name)
        if folder is None:
            return _Response(404)
        match method, rest:
            case "GET", ["api", "json"]:
                if self._outage:
                    raise urllib.error.URLError("still unreachable")
                jobs = [
                    {"name": job_name, "color": "blue", "lastBuild": self._last_build(job)}
                    for job_name, job in folder["jobs"].items()
                ]
                return self._json(
                    {"name": name, "description": folder["description"], "jobs": jobs}
                )
            case "POST", ["createItem"]:
                folder["jobs"][params["name"][0]] = {
                    "xml": body,
                    "buildable": False,
                    "building": False,
                    "last_success": None,
                }
                return _Response(200)
            case "POST", ["doDelete"]:
                if self.fail_teardown_and_probe:
                    self._outage = True
                    raise urllib.error.URLError("connection lost, delete never confirmed")
                del self.folders[name]
                if self.fail_teardown_after_delete:
                    raise urllib.error.URLError("connection lost after the delete landed")
                return _Response(200)
            case "POST", ["confirmRename"]:
                if self.fail_rename:
                    return _Response(500)
                self.folders[params["newName"][0]] = self.folders.pop(name)
                return _Response(302)
            case _, ["job", job_name, *tail]:
                if (job := folder["jobs"].get(job_name)) is None:
                    return _Response(404)
                return self._job(method, job, tail)
        return _Response(404)

    def _job(self, method: str, job: dict, tail: list[str]) -> _Response:
        match method, tail:
            case "GET", ["config.xml"]:
                return _Response(200, job["xml"])
            case "GET", ["api", "json"]:
                success = {"number": job["last_success"]} if job["last_success"] else None
                parameters = job.get("parameters")
                if parameters and self.drop_params_after_reads is not None:
                    self._param_reads += 1
                    if self._param_reads > self.drop_params_after_reads:
                        parameters = False
                if parameters == "empty":
                    definitions: list[dict] = []
                elif isinstance(parameters, list):
                    definitions = parameters
                else:
                    definitions = [{"name": "X"}]
                props = (
                    [
                        {
                            "_class": "hudson.model.ParametersDefinitionProperty",
                            "parameterDefinitions": definitions,
                        }
                    ]
                    if parameters
                    else []
                )
                return self._json(
                    {
                        "buildable": job["buildable"],
                        "lastSuccessfulBuild": success,
                        "lastBuild": self._last_build(job),
                        "property": props,
                    }
                )
            case "POST", ["enable"]:
                job["buildable"] = not self.fail_enable_readback
                return _Response(200)
            case "POST", ["buildWithParameters"]:
                # Real Jenkins refuses this endpoint on a parameterless job.
                if not job.get("parameters"):
                    return _Response(400)
                job["building"] = True
                return _Response(200)
            case "POST", ["build"]:
                # And this one on a parameterized job ("Nothing is submitted").
                if job.get("parameters"):
                    return _Response(400)
                job["building"] = True
                return _Response(200)
        return _Response(404)

    def _view_jobs(self, name: str) -> list[dict]:
        folder = self.folders.get(name, {"jobs": {}})
        jobs = [
            {"name": job, "url": f"{BASE}/job/previews/job/{name}/job/{job}/"}
            for job in folder["jobs"]
        ]
        if self.view_lists_stray:
            jobs.append({"name": "a-production-job", "url": f"{BASE}/job/a-production-job/"})
        return jobs

    @staticmethod
    def _last_build(job: dict) -> dict | None:
        if job["building"]:
            return {"number": 9, "building": True}
        if job["last_success"]:
            return {"number": job["last_success"], "building": False, "result": "SUCCESS"}
        return None

    @staticmethod
    def _json(payload: dict) -> _Response:
        return _Response(200, json.dumps(payload))


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def server() -> FakeJenkinsServer:
    return FakeJenkinsServer()


@pytest.fixture
def jenkins(server: FakeJenkinsServer) -> Jenkins:
    client = Jenkins(BASE, "alice", "token")
    client._opener = server  # the only faked layer. _request and gates stay real
    return client


@pytest.fixture
def offline_git(monkeypatch: pytest.MonkeyPatch):
    """`up` without network or git: any ref resolves, rendering serves the set,
    and the fake checkout carries a clean pipeline script for the fidelity scan."""
    monkeypatch.setattr(commands, "resolve_ref", lambda repo, ref: (SHA, f"refs/heads/{ref}"))

    def fake_checkout(repo, sha, anchor, dest) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "pipe.groovy").write_text("echo 'clean'\n")

    monkeypatch.setattr(commands, "checkout_at", fake_checkout)
    monkeypatch.setattr(
        commands,
        "render",
        lambda workdir, job_set, outdir: dict.fromkeys(job_set.jobs, RAW_PIPELINE),
    )


def up_args(**overrides) -> argparse.Namespace:
    defaults = {
        "set": "pxb-8.1",
        "repo": FORK,
        "ref": "topic",
        "name": None,
        "dry_run": False,
        "update": False,
        "root_view": False,
        "allow_foreign_fetch": False,
    }
    return argparse.Namespace(**{**defaults, **overrides})


FOLDER = "preview-alice-pxb-8.1-topic"
PATH = f"/job/previews/job/{FOLDER}"


def marker(view: str = "none", foreign: int = 0, **overrides) -> str:
    fields = {
        "preview_id": "cafe" * 8,
        "user": "alice",
        "job_set": "pxb-8.1",
        "repo": FORK,
        "sha": SHA,
        "anchor": "refs/heads/topic",
        "view": view,
        "foreign": foreign,
        # Current publishes always stamp the as-typed ref. Legacy-marker tests
        # pass ref="" explicitly to drop the field.
        "ref": "topic",
    }
    return folder_marker(**{**fields, **overrides})


def aged(text: str, created: str) -> str:
    """Rewrite the marker's creation stamp. Markers always carry one."""
    replaced = re.sub(r"created=\S+", f"created={created}", text)
    assert f"created={created}" in replaced, "stamp rewrite did not land"
    return replaced


# --------------------------------------------------------------------------- #
# Folder inference: the checkout answers for an omitted folder argument
# --------------------------------------------------------------------------- #


def _checkout_on_branch(tmp_path, monkeypatch, branch: str = "topic", origin: str = "") -> None:
    subprocess.run(["git", "init", "-q", "-b", branch, str(tmp_path)], check=True)
    if origin:
        subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", origin], check=True)
    monkeypatch.chdir(tmp_path)


def test_status_infers_the_folder_from_the_checkout(
    jenkins, server, tmp_path, monkeypatch, capsys
) -> None:
    """Standing in the checkout, the tool re-finds the preview it minted: the
    marker's owner, repo and anchor are the authority, so --name folders
    match too."""
    server.seed_folder(FOLDER, marker(), {"pxb-job": server.job()})
    _checkout_on_branch(tmp_path, monkeypatch, origin=FORK)
    rc = commands.status(jenkins, argparse.Namespace(folder=None))
    captured = capsys.readouterr()
    assert rc == 0
    assert f"folder: {FOLDER} (your preview of branch topic)" in captured.err
    assert FOLDER in captured.out


def test_folder_inference_refuses_ambiguity(jenkins, server, tmp_path, monkeypatch) -> None:
    server.seed_folder(FOLDER, marker())
    server.seed_folder("preview-alice-other-topic", marker(job_set="other"))
    _checkout_on_branch(tmp_path, monkeypatch, origin=FORK)
    with pytest.raises(Fail, match="2 previews of yours"):
        commands.status(jenkins, argparse.Namespace(folder=None))


def test_folder_inference_skips_swap_temps(jenkins, server, tmp_path, monkeypatch) -> None:
    """A swap temp records the FINAL folder as its view, disagreeing with its
    own name: that corroboration, not the name alone, makes it invisible."""
    server.seed_folder(FOLDER, marker(), {"pxb-job": server.job()})
    server.seed_folder(f"{FOLDER[:55]}-swabc123", marker(view=FOLDER))
    _checkout_on_branch(tmp_path, monkeypatch, origin=FORK)
    rc = commands.status(jenkins, argparse.Namespace(folder=None))
    assert rc == 0


def test_folder_inference_needs_an_origin(jenkins, server, tmp_path, monkeypatch) -> None:
    """Without an origin the repo leg of the match cannot be verified, and a
    half-verified guess must not pick a deletion target."""
    server.seed_folder(FOLDER, marker())
    subprocess.run(["git", "init", "-q", "-b", "topic", str(tmp_path)], check=True)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Fail, match="no origin remote"):
        commands.status(jenkins, argparse.Namespace(folder=None))


def test_folder_inference_needs_a_checkout(jenkins, server, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Fail, match="not a git checkout"):
        commands.status(jenkins, argparse.Namespace(folder=None))


def test_folder_inference_with_no_matching_preview_says_up_first(
    jenkins, server, tmp_path, monkeypatch
) -> None:
    server.seed_folder(FOLDER, marker(ref="another-branch", anchor="refs/heads/another-branch"))
    _checkout_on_branch(tmp_path, monkeypatch, origin=FORK)
    with pytest.raises(Fail, match="run `jenkins-preview up` first"):
        commands.down(jenkins, argparse.Namespace(folder=None, force=False, yes=False))


def test_folder_inference_keeps_a_lookalike_name(jenkins, server, tmp_path, monkeypatch) -> None:
    """A --name folder that merely LOOKS like a swap temp records itself (or
    none) as its view, so it stays inferrable for its whole life."""
    lookalike = "preview-alice-hotfix-swabc123"
    server.seed_folder(lookalike, marker(view=lookalike), {"pxb-job": server.job()})
    _checkout_on_branch(tmp_path, monkeypatch, origin=FORK)
    rc = commands.status(jenkins, argparse.Namespace(folder=None))
    assert rc == 0


def test_folder_inference_skips_another_forks_preview(
    jenkins, server, tmp_path, monkeypatch
) -> None:
    """Two clones of different forks can share a branch name. The other fork's
    preview must never be selected, let alone deleted, from this checkout."""
    server.seed_folder(FOLDER, marker(repo="https://github.com/other/jenkins-pipelines"))
    _checkout_on_branch(tmp_path, monkeypatch, origin=FORK)
    with pytest.raises(Fail, match="run `jenkins-preview up` first"):
        commands.status(jenkins, argparse.Namespace(folder=None))


def test_folder_inference_matches_the_anchor_not_the_spelling(
    jenkins, server, tmp_path, monkeypatch
) -> None:
    """The anchor is the resolved ref: a preview published as
    `--ref refs/heads/topic` matches branch topic, a TAG named topic never
    does."""
    server.seed_folder(FOLDER, marker(ref="refs/heads/topic"), {"pxb-job": server.job()})
    _checkout_on_branch(tmp_path, monkeypatch, origin=FORK)
    assert commands.status(jenkins, argparse.Namespace(folder=None)) == 0
    server.folders.clear()
    server.seed_folder(FOLDER, marker(anchor="refs/tags/topic"))
    with pytest.raises(Fail, match="run `jenkins-preview up` first"):
        commands.status(jenkins, argparse.Namespace(folder=None))


def test_doctor_with_an_empty_set_refuses(jenkins) -> None:
    """`--set ""` is an argument, not an omission, and reports as an unknown set."""
    rc = commands.doctor(jenkins, argparse.Namespace(set="", repo=FORK, ref="topic"))
    assert rc == 1


def test_doctor_names_an_invalid_url_cause(tmp_path, monkeypatch, offline_git, capsys) -> None:
    """Fields present but the URL malformed: the constructor's refusal must
    surface as a FAIL row, never degrade to a bare SKIP."""
    monkeypatch.setenv("JENKINS_URL", "ps3.cd.percona.com")
    monkeypatch.setenv("JENKINS_USER", "alice")
    monkeypatch.setenv("JENKINS_TOKEN", "token")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    rc = commands.doctor(None, up_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL  JENKINS_URL does not look like an http(s) URL" in out
    assert "SKIP  /me authentication check" in out


def test_doctor_reports_a_malformed_credentials_file(
    tmp_path, monkeypatch, offline_git, capsys
) -> None:
    """A broken credentials file is a finding, not a refusal that hides the
    rest of the diagnosis."""
    for env in ("JENKINS_URL", "JENKINS_USER", "JENKINS_TOKEN", "JENKINS_SERVER"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    path = tmp_path / "xdg" / "jenkins-preview" / "credentials.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("servers: [broken")
    path.chmod(0o600)
    rc = commands.doctor(None, up_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out and "not valid YAML" in out
    assert "NOT READY" in out


def test_doctor_via_main_reports_credential_gaps(tmp_path, monkeypatch, capsys) -> None:
    """The cli seam: main() must hand doctor a None client on a credential
    failure instead of dying with a top-level ERROR."""
    from conftest import PXB_SETS_FILE

    for env in ("JENKINS_URL", "JENKINS_USER", "JENKINS_TOKEN", "JENKINS_SERVER"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["doctor", "--set", "pxb-8.1", "--sets", PXB_SETS_FILE])
    captured = capsys.readouterr()
    assert rc == 1
    assert "FAIL  JENKINS_URL is not set" in captured.out
    assert "ERROR:" not in captured.err


def test_up_update_refuses_running_builds_before_the_swap(
    jenkins, server, offline_git, capsys
) -> None:
    """The refusal comes before the clone, the render and the sibling
    publish, with up's own recovery text."""
    server.seed_folder(FOLDER, marker(), {"pxb-job": server.job(building=True)})
    with pytest.raises(Fail, match="builds still running") as caught:
        commands.up(jenkins, up_args(update=True))
    assert "--force" in str(caught.value)
    assert not any("-sw" in name for name in server.folders), "no swap sibling may exist"


def test_up_notes_unpushed_commits(jenkins, server, offline_git, monkeypatch, capsys) -> None:
    """The zero-flag path warns when the checkout is ahead of the remote tip,
    the only guard against a green preview of the wrong commit."""
    monkeypatch.setattr(commands, "local_context", lambda: (FORK, "topic", "b" * 40))
    rc = commands.up(jenkins, up_args(repo=None, ref=None, dry_run=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "Unpushed commits are not in this preview" in out


def test_doctor_reports_missing_credentials_and_continues(
    tmp_path, monkeypatch, offline_git, capsys
) -> None:
    """Doctor never prompts: credential gaps are FAIL findings, the checks
    needing them are SKIPPED, and everything local still runs."""
    for env in ("JENKINS_URL", "JENKINS_USER", "JENKINS_TOKEN", "JENKINS_SERVER"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    rc = commands.doctor(None, up_args())
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL  JENKINS_URL is not set" in out
    assert "FAIL  JENKINS_TOKEN is not set" in out
    assert "SKIP  /me authentication check requires JENKINS_URL, JENKINS_USER, JENKINS_TOKEN" in out
    assert "all 6 requested jobs rendered" in out
    assert "NOT READY" in out
    assert "Jenkins API token:" not in out


def test_doctor_names_the_credential_sources(jenkins, monkeypatch, offline_git, capsys) -> None:
    monkeypatch.setenv("JENKINS_URL", BASE)
    monkeypatch.setenv("JENKINS_USER", "alice")
    monkeypatch.setenv("JENKINS_TOKEN", "token")
    commands.doctor(jenkins, up_args())
    out = capsys.readouterr().out
    assert "creds: url from env, user from env, token from env" in out
    assert "mix the environment" not in out


def test_doctor_notes_mixed_credential_sources(
    jenkins, tmp_path, monkeypatch, offline_git, capsys
) -> None:
    """A stale exported JENKINS_URL silently beats the selected server's url
    while the other fields keep coming from the file, so the token can travel
    to a host the file never named. Doctor points that out."""
    for env in ("JENKINS_USER", "JENKINS_TOKEN", "JENKINS_SERVER"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("JENKINS_URL", BASE)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    path = tmp_path / "xdg" / "jenkins-preview" / "credentials.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("servers:\n  ps3: {url: https://other.example.com, user: alice, token: t}\n")
    path.chmod(0o600)
    commands.doctor(jenkins, up_args())
    out = capsys.readouterr().out
    assert "creds: url from env, user from credentials.yaml (ps3)" in out
    assert "mix the environment and credentials.yaml" in out


def test_doctor_reports_auth_before_a_missing_sets_file(jenkins, capsys) -> None:
    """A new user must learn their credentials work before anything about
    sets: doctor authenticates first and reports the missing file as a
    finding, never a refusal that eats the just-typed token."""
    from jenkins_preview.sets import initialize

    initialize(None)
    try:
        rc = commands.doctor(jenkins, argparse.Namespace(set=None, repo=FORK, ref="topic"))
    finally:
        from conftest import PXB_SETS_FILE

        initialize(PXB_SETS_FILE)
    out = capsys.readouterr().out
    assert rc == 1
    assert "PASS  authenticated as alice" in out
    assert "no sets file was found" in out
    assert "NOT READY" in out


def test_a_missing_token_dies_naming_both_sources(tmp_path, monkeypatch, capsys) -> None:
    """No prompting anywhere: the token comes from the environment or the
    credentials file, and absence names both fixes."""
    monkeypatch.setenv("JENKINS_URL", BASE)
    monkeypatch.setenv("JENKINS_USER", "alice")
    monkeypatch.delenv("JENKINS_TOKEN", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    try:
        rc = cli.main(["list"])
    finally:
        # cli.main loads the registry module-globally, reload the suite sets
        from conftest import PXB_SETS_FILE
        from jenkins_preview.sets import initialize

        initialize(PXB_SETS_FILE)
    err = capsys.readouterr().err
    assert rc == 1
    assert "JENKINS_TOKEN is not set" in err
    assert "credentials.yaml" in err


def test_down_with_an_empty_folder_refuses(jenkins, server) -> None:
    """`down "$FOLDER"` with an unset variable must stay a loud refusal, never
    an inferred deletion: empty is an argument, only absent means inferred."""
    server.seed_folder(FOLDER, marker())
    with pytest.raises(Fail, match="invalid preview folder name"):
        commands.down(jenkins, argparse.Namespace(folder="", force=False, yes=False))
    assert FOLDER in server.folders


def test_down_inferred_asks_first(jenkins, server, tmp_path, monkeypatch, capsys) -> None:
    """A deletion of a name the user never typed gets one confirmation."""
    server.seed_folder(FOLDER, marker(), {"pxb-job": server.job()})
    _checkout_on_branch(tmp_path, monkeypatch, origin=FORK)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    rc = commands.down(jenkins, argparse.Namespace(folder=None, force=False, yes=False))
    assert rc == 0
    assert FOLDER in server.folders, "n must keep the folder"
    assert "aborted, nothing deleted" in capsys.readouterr().out
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    rc = commands.down(jenkins, argparse.Namespace(folder=None, force=False, yes=False))
    assert rc == 0
    assert FOLDER not in server.folders


# --------------------------------------------------------------------------- #
# Publish: order, verification, rollback (gate G8)
# --------------------------------------------------------------------------- #


def test_up_publishes_creates_verifies_then_enables(jenkins, server, offline_git) -> None:
    assert commands.up(jenkins, up_args()) == 0

    folder = server.folders[FOLDER]
    job_set = pick_set("pxb-8.1")
    assert set(folder["jobs"]) == set(job_set.jobs)
    assert all(job["buildable"] for job in folder["jobs"].values())

    ordered = [(m, p) for m, p in server.calls if m == "POST" or "config.xml" in p]
    folder_create = next(i for i, (_, p) in enumerate(ordered) if "previews/createItem" in p)
    job_create = next(i for i, (_, p) in enumerate(ordered) if f"{PATH}/createItem" in p)
    readback = next(i for i, (_, p) in enumerate(ordered) if "config.xml" in p)
    enable = next(i for i, (_, p) in enumerate(ordered) if p.endswith("/enable"))
    assert folder_create < job_create < readback < enable


def test_up_stores_sanitized_configs(jenkins, server, offline_git) -> None:
    commands.up(jenkins, up_args())
    stored = next(iter(server.folders[FOLDER]["jobs"].values()))["xml"]
    root = ET.fromstring(stored)
    assert root.findtext(".//lightweight") == "false"
    assert root.findtext(".//url") == FORK
    assert root.findtext(".//hudson.plugins.git.BranchSpec/name") == SHA


def test_up_rolls_back_when_a_job_stays_disabled(jenkins, server, offline_git) -> None:
    server.fail_enable_readback = True
    with pytest.raises(Fail, match="still disabled"):
        commands.up(jenkins, up_args())
    assert FOLDER not in server.folders, "the half-built folder must not survive"
    deletes = [p for m, p in server.calls if m == "POST" and p.endswith("doDelete")]
    assert deletes == [f"{PATH}/doDelete"], "rollback must delete only its own folder"


def test_up_rollback_removes_the_view_before_the_folder(jenkins, server, offline_git) -> None:
    server.view_lists_stray = True
    with pytest.raises(Fail, match="expected exactly"):
        commands.up(jenkins, up_args(root_view=True))
    assert FOLDER not in server.folders
    assert FOLDER not in server.views
    deletes = [p for m, p in server.calls if m == "POST" and p.endswith("doDelete")]
    assert deletes.index(f"/view/{FOLDER}/doDelete") < deletes.index(f"{PATH}/doDelete"), (
        "a global tab must never outlive its folder mid-rollback"
    )


def test_up_refuses_an_existing_folder_without_update(jenkins, server, offline_git) -> None:
    server.seed_folder(FOLDER, marker())
    with pytest.raises(Fail, match="already exists"):
        commands.up(jenkins, up_args())
    assert FOLDER in server.folders


def test_up_update_refuses_a_folder_it_does_not_own(jenkins, server, offline_git) -> None:
    server.seed_folder(FOLDER, "someone's hand-made folder")
    with pytest.raises(Fail, match="no ownership marker"):
        commands.up(jenkins, up_args(update=True))
    assert FOLDER in server.folders


# --------------------------------------------------------------------------- #
# Run: stage ordering, producer gate, triggering
# --------------------------------------------------------------------------- #


def run_args(**overrides) -> argparse.Namespace:
    defaults = {"folder": FOLDER, "stage": None, "yes": True, "param": []}
    return argparse.Namespace(**{**defaults, **overrides})


def seed_preview(server: FakeJenkinsServer, **job_state) -> None:
    """Every pxb job is parameterized in the real YAML, so the fake mirrors that
    unless a test overrides it."""
    job_set = pick_set("pxb-8.1")
    jobs = {
        name: server.job(**{"parameters": True, **job_state.get(name, {})}) for name in job_set.jobs
    }
    server.seed_folder(FOLDER, marker(), jobs)


def test_run_refuses_a_consumer_stage_before_its_producer(jenkins, server) -> None:
    seed_preview(server)
    with pytest.raises(Fail, match="no successful build"):
        commands.run(jenkins, run_args(stage="test"))
    assert not [p for m, p in server.calls if p.endswith("buildWithParameters")]


def test_run_triggers_exactly_the_stage_jobs(jenkins, server) -> None:
    seed_preview(server)
    job_set = pick_set("pxb-8.1")
    assert commands.run(jenkins, run_args(stage="compile")) == 0
    triggered = [p for m, p in server.calls if p.endswith("buildWithParameters")]
    assert len(triggered) == len(job_set.stage("compile"))
    for job in job_set.stage("compile"):
        assert any(f"/job/{job}/" in p for p in triggered)


def test_run_triggers_a_parameterless_job_via_build(jenkins, server) -> None:
    """POST /buildWithParameters answers 400 on a job with no parameters, so the
    endpoint has to follow the job. Found live on the first parameterless set."""
    producer = pick_set("pxb-8.1").stage("compile")[0]
    seed_preview(server, **{producer: {"parameters": False}})
    assert commands.run(jenkins, run_args(stage="compile")) == 0
    assert [p for m, p in server.calls if m == "POST" and p.endswith("/build")]
    assert not [p for m, p in server.calls if p.endswith("buildWithParameters")]


def test_run_treats_an_empty_parameters_property_as_parameterized(jenkins, server) -> None:
    """Jenkins counts a job as parameterized when ParametersDefinitionProperty
    exists, even with zero definitions, so presence decides the endpoint."""
    producer = pick_set("pxb-8.1").stage("compile")[0]
    seed_preview(server, **{producer: {"parameters": "empty"}})
    assert commands.run(jenkins, run_args(stage="compile")) == 0
    assert [p for m, p in server.calls if p.endswith("buildWithParameters")]
    assert not [p for m, p in server.calls if m == "POST" and p.endswith("/build")]


def test_interrupt_lands_on_its_own_line(monkeypatch, capsys) -> None:
    """A Ctrl-C after real output gets the separating blank line, one before
    any output does not."""
    from conftest import PXB_SETS_FILE
    from jenkins_preview.errors import say

    monkeypatch.setenv("JENKINS_URL", BASE)
    monkeypatch.setenv("JENKINS_USER", "alice")
    monkeypatch.setenv("JENKINS_TOKEN", "token")

    def interrupted_after_output(jenkins, args):
        say("partial progress")
        raise KeyboardInterrupt

    monkeypatch.setitem(cli.COMMANDS, "list", interrupted_after_output)
    rc = cli.main(["list", "--sets", PXB_SETS_FILE])
    captured = capsys.readouterr()
    assert rc == 130
    assert captured.err.startswith("\ninterrupted")

    monkeypatch.setitem(
        cli.COMMANDS, "list", lambda jenkins, args: (_ for _ in ()).throw(KeyboardInterrupt)
    )
    rc = cli.main(["list", "--sets", PXB_SETS_FILE])
    captured = capsys.readouterr()
    assert rc == 130
    assert captured.err.startswith("interrupted")


def test_a_trigger_400_names_the_endpoint_race(jenkins, server, monkeypatch, capsys) -> None:
    """A 400 from the trigger POST is the check-to-trigger window, and the
    refusal says so instead of the generic retry advice."""
    from conftest import PXB_SETS_FILE

    seed_preview(server)
    real_open = server.open

    def race(request, timeout=None):
        if request.full_url.endswith("buildWithParameters"):
            raise urllib.error.HTTPError(request.full_url, 400, "boom", {}, io.BytesIO(b""))
        return real_open(request, timeout)

    monkeypatch.setattr(server, "open", race)
    monkeypatch.setattr(cli, "credentials", lambda: jenkins)
    rc = cli.main(["run", FOLDER, "--yes", "--sets", PXB_SETS_FILE])
    captured = capsys.readouterr()
    assert rc == 1
    assert "changed between the check and the trigger" in captured.err


def test_teardown_commands_survive_a_broken_sets_file(
    jenkins, server, monkeypatch, capsys, tmp_path
) -> None:
    """A sets file that fails validation must never block status, down, list or
    reap, which read only the ownership markers."""
    from conftest import PXB_SETS_FILE
    from jenkins_preview.sets import initialize

    seed_preview(server)
    broken = tmp_path / "broken.json"
    broken.write_text('{"sets": {"x": {"nonsense": true}}}')
    monkeypatch.setattr(cli, "credentials", lambda: jenkins)
    try:
        assert cli.main(["list", "--sets", str(broken)]) == 0
        assert cli.main(["status", FOLDER, "--sets", str(broken)]) == 0
        assert cli.main(["reap", "--dry-run", "--sets", str(broken)]) == 0
        assert FOLDER in server.folders
        assert cli.main(["down", FOLDER, "--sets", str(broken)]) == 0
        assert FOLDER not in server.folders
        capsys.readouterr()
    finally:
        # cli.main loads the registry module-globally, reload the suite sets
        initialize(PXB_SETS_FILE)


def test_run_keeps_buildwithparameters_for_parameterized_jobs(jenkins, server) -> None:
    """The fake answers each endpoint exactly as Jenkins does (400 for the wrong
    kind), so passing here means the choice was made per job, not guessed."""
    seed_preview(server)
    assert commands.run(jenkins, run_args(stage="compile")) == 0
    assert [p for m, p in server.calls if p.endswith("buildWithParameters")]
    assert not [p for m, p in server.calls if m == "POST" and p.endswith("/build")]


def test_run_autopicks_the_next_stage_after_green(jenkins, server) -> None:
    producer = pick_set("pxb-8.1").stage("compile")[0]
    seed_preview(server, **{producer: {"last_success": 4}})
    assert commands.run(jenkins, run_args()) == 0
    triggered = [p for m, p in server.calls if p.endswith("buildWithParameters")]
    consumer = pick_set("pxb-8.1").stage("test")[0]
    assert triggered and all(f"/job/{consumer}/" in p for p in triggered)


def test_run_with_all_stages_green_triggers_nothing(jenkins, server) -> None:
    green = {job: {"last_success": 2} for job in pick_set("pxb-8.1").jobs}
    seed_preview(server, **green)
    assert commands.run(jenkins, run_args()) == 0
    assert not [p for m, p in server.calls if p.endswith("buildWithParameters")]


# --------------------------------------------------------------------------- #
# Teardown and reap: strict ownership, view-before-folder
# --------------------------------------------------------------------------- #


def down_args(**overrides) -> argparse.Namespace:
    defaults = {"folder": FOLDER, "force": False}
    return argparse.Namespace(**{**defaults, **overrides})


def test_down_refuses_while_builds_run_then_force_deletes(jenkins, server) -> None:
    producer = pick_set("pxb-8.1").stage("compile")[0]
    seed_preview(server, **{producer: {"building": True}})
    with pytest.raises(Fail, match="still running"):
        commands.down(jenkins, down_args())
    assert FOLDER in server.folders
    assert commands.down(jenkins, down_args(force=True)) == 0
    assert FOLDER not in server.folders


def test_run_names_the_local_sets_gap_without_destructive_advice(jenkins, server) -> None:
    """The preview is healthy. Only this invocation's sets file lacks the set.
    The old message blamed the marker and advised tearing the preview down."""
    server.seed_folder(FOLDER, marker(job_set="a-set-not-loaded-here"))
    with pytest.raises(Fail, match="not in your sets file"):
        commands.run(jenkins, run_args())
    assert FOLDER in server.folders


def test_down_deletes_an_orphaned_view_it_can_prove_is_its_own(jenkins, server) -> None:
    """A hand-deleted folder can leave its tab behind. The marker-proven view
    is the one thing down may still remove."""
    server.views[FOLDER] = {
        "description": view_marker(preview_id="cafe" * 8, folder=FOLDER, user="alice")
    }
    assert commands.down(jenkins, down_args()) == 0
    assert FOLDER not in server.views


def test_down_leaves_an_unprovable_orphan_view_alone(jenkins, server) -> None:
    server.views[FOLDER] = {"description": "hand made view, not ours"}
    with pytest.raises(Fail, match="not found"):
        commands.down(jenkins, down_args())
    assert FOLDER in server.views


def test_publish_names_a_concurrent_creation(jenkins, server, offline_git) -> None:
    server.race_on_create = True
    with pytest.raises(Fail, match="created concurrently"):
        commands.up(jenkins, up_args())


def test_down_refuses_a_folder_without_a_marker(jenkins, server) -> None:
    server.seed_folder(FOLDER, "a folder this tool did not create")
    with pytest.raises(Fail, match="no ownership marker"):
        commands.down(jenkins, down_args())
    assert FOLDER in server.folders


def test_down_fails_closed_on_an_unowned_view(jenkins, server) -> None:
    server.seed_folder(FOLDER, marker(view=FOLDER))
    server.views[FOLDER] = {"description": "hand-made view, not ours"}
    with pytest.raises(Fail, match="no ownership marker"):
        commands.down(jenkins, down_args())
    assert FOLDER in server.folders, "the folder must survive when its view is unprovable"
    assert FOLDER in server.views


def test_down_refuses_a_view_from_a_different_preview(jenkins, server) -> None:
    server.seed_folder(FOLDER, marker(view=FOLDER))
    server.views[FOLDER] = {
        "description": view_marker(preview_id="beef" * 8, folder=FOLDER, user="alice")
    }
    with pytest.raises(Fail, match="different preview"):
        commands.down(jenkins, down_args())
    assert FOLDER in server.folders


def test_down_deletes_the_view_then_the_folder(jenkins, server) -> None:
    server.seed_folder(FOLDER, marker(view=FOLDER))
    server.views[FOLDER] = {
        "description": view_marker(preview_id="cafe" * 8, folder=FOLDER, user="alice")
    }
    assert commands.down(jenkins, down_args()) == 0
    assert FOLDER not in server.folders
    assert FOLDER not in server.views
    deletes = [p for m, p in server.calls if m == "POST" and p.endswith("doDelete")]
    assert deletes.index(f"/view/{FOLDER}/doDelete") < deletes.index(f"{PATH}/doDelete")


def reap_args(**overrides) -> argparse.Namespace:
    defaults = {"older_than": 7, "dry_run": False, "force": False}
    return argparse.Namespace(**{**defaults, **overrides})


def test_reap_deletes_old_skips_young_and_foreign(jenkins, server) -> None:
    server.seed_folder("preview-alice-old", aged(marker(), "2026-07-01T00:00:00+00:00"))
    server.seed_folder("preview-alice-new", marker())
    server.seed_folder("hand-made", "no marker at all")
    server.seed_folder("preview-alice-naive", aged(marker(), "2026-07-01T00:00:00"))
    assert commands.reap(jenkins, reap_args()) == 0
    assert set(server.folders) == {"preview-alice-new", "hand-made", "preview-alice-naive"}


def test_reap_dry_run_deletes_nothing(jenkins, server) -> None:
    server.seed_folder("preview-alice-old", aged(marker(), "2026-07-01T00:00:00+00:00"))
    assert commands.reap(jenkins, reap_args(dry_run=True)) == 0
    assert "preview-alice-old" in server.folders
    assert not [p for m, p in server.calls if p.endswith("doDelete")]


# --------------------------------------------------------------------------- #
# Promoted defect register: all three guards now implemented (strict, so a fix
# flips the test to a failure that demands promotion to a plain assertion)
# --------------------------------------------------------------------------- #


def test_update_keeps_the_old_preview_when_the_new_ref_fails_to_render(
    jenkins, server, offline_git, monkeypatch
) -> None:
    """Promoted from the defect register: teardown happens only after every
    gate has passed on the replacement."""
    seed_preview(server)

    def broken_render(workdir, job_set, outdir):
        raise Fail("JJB failed to render", "fix the YAML")

    monkeypatch.setattr(commands, "render", broken_render)
    with pytest.raises(Fail, match="render"):
        commands.up(jenkins, up_args(update=True))
    assert FOLDER in server.folders, "a failed update must leave the old preview standing"


def test_update_replaces_its_own_root_tab(jenkins, server, offline_git) -> None:
    """The default loop: publish with the tab, push, update. The update must
    recognise its OWN paired view instead of refusing over it."""
    assert commands.up(jenkins, up_args(root_view=True)) == 0
    assert FOLDER in server.views
    assert commands.up(jenkins, up_args(root_view=True, update=True)) == 0
    assert FOLDER in server.views


def test_update_never_adopts_an_unpaired_view(jenkins, server, offline_git) -> None:
    server.seed_folder(FOLDER, marker())
    server.views[FOLDER] = {"description": "somebody's hand-made tab"}
    with pytest.raises(Fail, match="already exists"):
        commands.up(jenkins, up_args(root_view=True, update=True))


def test_update_refuses_a_preview_owned_by_someone_else(jenkins, server, offline_git) -> None:
    server.seed_folder(FOLDER, marker(user="somebody-else"))
    with pytest.raises(Fail, match="belongs to somebody-else"):
        commands.up(jenkins, up_args(update=True))
    assert FOLDER in server.folders


def test_update_refuses_a_folder_from_another_set(jenkins, server, offline_git) -> None:
    server.seed_folder(FOLDER, marker(job_set="pxb-9.x"))
    with pytest.raises(Fail, match="published from set"):
        commands.up(jenkins, up_args(update=True))
    assert FOLDER in server.folders


def test_run_refuses_a_stage_that_is_already_building(jenkins, server) -> None:
    producer = pick_set("pxb-8.1").stage("compile")[0]
    seed_preview(server, **{producer: {"building": True}})
    with pytest.raises(Fail, match="building"):
        commands.run(jenkins, run_args(stage="compile"))


def test_reap_refuses_a_negative_age(jenkins, server) -> None:
    server.seed_folder("preview-alice-new", marker())
    with pytest.raises(Fail, match="older-than"):
        commands.reap(jenkins, reap_args(older_than=-1))
    assert "preview-alice-new" in server.folders


def test_up_fits_a_long_user_and_branch_into_the_name_cap(server, offline_git) -> None:
    """Promoted from the defect register: the derived name is fitted, never refused."""
    client = Jenkins(BASE, "satya-bodapati", "token")
    client._opener = server
    args = up_args(ref="PXB-3613-add-arm-platforms-to-jenkins")
    assert commands.up(client, args) == 0
    (folder,) = server.folders
    assert len(folder) <= 64
    assert folder.startswith("preview-satya-bodapati-pxb-8.1-")


def _foreign_checkout(monkeypatch) -> None:
    """A fake checkout whose pipeline script fetches the canonical repo."""

    def checkout_writing_script(repo, sha, anchor, dest) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "pipe.groovy").write_text(
            "git branch: 'master', url: 'https://github.com/Percona-Lab/jenkins-pipelines'\n"
        )

    monkeypatch.setattr(commands, "checkout_at", checkout_writing_script)


def test_up_refuses_a_canonical_fetch_without_acknowledgment(
    jenkins, server, offline_git, monkeypatch, capsys
) -> None:
    _foreign_checkout(monkeypatch)
    with pytest.raises(Fail, match="outside your fork"):
        commands.up(jenkins, up_args())
    assert FOLDER not in server.folders
    out = capsys.readouterr().out
    assert "pipe.groovy:1 (canonical) https://github.com/Percona-Lab/jenkins-pipelines" in out


def test_up_publishes_a_canonical_fetch_when_acknowledged(
    jenkins, server, offline_git, monkeypatch, capsys
) -> None:
    """--allow-foreign-fetch publishes, keeps the disclosure in the output, and
    stamps the count into the marker so status can echo it later."""
    _foreign_checkout(monkeypatch)
    assert commands.up(jenkins, up_args(allow_foreign_fetch=True)) == 0
    out = capsys.readouterr().out
    assert "fetch leaves your fork" in out
    assert "foreign" in out
    from jenkins_preview.folders import parse_folder_marker

    stamped = parse_folder_marker(server.folders[FOLDER]["description"])
    assert stamped.get("foreign") == "1"


def test_up_refuses_a_missing_pipeline_script(jenkins, server, offline_git, monkeypatch) -> None:
    """A script absent at the pinned commit dies at build load, so publishing is
    refused outright. No acknowledgment applies."""
    monkeypatch.setattr(
        commands, "checkout_at", lambda repo, sha, anchor, dest: dest.mkdir(parents=True)
    )
    with pytest.raises(Fail, match="do not exist at the pinned commit"):
        commands.up(jenkins, up_args(allow_foreign_fetch=True))
    assert FOLDER not in server.folders


def test_down_refuses_a_preview_owned_by_someone_else(jenkins, server) -> None:
    server.seed_folder(FOLDER, marker(user="somebody-else"))
    with pytest.raises(Fail, match="owner"):
        commands.down(jenkins, down_args())
    assert FOLDER in server.folders


# --------------------------------------------------------------------------- #
# Doctor, status, list, dry-run: the read-only surfaces
# --------------------------------------------------------------------------- #


def test_doctor_reports_ready_on_a_healthy_setup(jenkins, server, offline_git, capsys) -> None:
    assert commands.doctor(jenkins, up_args()) == 0
    out = capsys.readouterr().out
    assert "READY" in out and "NOT READY" not in out
    assert "every build-time fetch stays on the fork" in out


def test_doctor_fails_without_the_previews_root(jenkins, server, offline_git, capsys) -> None:
    server.no_previews_root = True
    assert commands.doctor(jenkins, up_args()) == 1
    out = capsys.readouterr().out
    assert "folder does not exist" in out
    assert "NOT READY" in out


def test_doctor_fails_on_a_missing_pipeline_script(
    jenkins, server, offline_git, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        commands, "checkout_at", lambda repo, sha, anchor, dest: dest.mkdir(parents=True)
    )
    assert commands.doctor(jenkins, up_args()) == 1
    out = capsys.readouterr().out
    assert "do not exist at this commit" in out


def test_status_reports_the_pin_and_a_matching_tip(jenkins, server, monkeypatch, capsys) -> None:
    seed_preview(server)
    monkeypatch.setattr(commands, "resolve_ref", lambda repo, ref: (SHA, "refs/heads/topic"))
    assert commands.status(jenkins, argparse.Namespace(folder=FOLDER)) == 0
    out = capsys.readouterr().out
    assert "branch tip matches the pin" in out
    assert SHA in out


def test_status_notes_a_moved_branch_with_the_sync_hint(
    jenkins, server, monkeypatch, capsys
) -> None:
    seed_preview(server)
    monkeypatch.setattr(commands, "resolve_ref", lambda repo, ref: ("b" * 40, "refs/heads/topic"))
    assert commands.status(jenkins, argparse.Namespace(folder=FOLDER)) == 0
    out = capsys.readouterr().out
    assert "the branch has moved" in out
    assert f"jenkins-preview sync {FOLDER}" in out


def test_status_echoes_the_foreign_fetch_count(jenkins, server, monkeypatch, capsys) -> None:
    server.seed_folder(FOLDER, marker(foreign=3), {})
    monkeypatch.setattr(commands, "resolve_ref", lambda repo, ref: (SHA, "refs/heads/topic"))
    commands.status(jenkins, argparse.Namespace(folder=FOLDER))
    assert "3 build-time fetches leave the fork" in capsys.readouterr().out


def test_list_shows_owner_set_and_tab_per_preview(jenkins, server, capsys) -> None:
    server.seed_folder(FOLDER, marker())
    server.seed_folder("hand-made", "no marker")
    assert commands.preview_list(jenkins, argparse.Namespace()) == 0
    out = capsys.readouterr().out
    assert FOLDER in out and "alice" in out
    assert "hand-made" in out and "?" in out, "unmarked folders stay visible, attributed to '?'"


def test_list_with_no_previews_says_so(jenkins, server, capsys) -> None:
    assert commands.preview_list(jenkins, argparse.Namespace()) == 0
    assert "no previews" in capsys.readouterr().out


def test_up_dry_run_creates_nothing(jenkins, server, offline_git, capsys) -> None:
    assert commands.up(jenkins, up_args(dry_run=True)) == 0
    assert server.folders == {}
    assert "DRY RUN, nothing was created" in capsys.readouterr().out


def test_run_declined_at_the_prompt_triggers_nothing(jenkins, server, monkeypatch, capsys) -> None:
    seed_preview(server)

    def no_terminal(prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", no_terminal)
    assert commands.run(jenkins, run_args(yes=False, stage="compile")) == 0
    assert "aborted, nothing triggered" in capsys.readouterr().out
    assert not [p for m, p in server.calls if p.endswith("buildWithParameters")]


# --------------------------------------------------------------------------- #
# Error output: the ERROR line never leads with a blank line
# --------------------------------------------------------------------------- #


def test_error_starts_flush_when_nothing_was_printed(jenkins, server, monkeypatch, capsys) -> None:
    """A refusal with no prior output begins at line one, column one."""
    from conftest import PXB_SETS_FILE

    seed_preview(server)
    monkeypatch.setattr(cli, "credentials", lambda: jenkins)
    rc = cli.main(["run", FOLDER, "--stage", "test", "--sets", PXB_SETS_FILE])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert captured.err.startswith("ERROR: ")


def test_error_keeps_one_blank_line_after_real_output(jenkins, server, monkeypatch, capsys) -> None:
    """After progress lines, the ERROR line stays separated by one blank line."""
    from conftest import PXB_SETS_FILE

    seed_preview(server)
    real_open = server.open

    def failing_open(request, timeout=None):
        if request.full_url.endswith("buildWithParameters"):
            raise urllib.error.HTTPError(request.full_url, 500, "boom", {}, io.BytesIO(b""))
        return real_open(request, timeout)

    monkeypatch.setattr(server, "open", failing_open)
    monkeypatch.setattr(cli, "credentials", lambda: jenkins)
    rc = cli.main(["run", FOLDER, "--yes", "--sets", PXB_SETS_FILE])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out.startswith("stage  compile")
    assert captured.err.startswith("\nERROR: ")


# --------------------------------------------------------------------------- #
# Run parameters: strict validation, form-body wiring, honest echo
# --------------------------------------------------------------------------- #

PRODUCER = pick_set("pxb-8.1").stage("compile")[0]
CONSUMER = pick_set("pxb-8.1").stage("test")[0]


def _trigger_posts(server: FakeJenkinsServer) -> list[tuple[str, str, bytes, str | None]]:
    return [
        r
        for r in server.requests
        if r[0] == "POST" and (r[1].endswith("buildWithParameters") or r[1].endswith("/build"))
    ]


def test_run_passes_params_in_the_buildwithparameters_body(jenkins, server) -> None:
    seed_preview(
        server, **{PRODUCER: {"parameters": [{"name": "CLOUD"}, {"name": "N"}, {"name": "Z"}]}}
    )
    assert commands.run(jenkins, run_args(stage="compile", param=["CLOUD=AWS", "N=2"])) == 0
    posts = _trigger_posts(server)
    assert len(posts) == 1
    _, url, body, content_type = posts[0]
    assert url.endswith("buildWithParameters")
    assert body == b"CLOUD=AWS&N=2"
    assert content_type == "application/x-www-form-urlencoded; charset=UTF-8"


def test_run_param_encoding_matches_urlencode(jenkins, server) -> None:
    """A naive '&'.join(f'{k}={v}') passes the clean-ASCII test and corrupts
    these. The wire bytes must be exactly urlencode's. No control characters
    here: those refuse at parse time before any encoding."""
    hostile = "a&b %+café"
    seed_preview(server, **{PRODUCER: {"parameters": [{"name": "MSG"}]}})
    assert commands.run(jenkins, run_args(stage="compile", param=[f"MSG={hostile}"])) == 0
    _, _, body, _ = _trigger_posts(server)[0]
    assert body == urllib.parse.urlencode({"MSG": hostile}).encode()


def test_run_refuses_a_control_character_value(jenkins, server) -> None:
    """shlex.quote passes ESC through verbatim, so a control byte in a value
    would forge terminal output wherever the value is echoed or hinted."""
    seed_preview(server)
    with pytest.raises(Fail, match="value carries control characters"):
        commands.run(jenkins, run_args(stage="compile", param=["X=a\x1bb"]))
    assert not [p for m, p in server.calls if m == "POST"]


def test_run_dies_when_a_stage_job_is_missing_under_params(jenkins, server) -> None:
    """Set drift: the sets file names a job the folder never got. Without the
    absence check the refusal would misread the 404 as 'declares no
    parameters' and advise a first build that can only 404."""
    seed_preview(server)
    del server.folders[FOLDER]["jobs"][PRODUCER]
    with pytest.raises(Fail, match="is not in the preview folder"):
        commands.run(jenkins, run_args(stage="compile", param=["X=1"]))
    assert not [p for m, p in server.calls if p.endswith("buildWithParameters")]


def test_run_hint_survives_a_missing_next_stage_job(jenkins, server, capsys) -> None:
    """A next-stage job absent from the folder only costs the hint its carried
    parameters. The run itself already succeeded and must say so."""
    seed_preview(server)
    del server.folders[FOLDER]["jobs"][CONSUMER]
    assert commands.run(jenkins, run_args(stage="compile", param=["X=1"])) == 0
    then = [line for line in capsys.readouterr().out.splitlines() if line.startswith("then")]
    assert then and "--stage test" in then[0] and " -p " not in then[0]


def test_run_param_value_keeps_equals_and_empty(jenkins, server) -> None:
    seed_preview(server, **{PRODUCER: {"parameters": [{"name": "A"}, {"name": "B"}]}})
    assert commands.run(jenkins, run_args(stage="compile", param=["A=b=c", "B="])) == 0
    _, _, body, _ = _trigger_posts(server)[0]
    assert body == b"A=b%3Dc&B="


def test_run_refuses_params_without_a_stage(jenkins, server) -> None:
    with pytest.raises(Fail, match="explicit --stage"):
        commands.run(jenkins, run_args(param=["CLOUD=AWS"]))
    assert not server.calls


def test_run_refuses_a_malformed_param(jenkins, server) -> None:
    for bad in ("novalue", "=x", "a b=c"):
        with pytest.raises(Fail):
            commands.run(jenkins, run_args(stage="compile", param=[bad]))
    assert not server.calls


def test_run_refuses_a_duplicate_param(jenkins, server) -> None:
    with pytest.raises(Fail, match="given twice"):
        commands.run(jenkins, run_args(stage="compile", param=["A=1", "A=2"]))
    assert not server.calls


def test_run_refuses_a_reserved_param_name(jenkins, server) -> None:
    with pytest.raises(Fail, match="collides"):
        commands.run(jenkins, run_args(stage="compile", param=["delay=5"]))
    assert not server.calls


def test_run_refuses_a_param_for_a_parameterless_stage_job(jenkins, server) -> None:
    seed_preview(server, **{PRODUCER: {"last_success": 4}, CONSUMER: {"parameters": False}})
    with pytest.raises(Fail, match="declares no parameters"):
        commands.run(jenkins, run_args(stage="test", param=["X=1"]))
    assert not _trigger_posts(server)


def test_run_refuses_an_undeclared_param_key(jenkins, server) -> None:
    seed_preview(server)
    with pytest.raises(Fail, match=r"does not declare parameter 'TYPO' \(declared: X\)"):
        commands.run(jenkins, run_args(stage="compile", param=["TYPO=1"]))
    assert not _trigger_posts(server)


def test_run_refuses_a_bad_boolean_value(jenkins, server) -> None:
    seed_preview(
        server,
        **{PRODUCER: {"parameters": [{"name": "FLAG", "type": "BooleanParameterDefinition"}]}},
    )
    with pytest.raises(Fail, match="is boolean"):
        commands.run(jenkins, run_args(stage="compile", param=["FLAG=yes"]))
    assert not _trigger_posts(server)
    assert commands.run(jenkins, run_args(stage="compile", param=["FLAG=TRUE"])) == 0
    assert _trigger_posts(server)[0][2] == b"FLAG=TRUE"


def test_run_refuses_a_choice_value_outside_the_list(jenkins, server) -> None:
    definition = {
        "name": "PICK",
        "type": "ChoiceParameterDefinition",
        "choices": ["alpha", "beta"],
    }
    seed_preview(server, **{PRODUCER: {"parameters": [definition]}})
    with pytest.raises(Fail, match="accepts alpha, beta"):
        commands.run(jenkins, run_args(stage="compile", param=["PICK=zzz"]))
    assert not _trigger_posts(server)
    assert commands.run(jenkins, run_args(stage="compile", param=["PICK=alpha"])) == 0


def test_run_refuses_a_secret_typed_param(jenkins, server) -> None:
    seed_preview(
        server,
        **{PRODUCER: {"parameters": [{"name": "SECRET", "type": "PasswordParameterDefinition"}]}},
    )
    with pytest.raises(Fail, match="ps and shell history"):
        commands.run(jenkins, run_args(stage="compile", param=["SECRET=hunter2"]))
    assert not _trigger_posts(server)


def test_run_echoes_params_and_the_defaults_count_before_the_prompt(
    jenkins, server, monkeypatch, capsys
) -> None:
    seed_preview(server, **{PRODUCER: {"parameters": [{"name": "CLOUD"}, {"name": "OTHER"}]}})
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert commands.run(jenkins, run_args(stage="compile", yes=False, param=["CLOUD=AWS"])) == 0
    out = capsys.readouterr().out
    assert "param  CLOUD=AWS" in out
    assert "default 1 other declared parameters keep their defaults" in out
    assert out.index("param  CLOUD=AWS") < out.index("aborted")
    assert not _trigger_posts(server)


def test_run_carries_params_into_the_then_hint(jenkins, server, capsys) -> None:
    declares_cloud = {"parameters": [{"name": "CLOUD"}]}
    seed_preview(server, **{PRODUCER: declares_cloud, CONSUMER: declares_cloud})
    assert commands.run(jenkins, run_args(stage="compile", param=["CLOUD=AWS"])) == 0
    out = capsys.readouterr().out
    assert f"run {FOLDER} --stage test -p CLOUD=AWS" in out


def test_run_drops_the_hint_params_the_next_stage_does_not_declare(jenkins, server, capsys) -> None:
    """A pasted hint must never refuse. Stage two's job does not declare CLOUD,
    so the printed command carries no -p."""
    seed_preview(server, **{PRODUCER: {"parameters": [{"name": "CLOUD"}]}})
    assert commands.run(jenkins, run_args(stage="compile", param=["CLOUD=AWS"])) == 0
    out = capsys.readouterr().out
    assert f"run {FOLDER} --stage test   (once this stage is green)" in out
    assert "--stage test -p" not in out


def test_run_dies_when_a_job_loses_its_parameters_mid_prompt(jenkins, server) -> None:
    """The pre-prompt read serves validation only. The trigger loop re-reads,
    so a job edited while the prompt sat open dies with the truth instead of
    riding stale knowledge into a misdirected POST."""
    seed_preview(server, **{PRODUCER: {"parameters": [{"name": "CLOUD"}]}})
    server.drop_params_after_reads = 2
    with pytest.raises(Fail, match="lost its parameters"):
        commands.run(jenkins, run_args(stage="compile", param=["CLOUD=AWS"]))
    assert not _trigger_posts(server)


# --------------------------------------------------------------------------- #
# Sync: exact-folder republish, inherited acknowledgment, strict refusals
# --------------------------------------------------------------------------- #


def sync_args(**overrides) -> argparse.Namespace:
    defaults = {"folder": FOLDER, "allow_foreign_fetch": False}
    return argparse.Namespace(**{**defaults, **overrides})


OLD_SHA = "c" * 40


def _age_pin(server: FakeJenkinsServer, folder: str = FOLDER) -> None:
    """Rewrite the marker's pin so the branch tip reads as moved."""
    description = server.folders[folder]["description"]
    server.folders[folder]["description"] = re.sub(r"sha=\S+", f"sha={OLD_SHA}", description)


@pytest.fixture
def offline_git_foreign(monkeypatch: pytest.MonkeyPatch):
    """Like offline_git, but the fake checkout's script fetches the canonical
    repo, so the fidelity scan yields one blocking escape."""
    monkeypatch.setattr(commands, "resolve_ref", lambda repo, ref: (SHA, f"refs/heads/{ref}"))

    def fake_checkout(repo, sha, anchor, dest) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "pipe.groovy").write_text(
            "git branch: 'master', url: 'https://github.com/upstream/jenkins-pipelines'\n"
        )

    monkeypatch.setattr(commands, "checkout_at", fake_checkout)
    monkeypatch.setattr(
        commands,
        "render",
        lambda workdir, job_set, outdir: dict.fromkeys(job_set.jobs, RAW_PIPELINE),
    )


def test_sync_republishes_at_the_new_tip_with_inherited_ack(
    jenkins, server, offline_git_foreign, capsys
) -> None:
    assert commands.up(jenkins, up_args(allow_foreign_fetch=True)) == 0
    _age_pin(server)
    assert commands.sync(jenkins, sync_args()) == 0
    out = capsys.readouterr().out
    assert "inheriting the acknowledgment stamped at publish" in out
    assert f"sha={SHA}" in server.folders[FOLDER]["description"]
    assert [p for m, p in server.calls if m == "POST" and p == f"{PATH}/doDelete"]


def test_sync_refuses_when_the_blocking_set_changes(
    jenkins, server, offline_git_foreign, monkeypatch
) -> None:
    assert commands.up(jenkins, up_args(allow_foreign_fetch=True)) == 0
    _age_pin(server)

    def two_fetches(repo, sha, anchor, dest) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "pipe.groovy").write_text(
            "git branch: 'master', url: 'https://github.com/upstream/jenkins-pipelines'\n"
            "sh 'curl https://raw.githubusercontent.com/upstream/jenkins-pipelines/x/y'\n"
        )

    monkeypatch.setattr(commands, "checkout_at", two_fetches)
    with pytest.raises(Fail, match="no longer covers"):
        commands.sync(jenkins, sync_args())
    assert FOLDER in server.folders
    assert f"sha={OLD_SHA}" in server.folders[FOLDER]["description"]


def test_sync_without_a_stamped_ack_refuses_blocking_fetches(
    jenkins, server, offline_git_foreign
) -> None:
    """A 0.5.0-era marker carries no ackdigest: nothing is inherited, and the
    flag on sync itself is the explicit path through."""
    server.seed_folder(FOLDER, marker(ref="topic", sha=OLD_SHA))
    with pytest.raises(Fail, match="would silently test code outside your fork"):
        commands.sync(jenkins, sync_args())
    assert f"sha={OLD_SHA}" in server.folders[FOLDER]["description"]
    assert commands.sync(jenkins, sync_args(allow_foreign_fetch=True)) == 0
    assert f"sha={SHA}" in server.folders[FOLDER]["description"]


def test_sync_drives_the_exact_named_folder(jenkins, server, offline_git, capsys) -> None:
    """A --name preview must sync in place. Deriving the folder from set and
    branch would fork it, or worse, tear down an unrelated derived-name one."""
    named = "preview-alice-hotfix"
    server.seed_folder(named, marker(ref="topic", sha=OLD_SHA))
    server.seed_folder(FOLDER, marker(ref="topic"))
    assert commands.sync(jenkins, sync_args(folder=named)) == 0
    deletes = [p for m, p in server.calls if m == "POST" and p.endswith("doDelete")]
    assert deletes == [f"/job/previews/job/{named}/doDelete"]
    assert f"sha={SHA}" in server.folders[named]["description"]
    assert f"target /job/previews/job/{named}" in capsys.readouterr().out


def test_sync_honours_a_no_tab_publish(jenkins, server, offline_git) -> None:
    server.seed_folder(FOLDER, marker(ref="topic", sha=OLD_SHA, view="none"))
    assert commands.sync(jenkins, sync_args()) == 0
    assert not [p for m, p in server.calls if "createView" in p]


def test_sync_recreates_the_paired_tab(jenkins, server, offline_git) -> None:
    server.seed_folder(FOLDER, marker(ref="topic", sha=OLD_SHA, view=FOLDER))
    server.views[FOLDER] = {
        "description": view_marker(preview_id="cafe" * 8, folder=FOLDER, user="alice")
    }
    assert commands.sync(jenkins, sync_args()) == 0
    assert [p for m, p in server.calls if "createView" in p]
    assert FOLDER in server.views


def test_sync_refuses_an_exact_sha_pin(jenkins, server, offline_git) -> None:
    server.seed_folder(FOLDER, marker(ref=SHA, sha=OLD_SHA))
    with pytest.raises(Fail, match="pinned to the exact commit"):
        commands.sync(jenkins, sync_args())
    assert not [p for m, p in server.calls if p.endswith("doDelete")]


def test_sync_refuses_a_non_branch_anchor(jenkins, server, offline_git) -> None:
    server.seed_folder(FOLDER, marker(ref="v1.0", sha=OLD_SHA, anchor="refs/tags/v1.0"))
    with pytest.raises(Fail, match="not a branch"):
        commands.sync(jenkins, sync_args())


def test_sync_refuses_another_owners_preview(jenkins, server, offline_git) -> None:
    server.seed_folder(FOLDER, marker(ref="topic", sha=OLD_SHA, user="bob"))
    with pytest.raises(Fail, match="belongs to owner"):
        commands.sync(jenkins, sync_args())
    assert f"sha={OLD_SHA}" in server.folders[FOLDER]["description"]


def test_sync_is_a_noop_at_the_tip(jenkins, server, offline_git, capsys) -> None:
    server.seed_folder(FOLDER, marker(ref="topic"))
    assert commands.sync(jenkins, sync_args()) == 0
    out = capsys.readouterr().out
    assert "nothing to sync" in out
    assert "up --set pxb-8.1 --update" in out
    assert not [p for m, p in server.calls if p.endswith("doDelete")]


def test_sync_refuses_running_builds_before_rendering(jenkins, server, monkeypatch) -> None:
    seed_preview(server, **{PRODUCER: {"building": True}})
    _age_pin(server)

    def no_clone(*a, **k):
        raise AssertionError("sync must refuse before the clone")

    monkeypatch.setattr(commands, "checkout_at", no_clone)
    monkeypatch.setattr(commands, "resolve_ref", lambda repo, ref: (SHA, "refs/heads/topic"))
    with pytest.raises(Fail, match="still running"):
        commands.sync(jenkins, sync_args())


def test_sync_asserts_identity_before_teardown(jenkins, server, offline_git, monkeypatch) -> None:
    """A publish that replaces the folder between sync's marker read and the
    teardown must stop the teardown, or sync destroys somebody else's rebuild."""
    server.seed_folder(FOLDER, marker(ref="topic", sha=OLD_SHA))
    real_render = commands.render

    def render_and_swap(workdir, job_set, outdir):
        server.folders[FOLDER]["description"] = marker(
            ref="topic", sha=OLD_SHA, preview_id="beef" * 8
        )
        return real_render(workdir, job_set, outdir)

    monkeypatch.setattr(commands, "render", render_and_swap)
    with pytest.raises(Fail, match="changed identity"):
        commands.sync(jenkins, sync_args())
    assert FOLDER in server.folders, "the replaced folder must survive"
    deletes = [p for m, p in server.calls if m == "POST" and p.endswith("doDelete")]
    assert f"{PATH}/doDelete" not in deletes, "only the temp sibling may be rolled back"


def test_update_keeps_the_old_preview_when_the_controller_fails_mid_publish(
    jenkins, server, offline_git
) -> None:
    """The swap protocol's whole point: a controller-side failure while the
    replacement publishes must leave the old preview untouched."""
    seed_preview(server)
    server.fail_enable_readback = True
    with pytest.raises(Fail, match="still disabled"):
        commands.up(jenkins, up_args(update=True))
    assert FOLDER in server.folders, "the old preview must survive"
    assert list(server.folders) == [FOLDER], "the temp sibling must be rolled back"


def test_update_swaps_the_replacement_into_place(jenkins, server, offline_git) -> None:
    seed_preview(server)
    assert commands.up(jenkins, up_args(update=True)) == 0
    assert list(server.folders) == [FOLDER], "exactly the final folder remains"
    ordered = [(m, p) for m, p in server.calls if m == "POST"]
    temp_create = next(i for i, (_, p) in enumerate(ordered) if "previews/createItem" in p)
    old_delete = next(i for i, (_, p) in enumerate(ordered) if p == f"{PATH}/doDelete")
    rename = next(i for i, (_, p) in enumerate(ordered) if "confirmRename" in p)
    assert temp_create < old_delete < rename, "publish fully, then delete, then rename"


def test_update_keeps_the_replacement_when_the_rename_fails(jenkins, server, offline_git) -> None:
    seed_preview(server)
    server.fail_rename = True
    with pytest.raises(Fail, match="fully working at"):
        commands.up(jenkins, up_args(update=True))
    survivors = [name for name in server.folders if name.startswith(f"{FOLDER[:55]}-sw")]
    assert survivors, "the fully published replacement must survive under the temp name"
    assert all(job["buildable"] for job in server.folders[survivors[0]]["jobs"].values())


def test_update_survives_an_ambiguous_teardown(jenkins, server, offline_git, capsys) -> None:
    """The delete lands but its response is lost. Rolling the replacement back
    here would leave zero previews, so the swap probes and continues instead."""
    seed_preview(server)
    server.fail_teardown_after_delete = True
    assert commands.up(jenkins, up_args(update=True)) == 0
    assert list(server.folders) == [FOLDER], "the swap must complete on the final name"
    assert "Continuing the swap" in capsys.readouterr().out


def test_update_names_both_folders_when_the_probe_also_fails(
    jenkins, server, offline_git, capsys
) -> None:
    """A full outage: the delete never confirms and the probe cannot answer.
    Rolling back on a guess could destroy the only working copy, so nothing is
    rolled back and the message names both survivors."""
    seed_preview(server)
    server.fail_teardown_and_probe = True
    with pytest.raises(Fail, match="cannot reach"):
        commands.up(jenkins, up_args(update=True))
    assert FOLDER in server.folders, "the old preview must not be guessed away"
    assert [name for name in server.folders if name.startswith(f"{FOLDER}-sw")], (
        "the replacement must survive under its swap name"
    )
    assert "so did the probe" in capsys.readouterr().out


def test_down_leaves_a_view_paired_with_a_live_replacement(jenkins, server, capsys) -> None:
    """A swap that crashed before its rename leaves a temp folder whose marker
    names the final view, one it never owned. Once the final preview is
    republished that view belongs to the new publish, and dying on the id
    mismatch would wedge the temp beyond both down and reap."""
    seed_preview(server)
    server.views[FOLDER] = {
        "description": view_marker(preview_id="cafe" * 8, folder=FOLDER, user="alice")
    }
    temp = f"{FOLDER}-swabc123"
    server.seed_folder(temp, marker(view=FOLDER, preview_id="dead" * 8))
    assert commands.down(jenkins, down_args(folder=temp)) == 0
    assert temp not in server.folders
    assert FOLDER in server.folders and FOLDER in server.views
    assert "paired with a live preview" in capsys.readouterr().out


def test_sync_refuses_a_pre_060_marker(jenkins, server, offline_git) -> None:
    server.seed_folder(FOLDER, marker(ref="", sha=OLD_SHA))
    with pytest.raises(Fail, match=r"published before 0\.6\.0"):
        commands.sync(jenkins, sync_args())
    assert f"sha={OLD_SHA}" in server.folders[FOLDER]["description"]


def test_status_hints_up_update_for_an_unsyncable_preview(
    jenkins, server, monkeypatch, capsys
) -> None:
    """A sha-pinned preview cannot sync, so the moved-branch hint must not
    point at a command guaranteed to refuse."""
    server.seed_folder(FOLDER, marker(ref=SHA))
    monkeypatch.setattr(commands, "resolve_ref", lambda repo, ref: ("b" * 40, "refs/heads/topic"))
    assert commands.status(jenkins, argparse.Namespace(folder=FOLDER)) == 0
    out = capsys.readouterr().out
    assert "up --set pxb-8.1 --update" in out
    assert f"sync {FOLDER}" not in out


def test_update_keeps_the_folder_when_the_tab_fails(jenkins, server, offline_git) -> None:
    """Fresh publishes still roll everything back on a bad tab. A swapped-in
    replacement is a complete verified preview, so it survives its tab."""
    seed_preview(
        server,
    )
    server.seed_folder(FOLDER, marker(view=FOLDER))
    server.views[FOLDER] = {
        "description": view_marker(preview_id="cafe" * 8, folder=FOLDER, user="alice")
    }
    server.view_lists_stray = True
    with pytest.raises(Fail, match="expected exactly"):
        commands.up(jenkins, up_args(update=True, root_view=True))
    assert FOLDER in server.folders, "the swapped-in preview must survive its tab"
    assert FOLDER not in server.views, "the bad tab must not be left advertising it"
