"""Ref resolution against a real local repository (gates G2 and G3).

`git ls-remote` and `git clone` treat a filesystem path as a remote, so the
Jenkins-driven rules (anchor required, annotated tags peeled, checkout of a
reachable non-tip commit) are provable here without any network.
"""

import subprocess
from pathlib import Path

import pytest

from jenkins_preview.errors import Fail
from jenkins_preview.gitref import branch_of, checkout_at, remote_tips, resolve_ref


@pytest.fixture
def origin(tmp_path: Path) -> tuple[str, dict[str, str]]:
    """A local repo exposing branch my-topic (two commits), annotated tag v1 on
    the first commit, and one dangling commit no ref reaches."""
    repo = tmp_path / "origin"
    repo.mkdir()

    def git(*args: str) -> str:
        done = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
        return done.stdout.strip()

    git("init", "-q", "-b", "my-topic")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (repo / "f").write_text("one")
    git("add", "f")
    git("commit", "-q", "-m", "one")
    first = git("rev-parse", "HEAD")
    git("tag", "-a", "v1", "-m", "annotated")
    (repo / "f").write_text("two")
    git("commit", "-aq", "-m", "two")
    tip = git("rev-parse", "HEAD")
    dangling = git("commit-tree", "HEAD^{tree}", "-m", "dangling")
    return str(repo), {"first": first, "tip": tip, "dangling": dangling}


def test_a_branch_resolves_to_its_tip_with_an_anchor(origin) -> None:
    repo, shas = origin
    assert resolve_ref(repo, "my-topic") == (shas["tip"], "refs/heads/my-topic")


def test_an_annotated_tag_resolves_to_the_peeled_commit(origin) -> None:
    """The tag OBJECT's id would fail the checkout comparison. Only the peeled
    commit is pinnable, which is why remote_tips prefers the ^{} line."""
    repo, shas = origin
    sha, anchor = resolve_ref(repo, "v1")
    assert (sha, anchor) == (shas["first"], "refs/tags/v1")
    assert remote_tips(repo)["refs/tags/v1"] == shas["first"]


def test_an_anchored_sha_is_accepted_case_insensitively(origin) -> None:
    repo, shas = origin
    sha, anchor = resolve_ref(repo, shas["tip"].upper())
    assert (sha, anchor) == (shas["tip"], "refs/heads/my-topic")


def test_a_dangling_sha_is_refused(origin) -> None:
    """Gate G3: a commit no ref reaches cannot be fetched by the agent later."""
    repo, shas = origin
    with pytest.raises(Fail, match="not reachable through any branch or tag"):
        resolve_ref(repo, shas["dangling"])


def test_an_unknown_ref_is_refused(origin) -> None:
    repo, _ = origin
    with pytest.raises(Fail, match="not found in"):
        resolve_ref(repo, "no-such-branch")


def test_checkout_at_lands_on_a_reachable_non_tip_commit(origin, tmp_path) -> None:
    """The pin does not need to be a branch tip, only reachable from the anchor.
    This is the exact case the non-shallow clone exists to serve."""
    repo, shas = origin
    dest = tmp_path / "work"
    checkout_at(repo, shas["first"], "refs/heads/my-topic", dest)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=dest, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert head == shas["first"]
    assert (dest / "f").read_text() == "one"


def test_branch_of_keeps_slashes() -> None:
    assert branch_of("refs/heads/feature/foo") == "feature/foo"
    assert branch_of("refs/tags/v1") == "v1"


def test_pull_request_refs_get_the_source_branch_hint(origin) -> None:
    """A pull ref that does not resolve dies naming the fix, the PR's source
    branch, instead of the generic not-found message."""
    repo, _ = origin
    for form in ("refs/pull/6/head", "pull/6/head", "refs/pull/123/merge"):
        with pytest.raises(Fail, match="pull-request ref"):
            resolve_ref(repo, form)


def test_a_real_branch_named_like_a_pull_ref_still_resolves(origin, tmp_path) -> None:
    """The hint only upgrades the not-found path, so a branch literally named
    pull/1/head keeps working."""
    repo, shas = origin
    subprocess.run(
        ["git", "-C", repo, "update-ref", "refs/heads/pull/1/head", shas["tip"]],
        check=True,
        capture_output=True,
    )
    assert resolve_ref(repo, "pull/1/head") == (shas["tip"], "refs/heads/pull/1/head")
