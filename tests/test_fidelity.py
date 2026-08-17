"""The build-time fidelity scan: classification, exemptions, and scan gaps."""

from pathlib import Path

from jenkins_preview.fidelity import (
    Escape,
    blocking,
    blocking_digest,
    canonical_identities,
    missing,
    repo_identity,
    scan,
)

FORK = "https://github.com/someone/jenkins-pipelines"
SHA = "a" * 40
CANONICAL = "https://github.com/Percona-Lab/jenkins-pipelines"

RAW_CONFIG = f"""<flow-definition>
  <definition class="org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition">
    <scm class="hudson.plugins.git.GitSCM">
      <userRemoteConfigs><hudson.plugins.git.UserRemoteConfig>
        <url>{CANONICAL}</url>
      </hudson.plugins.git.UserRemoteConfig></userRemoteConfigs>
    </scm>
    <scriptPath>pipe.groovy</scriptPath>
  </definition>
</flow-definition>"""

NO_SCRIPT_CONFIG = """<flow-definition>
  <definition class="org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition"/>
</flow-definition>"""


def run_scan(tmp_path: Path, script_text: str | None, fork: str = FORK):
    if script_text is not None:
        (tmp_path / "pipe.groovy").write_text(script_text)
    configs = {"a": RAW_CONFIG}
    return scan(configs, tmp_path, fork, sha=SHA, canonical=canonical_identities(configs))


# --------------------------------------------------------------------------- #
# Repo identity
# --------------------------------------------------------------------------- #


def test_repo_identity_unifies_spellings() -> None:
    forms = (
        "https://GitHub.com/Someone/jenkins-pipelines",
        "https://github.com/Someone/jenkins-pipelines.git",
        "git@github.com:Someone/jenkins-pipelines.git",
        "ssh://git@github.com/Someone/jenkins-pipelines",
        "https://raw.githubusercontent.com/Someone/jenkins-pipelines/master/x.sh",
    )
    assert {repo_identity(url) for url in forms} == {"github.com/Someone/jenkins-pipelines"}


def test_repo_identity_keeps_path_case() -> None:
    assert repo_identity("https://host/CaseSensitive") != repo_identity(
        "https://host/casesensitive"
    )


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


def test_a_canonical_git_step_is_flagged_and_blocking(tmp_path) -> None:
    escapes = run_scan(tmp_path, f"git branch: 'master', url: '{CANONICAL}'\n")
    (escape,) = escapes
    assert (escape.line, escape.kind, escape.detail) == (1, "canonical", CANONICAL)
    assert blocking(escapes) == escapes


def test_a_fork_fetch_in_ssh_spelling_is_exempt(tmp_path) -> None:
    """The same repo under another transport must not read as foreign."""
    assert run_scan(tmp_path, "git url: 'git@github.com:someone/jenkins-pipelines.git'\n") == []


def test_checkout_scm_scans_clean(tmp_path) -> None:
    """`checkout scm` re-checks-out the job's own rewritten SCM, the fork at the
    pinned commit, so it is the recommended fix and must never be flagged.
    Proven live: a preview pipeline using it built green on ps3 with the pinned
    fork SHA and a fork-only marker file in the console."""
    assert run_scan(tmp_path, "checkout scm\nsh 'git rev-parse HEAD'\n") == []


def test_someone_elses_fork_of_the_same_repo_is_canonical(tmp_path) -> None:
    (escape,) = run_scan(tmp_path, "git url: 'https://github.com/eve/jenkins-pipelines'\n")
    assert escape.kind == "canonical"


def test_a_product_repo_fetch_is_external_and_not_blocking(tmp_path) -> None:
    escapes = run_scan(tmp_path, "git url: 'https://github.com/percona/percona-server'\n")
    (escape,) = escapes
    assert escape.kind == "external"
    assert blocking(escapes) == []


def test_a_raw_file_download_of_the_canonical_repo_is_canonical(tmp_path) -> None:
    url = "https://raw.githubusercontent.com/Percona-Lab/jenkins-pipelines/master/x.sh"
    (escape,) = run_scan(tmp_path, f"sh 'wget {url}'\n")
    assert (escape.kind, escape.detail) == ("canonical", url)


# --------------------------------------------------------------------------- #
# Shared libraries
# --------------------------------------------------------------------------- #

REAL_FORM_LIBRARY = (
    'library changelog: false, identifier: "lib@master", retriever: modernSCM([\n'
    "    $class: 'GitSCMSource',\n"
    f"    remote: '{CANONICAL}.git'\n"
    "])\n"
)


def test_the_repo_real_library_form_is_one_library_escape(tmp_path) -> None:
    """The dominant in-repo form spans lines with arguments between `library`
    and `identifier:`. It must yield ONE library escape at the declaration,
    with no separate canonical escape for the remote line it consumed."""
    escapes = run_scan(tmp_path, REAL_FORM_LIBRARY)
    (escape,) = escapes
    assert (escape.line, escape.kind) == (1, "library")


