"""Resolving a git ref to a commit, and fetching the tree at that commit.

The rules encoded here come from how the Jenkins git client behaves at build time,
not from git itself. See docs/design.md.
"""

import re
import subprocess
from pathlib import Path

from .errors import Fail, die

SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")

# GitHub-style pull-request refs. They resolve on the server, but build agents
# fetch refs/heads/*, so a pull ref could publish and then never build.
PULL_REF_PATTERN = re.compile(r"^(refs/)?pull/\d+/(head|merge)$")


def run_git(args: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        # Name the git subcommand, skipping any -c options, so the message is useful.
        subcommand = next((arg for arg in args if not arg.startswith("-")), args[0])
        die(
            f"git {subcommand} failed: {result.stderr.strip()[:400]}",
            "check the repository URL is reachable and the ref exists",
        )
    return result.stdout


def remote_tips(repo: str) -> dict[str, str]:
    """Map every head and tag ref name to the COMMIT it resolves to.

    An annotated tag's own line carries the tag object's id, not a commit. git emits
    a following `^{}` line with the peeled commit, and that is the one that matters:
    pinning a tag object id would make the checkout compare unequal and fail.
    """
    tips: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in run_git(["ls-remote", "--heads", "--tags", repo]).splitlines():
        sha, _, name = line.partition("\t")
        name, sha = name.strip(), sha.strip()
        if not name:
            continue
        if name.endswith("^{}"):
            peeled[name.removesuffix("^{}")] = sha
        else:
            tips[name] = sha
    tips.update(peeled)  # a peeled commit always wins over the tag object
    if not tips:
        die(f"{repo} exposes no heads or tags", "check the URL and that the repo is readable")
    return tips


def resolve_ref(repo: str, ref: str) -> tuple[str, str]:
    """Return (commit, anchor_ref). Gates G2 and G3.

    A bare commit is accepted only when a head or tag resolves to it. That is
    stricter than Jenkins strictly needs, since the agent can resolve any commit
    reachable from the fetched refspec. It is deliberate: it guarantees the
    single-branch clone here contains the commit, and it gives `status` an anchor to
    compare the pin against later.
    """
    tips = remote_tips(repo)

    if SHA_PATTERN.match(ref):
        wanted = ref.lower()
        for name, sha in tips.items():
            if sha.lower() == wanted:
                return sha, name
        die(
            f"commit {ref[:12]} is not reachable through any branch or tag tip in {repo}",
            "push a branch pointing at that commit and pass the branch name instead. "
            "This tool pins only commits it can locate from a ref",
        )

    for candidate in (f"refs/heads/{ref}", f"refs/tags/{ref}", ref):
        if candidate in tips:
            return tips[candidate], candidate

    # A branch literally named pull/N/head resolves above, so this only upgrades
    # the message for refs that truly did not resolve.
    if PULL_REF_PATTERN.match(ref):
        die(
            f"ref '{ref}' is a pull-request ref, not a branch or tag",
            "preview the pull request's source branch instead, passing --repo when "
            "the PR comes from a fork. Only branches and tags can anchor a preview",
        )
    # Raised directly rather than through die(), so the control flow is obvious to
    # both a reader and a linter: this function has no fall-through path.
    raise Fail(
        f"ref '{ref}' not found in {repo}",
        "pass an existing branch or tag, or a commit a branch points at. "
        f"`git ls-remote {repo}` lists what the remote exposes",
    )


def local_context(cwd: Path | None = None) -> tuple[str, str, str]:
    """Infer (repo_url, branch, local_head) from the clone the user is standing in.

    Exists so the common case needs zero flags: an engineer testing a change runs
    `up --set ...` from inside their fork checkout, and the tool reads where that
    checkout came from instead of making them retype it.
    """
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        die(
            "not inside a git checkout, so --repo and --ref cannot be inferred",
            "run from inside your jenkins-pipelines clone, or pass --repo and --ref",
        )
    repo = run_git(["remote", "get-url", "origin"], cwd=cwd).strip()
    branch_probe = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if branch_probe.returncode != 0:
        die(
            "this checkout is on a detached HEAD, so --ref cannot be inferred",
            "check out a branch, or pass --ref explicitly",
        )
    branch = branch_probe.stdout.strip()
    local_head = run_git(["rev-parse", "HEAD"], cwd=cwd).strip()
    return repo, branch, local_head


def branch_of(anchor: str) -> str:
    """The branch or tag name inside a full ref, keeping any slashes.

    `refs/heads/feature/foo` is the branch `feature/foo`, not `foo`.
    """
    for prefix in ("refs/heads/", "refs/tags/"):
        if anchor.startswith(prefix):
            return anchor.removeprefix(prefix)
    return anchor


def checkout_at(repo: str, sha: str, anchor: str, dest: Path) -> None:
    """Clone blobless and NON-shallow, then check out the pinned commit.

    Shallow would risk a clone that does not contain the commit, which is the same
    failure class the heavyweight-checkout rule exists to avoid.
    """
    run_git(
        [
            "clone",
            "--filter=blob:none",
            "--single-branch",
            "--branch",
            branch_of(anchor),
            "--quiet",
            repo,
            str(dest),
        ]
    )
    run_git(["-c", "advice.detachedHead=false", "checkout", "--quiet", sha], cwd=dest)
    head = run_git(["rev-parse", "HEAD"], cwd=dest).strip()
    if head.lower() != sha.lower():
        die(
            f"checkout landed on {head[:12]} instead of {sha[:12]}",
            "re-run. If it repeats, the ref moved mid-run or the remote is inconsistent",
        )