def test_a_library_pinned_to_the_fork_at_the_sha_is_exempt(tmp_path) -> None:
    text = (
        f'library changelog: false, identifier: "lib@{SHA}", retriever: modernSCM([\n'
        f"    remote: '{FORK}'\n"
        "])\n"
    )
    assert run_scan(tmp_path, text) == []


def test_a_library_on_the_fork_but_not_the_sha_is_flagged(tmp_path) -> None:
    """`lib@master` against the fork still floats past the pin, so it warns."""
    text = f"library identifier: \"lib@master\", retriever: modernSCM([remote: '{FORK}'])\n"
    (escape,) = run_scan(tmp_path, text)
    assert escape.kind == "library"


def test_a_global_library_load_is_flagged(tmp_path) -> None:
    (escape,) = run_scan(tmp_path, '@Library("percona-lib") _\n')
    assert escape.kind == "library"


# --------------------------------------------------------------------------- #
# Comments and scan gaps
# --------------------------------------------------------------------------- #


def test_comments_are_not_scanned(tmp_path) -> None:
    text = (
        f"// git url: '{CANONICAL}'\n"
        f"# url: '{CANONICAL}'\n"
        f"/* url: '{CANONICAL}'\n   url: '{CANONICAL}' */\n"
        f"echo 'hi'  // url: '{CANONICAL}'\n"
    )
    assert run_scan(tmp_path, text) == []


def test_a_missing_script_is_its_own_hard_kind(tmp_path) -> None:
    escapes = run_scan(tmp_path, None)
    (escape,) = escapes
    assert (escape.script, escape.kind) == ("pipe.groovy", "missing")
    assert missing(escapes) == escapes
    assert blocking(escapes) == [], "missing is refused harder than blocking, not with it"


def test_an_unparseable_config_is_uninspectable(tmp_path) -> None:
    escapes = scan({"a": "<not-xml"}, tmp_path, FORK, sha=SHA, canonical=set())
    (escape,) = escapes
    assert escape.kind == "uninspectable"
    assert blocking(escapes) == escapes


def test_a_pipeline_without_a_scriptpath_is_uninspectable(tmp_path) -> None:
    (escape,) = scan({"a": NO_SCRIPT_CONFIG}, tmp_path, FORK, sha=SHA, canonical=set())
    assert escape.kind == "uninspectable"


def test_a_script_path_escaping_the_checkout_is_uninspectable(tmp_path) -> None:
    config = RAW_CONFIG.replace("pipe.groovy", "../outside.groovy")
    (escape,) = scan({"a": config}, tmp_path, FORK, sha=SHA, canonical=set())
    assert (escape.kind, escape.detail) == ("uninspectable", "script path escapes the checkout")


def test_jobs_sharing_a_script_deduplicate_to_one_escape(tmp_path) -> None:
    (tmp_path / "pipe.groovy").write_text(f"git url: '{CANONICAL}'\n")
    configs = {"a": RAW_CONFIG, "b": RAW_CONFIG}
    (escape,) = scan(configs, tmp_path, FORK, sha=SHA, canonical=canonical_identities(configs))
    assert escape.kind == "canonical"


def test_a_sibling_reference_outside_the_set_is_blocking(tmp_path) -> None:
    """`copyArtifacts projectName:` in the script resolves folder-first and then
    falls back towards the root, where production lives."""
    text = "copyArtifacts filter: 'x', projectName: 'some-other-job', selector: specific('1')\n"
    escapes = run_scan(tmp_path, text)
    (escape,) = escapes
    assert (escape.kind, escape.detail) == ("sibling", "some-other-job")
    assert blocking(escapes) == escapes


def test_a_sibling_reference_inside_the_set_is_clean(tmp_path) -> None:
    (tmp_path / "pipe.groovy").write_text("build job: 'a', wait: true\n")
    configs = {"a": RAW_CONFIG}
    assert scan(configs, tmp_path, FORK, sha=SHA, canonical=canonical_identities(configs)) == []


def test_an_absolute_job_reference_is_always_a_sibling_escape(tmp_path) -> None:
    (escape,) = run_scan(tmp_path, "build job: '/production-job'\n")
    assert (escape.kind, escape.detail) == ("sibling", "/production-job")


def test_blocking_digest_ignores_pure_line_moves() -> None:
    """Editing a script above an unchanged fetch moves its line without
    changing what was acknowledged, so the stamped acknowledgment survives."""
    before = [Escape(script="a.groovy", line=10, kind="canonical", detail="https://x/y")]
    after = [Escape(script="a.groovy", line=42, kind="canonical", detail="https://x/y")]
    assert blocking_digest(before) == blocking_digest(after)


def test_blocking_digest_changes_when_a_blocker_changes() -> None:
    one = [Escape(script="a.groovy", line=10, kind="canonical", detail="https://x/y")]
    other = [Escape(script="a.groovy", line=10, kind="canonical", detail="https://x/z")]
    assert blocking_digest(one) != blocking_digest(other)
