"""Every gate must fire on a violation and stay quiet on clean input.

A gate nobody has seen fire is not a gate. These tests need no network and no
Jenkins, so they run in CI.

One lesson worth keeping: an empty XML element serialises with whitespace inside it,
so injecting a violation with a naive string replace can silently match nothing and
pass while proving nothing. Inject through ElementTree and assert it landed.
"""

import re
import xml.etree.ElementTree as ET

import pytest

from jenkins_preview.client import assert_view_path, assert_write_path
from jenkins_preview.errors import Fail
from jenkins_preview.folders import (
    folder_marker,
    folder_name,
    folder_path,
    parse_folder_marker,
    parse_view_marker,
    view_job_names,
    view_marker,
)
from jenkins_preview.names import check_name, check_repo_url
from jenkins_preview.sets import SETS, pick_set
from jenkins_preview.transform import sanitize, validate, verify_readback

FORK = "https://github.com/someone/jenkins-pipelines"
SHA = "a" * 40
OTHER_SHA = "b" * 40
WIDE = "+refs/heads/*:refs/remotes/origin/*"

PIPELINE_XML = """<flow-definition>
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
      <extensions>
        <hudson.plugins.git.extensions.impl.CloneOption>
          <shallow>true</shallow>
          <honorRefspec>true</honorRefspec>
        </hudson.plugins.git.extensions.impl.CloneOption>
      </extensions>
    </scm>
    <lightweight>true</lightweight>
  </definition>
  <authToken>a-remote-build-token</authToken>
  <triggers>
    <hudson.triggers.TimerTrigger><spec>H 2 * * *</spec></hudson.triggers.TimerTrigger>
  </triggers>
  <description>original</description>
</flow-definition>"""

MATRIX_XML = """<matrix-project>
  <description>original</description>
  <triggers/>
  <axes/>
</matrix-project>"""


@pytest.fixture
def clean() -> str:
    return sanitize(PIPELINE_XML, fork_url=FORK, sha=SHA, description="preview")


@pytest.fixture
def job_set():
    return pick_set("pxb-8.1")


def _inject(xml_text: str, parent_tag: str, child_tag: str, text: str | None = None) -> str:
    root = ET.fromstring(xml_text)
    parent = root.find(parent_tag) if parent_tag != "." else root
    assert parent is not None, f"fixture has no <{parent_tag}>"
    element = ET.SubElement(parent, child_tag)
    element.text = text
    injected = ET.tostring(root, encoding="unicode")
    assert child_tag in injected, "injection did not land"
    return injected


# --------------------------------------------------------------------------- #
# G1: the write-path gate
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    [
        "/createItem?name=anything",
        "/job/percona-xtrabackup-8.1-compile-pipeline/config.xml",
        "/job/previewsfake/job/x/doDelete",
        "/job/other/job/previews/config.xml",
        "",
        # traversal, the finding that broke the tool's central claim
        "/job/previews/job/a/../../../../job/production/doDelete",
        "/job/previews/job/../production/doDelete",
        "/job/previews/job/a%2f..%2fb/doDelete",
        "/job/previews/job/a//b/doDelete",
        "/job/previews/job/a\\b/doDelete",
        # an unsafe folder segment
        "/job/previews/job/has space/doDelete",
        "/job/previews/job/.hidden/doDelete",
        # an unrecognised endpoint directly under the preview root
        "/job/previews/doDelete",
        "/job/previews/createItemBogus",
        "/job/previews/createItem",
        "/job/previews/createItem?name=a|b",
        "/job/previews/createItem?other=x",
    ],
)
def test_g1_refuses_unsafe_write_paths(path: str) -> None:
    with pytest.raises(Fail):
        assert_write_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "/job/previews/createItem?name=preview-me-x",
        "/job/previews/job/preview-me-x/doDelete",
        "/job/previews/job/preview-me-x/job/a-job/enable",
        "/job/previews/job/preview-me-x/job/a-job/buildWithParameters",
    ],
)
def test_g1_allows_writes_inside_the_previews_folder(path: str) -> None:
    assert_write_path(path)


# --------------------------------------------------------------------------- #
# G11: the view gate
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("path", "name"),
    [
        ("/createView?name=preview-me-x&mode=ListView", "preview-me-x"),
        ("/view/preview-me-x/config.xml", "preview-me-x"),
        ("/view/PXB%208.0/doDelete", "preview-me-x"),
        ("/createView?name=other", "preview-me-x"),
        ("/view/preview-me-x/doDelete?extra=1", "preview-me-x"),
        ("/createView?name=a/b", "a/b"),
    ],
)
def test_g11_refuses_anything_but_its_own_two_endpoints(path: str, name: str) -> None:
    with pytest.raises(Fail):
        assert_view_path(path, name)


@pytest.mark.parametrize(
    "path",
    ["/createView?name=preview-me-x", "/view/preview-me-x/doDelete"],
)
def test_g11_allows_its_own_endpoints(path: str) -> None:
    assert_view_path(path, "preview-me-x")


# --------------------------------------------------------------------------- #
# Name and URL validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name",
    ["a/b", "..", ".", "-leading", "has space", "x" * 65, "", "a|.*", "a\\Eb", "%2e"],
)
def test_unsafe_names_are_refused(name: str) -> None:
    with pytest.raises(Fail):
        check_name(name, "test name")


@pytest.mark.parametrize("name", ["a", "pxb-8.1-1707cc04", "preview-me-topic_1", "A9"])
def test_safe_names_are_accepted(name: str) -> None:
    assert check_name(name, "test name") == name


def test_repo_url_with_embedded_credentials_is_refused() -> None:
    with pytest.raises(Fail, match="embedded credentials"):
        check_repo_url("https://user:token@github.com/me/repo")


def test_repo_url_with_unsupported_scheme_is_refused() -> None:
    with pytest.raises(Fail, match="scheme"):
        check_repo_url("file:///etc/passwd")


@pytest.mark.parametrize(
    "repo",
    ["https://github.com/me/repo", "ssh://git@github.com/me/repo", "git://host/repo"],
)
def test_plain_repo_urls_are_accepted(repo: str) -> None:
    assert check_repo_url(repo) == repo


# --------------------------------------------------------------------------- #
# Sanitising
# --------------------------------------------------------------------------- #


def test_sanitize_forces_every_invariant(clean: str) -> None:
    root = ET.fromstring(clean)
    assert root.findtext(".//lightweight") == "false"
    assert root.findtext(".//hudson.plugins.git.BranchSpec/name") == SHA
    assert root.findtext(".//url") == FORK
    assert root.findtext(".//refspec") == WIDE
    assert root.findtext(".//shallow") == "false"
    assert root.findtext(".//honorRefspec") == "false"
    assert root.findtext(".//noTags") == "false"
    assert root.findtext("disabled") == "true"


def test_sanitize_strips_tokens_and_triggers(clean: str) -> None:
    root = ET.fromstring(clean)
    assert not list(root.iter("authToken"))
    assert not list(root.iter("token"))
    assert not list(root.iter("hudson.triggers.TimerTrigger"))
    assert len(next(iter(root.iter("triggers")))) == 0


def test_sanitize_strips_an_outbound_remote_trigger_token() -> None:
    with_token = _inject(PIPELINE_XML, ".", "token", "outbound-secret")
    cleaned = sanitize(with_token, fork_url=FORK, sha=SHA, description="preview")
    assert not list(ET.fromstring(cleaned).iter("token"))


def test_sanitize_refuses_a_non_commit_pin() -> None:
    with pytest.raises(Fail, match="non-commit"):
        sanitize(PIPELINE_XML, fork_url=FORK, sha="master", description="preview")


def test_sanitize_is_idempotent(clean: str) -> None:
    again = sanitize(clean, fork_url=FORK, sha=SHA, description="preview")
    root = ET.fromstring(again)
    assert root.findtext(".//lightweight") == "false"
    assert root.findtext("disabled") == "true"


def test_sanitize_preserves_nested_descriptions() -> None:
    """A matrix job has nested descriptions. Only the job's own must change."""
    nested = _inject(MATRIX_XML, "axes", "description", "an axis description")
    cleaned = sanitize(nested, fork_url=FORK, sha=SHA, description="preview")
    root = ET.fromstring(cleaned)
    assert root.findtext("description") == "preview"
    assert root.findtext("axes/description") == "an axis description"


# --------------------------------------------------------------------------- #
# G5 and G7
# --------------------------------------------------------------------------- #


def test_g5_accepts_a_clean_config(clean: str, job_set) -> None:
    validate({"a": clean}, fork_url=FORK, sha=SHA, job_set=job_set)


def test_g5_accepts_a_clean_matrix_config(job_set) -> None:
    cleaned = sanitize(MATRIX_XML, fork_url=FORK, sha=SHA, description="preview")
    validate({"a": cleaned}, fork_url=FORK, sha=SHA, job_set=job_set)


def test_g5_refuses_an_inline_pipeline_with_no_scm(job_set) -> None:
    """The vacuous-pass finding: no SCM means nothing pins the preview."""
    inline = (
        '<flow-definition><definition class="org.jenkinsci.plugins.workflow.cps.'
        'CpsFlowDefinition"><script>echo 1</script></definition>'
        "<triggers/><description>x</description><disabled>true</disabled></flow-definition>"
    )
    with pytest.raises(Fail, match="inline pipeline definition"):
        validate({"a": inline}, fork_url=FORK, sha=SHA, job_set=job_set)


def test_g5_catches_a_reintroduced_token(clean: str, job_set) -> None:
    tampered = _inject(clean, ".", "authToken", "x")
    with pytest.raises(Fail, match="remote-build token"):
        validate({"a": tampered}, fork_url=FORK, sha=SHA, job_set=job_set)


def test_g5_catches_lightweight_flipped_back_on(clean: str, job_set) -> None:
    tampered = clean.replace("<lightweight>false", "<lightweight>true")
    with pytest.raises(Fail, match="lightweight"):
        validate({"a": tampered}, fork_url=FORK, sha=SHA, job_set=job_set)


@pytest.mark.parametrize(
    "trigger_tag",
    [
        "hudson.triggers.TimerTrigger",
        "hudson.triggers.SCMTrigger",
        "com.cloudbees.jenkins.GitHubPushTrigger",
        "jenkins.triggers.ReverseBuildTrigger",
    ],
)
def test_g5_catches_a_reintroduced_trigger(clean: str, job_set, trigger_tag: str) -> None:
    tampered = _inject(clean, "triggers", trigger_tag)
    with pytest.raises(Fail, match="trigger"):
        validate({"a": tampered}, fork_url=FORK, sha=SHA, job_set=job_set)


def test_g5_catches_an_unknown_trigger(clean: str, job_set) -> None:
    tampered = _inject(clean, "triggers", "com.example.SomeFutureTrigger")
    with pytest.raises(Fail, match="unrecognised trigger remains"):
        validate({"a": tampered}, fork_url=FORK, sha=SHA, job_set=job_set)


def test_g5_catches_the_wrong_fork(clean: str, job_set) -> None:
    with pytest.raises(Fail, match="not the requested fork"):
        validate({"a": clean}, fork_url="https://github.com/wrong/repo", sha=SHA, job_set=job_set)


def test_g5_catches_the_wrong_commit(clean: str, job_set) -> None:
    with pytest.raises(Fail, match="not the pinned commit"):
        validate({"a": clean}, fork_url=FORK, sha=OTHER_SHA, job_set=job_set)


def test_g5_catches_a_narrowed_refspec(clean: str, job_set) -> None:
    tampered = clean.replace(WIDE, "+refs/heads/main:refs/remotes/origin/main")
    with pytest.raises(Fail, match="may not fetch the pinned commit"):
        validate({"a": tampered}, fork_url=FORK, sha=SHA, job_set=job_set)


def test_g5_catches_honor_refspec_flipped_back_on(clean: str, job_set) -> None:
    tampered = clean.replace("<honorRefspec>false", "<honorRefspec>true")
    with pytest.raises(Fail, match="honorRefspec"):
        validate({"a": tampered}, fork_url=FORK, sha=SHA, job_set=job_set)


def test_g5_catches_a_shallow_clone(clean: str, job_set) -> None:
    tampered = clean.replace("<shallow>false", "<shallow>true")
    with pytest.raises(Fail, match="shallow"):
        validate({"a": tampered}, fork_url=FORK, sha=SHA, job_set=job_set)


@pytest.mark.parametrize(
    "tag",
    ["projectName", "jobName", "childProjects", "downstreamProjectNames", "joinProjects"],
)
def test_g7_catches_a_reference_outside_the_set(clean: str, job_set, tag: str) -> None:
    tampered = _inject(clean, ".", tag, "a-production-job")
    with pytest.raises(Fail, match="not in this set"):
        validate({"a": tampered}, fork_url=FORK, sha=SHA, job_set=job_set)


@pytest.mark.parametrize("value", ["/job/production", "../production", "a/../b"])
def test_g7_catches_an_escaping_reference(clean: str, job_set, value: str) -> None:
    tampered = _inject(clean, ".", "childProjects", value)
    with pytest.raises(Fail, match="escaping job reference"):
        validate({"a": tampered}, fork_url=FORK, sha=SHA, job_set=job_set)


def test_g7_allows_a_reference_to_a_sibling(clean: str, job_set) -> None:
    tampered = _inject(clean, ".", "projectName", "sibling")
    validate({"a": tampered, "sibling": clean}, fork_url=FORK, sha=SHA, job_set=job_set)


def test_g7_treats_a_single_valued_reference_as_one_name(clean: str, job_set) -> None:
    """Job names may contain spaces. Splitting on whitespace invented three names."""
    tampered = _inject(clean, ".", "jobName", "My Compile Job")
    with pytest.raises(Fail, match="'My Compile Job'"):
        validate({"a": tampered}, fork_url=FORK, sha=SHA, job_set=job_set)


def test_g7_allows_a_copyartifact_selector_on_a_published_job(clean: str, job_set) -> None:
    """`myjob/BUILD_TYPE=debug` is a selector, not an absolute path."""
    tampered = _inject(clean, ".", "projectName", "sibling/BUILD_TYPE=debug")
    validate({"a": tampered, "sibling": clean}, fork_url=FORK, sha=SHA, job_set=job_set)


def test_g7_ignores_a_repository_browser_project_name(clean: str, job_set) -> None:
    """A browser's projectName is a repo identifier, not a job reference."""
    root = ET.fromstring(clean)
    browser = ET.SubElement(root, "hudson.plugins.git.browser.GitLab")
    ET.SubElement(browser, "projectName").text = "group/repo"
    validate({"a": ET.tostring(root, encoding="unicode")}, fork_url=FORK, sha=SHA, job_set=job_set)


# --------------------------------------------------------------------------- #
# G6
# --------------------------------------------------------------------------- #


def test_g6_accepts_what_it_sent(clean: str) -> None:
    verify_readback("a", clean, fork_url=FORK, sha=SHA)


def test_g6_is_as_strict_as_g5_about_triggers(clean: str) -> None:
    tampered = _inject(clean, "triggers", "hudson.triggers.TimerTrigger")
    with pytest.raises(Fail, match="trigger"):
        verify_readback("a", tampered, fork_url=FORK, sha=SHA)


def test_g6_is_as_strict_as_g5_about_shallow(clean: str) -> None:
    tampered = clean.replace("<shallow>false", "<shallow>true")
    with pytest.raises(Fail, match="shallow"):
        verify_readback("a", tampered, fork_url=FORK, sha=SHA)


def test_g6_catches_a_stored_config_pinned_elsewhere(clean: str) -> None:
    with pytest.raises(Fail, match="not the pinned commit"):
        verify_readback("a", clean, fork_url=FORK, sha=OTHER_SHA)


# --------------------------------------------------------------------------- #
# G9: ownership markers
# --------------------------------------------------------------------------- #


def test_folder_marker_round_trips() -> None:
    text = folder_marker(
        preview_id="deadbeef",
        user="someone",
        job_set="pxb-8.1",
        repo=FORK,
        sha=SHA,
        anchor="refs/heads/topic",
        view="preview-someone-x",
    )
    fields = parse_folder_marker(text)
    assert fields["owner"] == "someone"
    assert fields["id"] == "deadbeef"
    assert fields["view"] == "preview-someone-x"
    assert "created" in fields


def test_view_marker_round_trips() -> None:
    fields = parse_view_marker(view_marker(preview_id="deadbeef", folder="preview-me-x", user="me"))
    assert fields["id"] == "deadbeef"
    assert fields["folder"] == "previews/preview-me-x"


@pytest.mark.parametrize(
    "description",
    [
        None,
        "",
        "a folder someone else made",
        "previews",
        # the loose-parser finding: the marker quoted mid-sentence is not ours
        "Foreign folder. Example: jenkins-preview:v1:folder note=do-not-delete",
        "please do not delete, see jenkins-preview:v1:folder id=x",
    ],
)
def test_unmarked_descriptions_are_not_ours(description: str | None) -> None:
    assert parse_folder_marker(description) == {}


def test_marker_escapes_xml_metacharacters() -> None:
    """A ref or URL containing & must not produce malformed folder XML."""
    text = folder_marker(
        preview_id="id",
        user="me",
        job_set="s",
        repo="https://host/r?a=1&b=2",
        sha=SHA,
        anchor="refs/heads/feature&fix",
        view="none",
    )
    assert "&amp;" in text
    assert "&b=2" not in text
    ET.fromstring(f"<description>{text}</description>")


def test_view_job_names_are_full_relative_paths() -> None:
    xml = view_job_names("preview-me-x", ["b-job", "a-job"])
    assert "<string>previews/preview-me-x/a-job</string>" in xml
    assert xml.index("a-job") < xml.index("b-job"), "job names should be sorted"


# --------------------------------------------------------------------------- #
# Paths, names, sets
# --------------------------------------------------------------------------- #


def test_folder_paths_stay_inside_the_preview_root() -> None:
    assert folder_path("anything").startswith("/job/previews/job/")
    assert_write_path(folder_path("anything") + "/doDelete")


def test_folder_names_embed_the_owner() -> None:
    assert folder_name("alice", "topic") == "preview-alice-topic"


def test_unknown_set_lists_the_valid_ones() -> None:
    with pytest.raises(Fail, match=re.escape("pxb-8.0, pxb-8.1, pxb-9.x")):
        pick_set("nope")


def test_job_sets_are_hashable() -> None:
    """Frozen dataclasses with collection fields raise on hash. Ours must not."""
    assert len({SETS["pxb-8.0"], SETS["pxb-8.1"]}) == 2


def test_next_stage_walks_the_order(job_set) -> None:
    assert job_set.next_stage("compile") == "test"
    assert job_set.next_stage("test") is None


def test_unknown_stage_is_refused(job_set) -> None:
    with pytest.raises(Fail, match="unknown stage"):
        job_set.stage("nope")


# --------------------------------------------------------------------------- #
# Second review round: the findings that survived the first fix batch
# --------------------------------------------------------------------------- #


def test_base_url_with_query_or_fragment_is_refused() -> None:
    """A query or fragment in the base URL would neutralise the gated path."""
    from jenkins_preview.client import Jenkins

    for url in (
        "https://jenkins.example/createItem?name=production#",
        "https://jenkins.example/#frag",
        "https://user:pw@jenkins.example",
        "ftp://jenkins.example",
        "not-a-url",
    ):
        with pytest.raises(Fail):
            Jenkins(url, "me", "token")


def test_base_url_with_context_path_is_accepted() -> None:
    from jenkins_preview.client import Jenkins

    client = Jenkins("https://host.example/jenkins/", "me", "token")
    assert client.url == "https://host.example/jenkins"


def test_g5_refuses_an_enabled_job(clean: str, job_set) -> None:
    tampered = clean.replace("<disabled>true</disabled>", "<disabled>false</disabled>")
    with pytest.raises(Fail, match="not disabled"):
        validate({"a": tampered}, fork_url=FORK, sha=SHA, job_set=job_set)


def test_g5_refuses_a_missing_refspec(clean: str, job_set) -> None:
    root = ET.fromstring(clean)
    for remote in root.iter("hudson.plugins.git.UserRemoteConfig"):
        for refspec in remote.findall("refspec"):
            remote.remove(refspec)
    with pytest.raises(Fail, match="refspec is missing"):
        validate(
            {"a": ET.tostring(root, encoding="unicode")}, fork_url=FORK, sha=SHA, job_set=job_set
        )


def test_g5_refuses_no_tags(clean: str, job_set) -> None:
    tampered = clean.replace("<noTags>false</noTags>", "<noTags>true</noTags>")
    with pytest.raises(Fail, match="noTags"):
        validate({"a": tampered}, fork_url=FORK, sha=SHA, job_set=job_set)


def test_g5_refuses_a_decoy_scm_outside_the_definition(job_set) -> None:
    """A pinned SCM elsewhere in the document must not satisfy the definition."""
    decoy = f"""<flow-definition>
      <definition class="org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition">
        <lightweight>false</lightweight>
      </definition>
      <scm class="hudson.plugins.git.GitSCM">
        <userRemoteConfigs><hudson.plugins.git.UserRemoteConfig>
          <url>{FORK}</url><refspec>+refs/heads/*:refs/remotes/origin/*</refspec>
        </hudson.plugins.git.UserRemoteConfig></userRemoteConfigs>
        <branches><hudson.plugins.git.BranchSpec><name>{SHA}</name></hudson.plugins.git.BranchSpec></branches>
      </scm>
      <triggers/><description>x</description><disabled>true</disabled>
    </flow-definition>"""
    with pytest.raises(Fail, match="definition's own scm"):
        validate({"a": decoy}, fork_url=FORK, sha=SHA, job_set=job_set)


def test_g7_catches_copyartifact_project(clean: str, job_set) -> None:
    """JJB's copyartifact builder stores the source job in <project>."""
    root = ET.fromstring(clean)
    builder = ET.SubElement(root, "hudson.plugins.copyartifact.CopyArtifact")
    ET.SubElement(builder, "project").text = "/production"
    with pytest.raises(Fail, match="escaping job reference"):
        validate(
            {"a": ET.tostring(root, encoding="unicode")}, fork_url=FORK, sha=SHA, job_set=job_set
        )


def test_g7_catches_copyartifact_project_outside_the_set(clean: str, job_set) -> None:
    root = ET.fromstring(clean)
    builder = ET.SubElement(root, "hudson.plugins.copyartifact.CopyArtifact")
    ET.SubElement(builder, "project").text = "some-production-job"
    with pytest.raises(Fail, match="not in this set"):
        validate(
            {"a": ET.tostring(root, encoding="unicode")}, fork_url=FORK, sha=SHA, job_set=job_set
        )


def test_g7_refuses_a_remote_trigger_plugin(clean: str, job_set) -> None:
    """A remote-trigger plugin starts jobs on ANOTHER controller, where set
    membership proves nothing."""
    root = ET.fromstring(clean)
    trigger = ET.SubElement(
        root,
        "org.jenkinsci.plugins.ParameterizedRemoteTrigger.RemoteBuildConfiguration",
    )
    ET.SubElement(trigger, "job").text = "a"  # in the set, and still refused
    with pytest.raises(Fail, match="remote controller"):
        validate(
            {"a": ET.tostring(root, encoding="unicode")}, fork_url=FORK, sha=SHA, job_set=job_set
        )


def test_names_refuse_a_trailing_newline() -> None:
    with pytest.raises(Fail):
        check_name("safe\n", "test name")


def test_repo_url_with_query_token_is_refused() -> None:
    with pytest.raises(Fail, match="query string"):
        check_repo_url("https://host/repo?access_token=secret")


def test_scp_style_ssh_url_is_accepted() -> None:
    assert check_repo_url("git@github.com:org/repo.git")


# --------------------------------------------------------------------------- #
# Use-case round: source inference, stable slugs, run-next
# --------------------------------------------------------------------------- #


def test_derived_folder_name_is_unchanged_when_short() -> None:
    """Short inputs pass through byte-identical, so folder names stay stable."""
    from jenkins_preview.names import derived_folder_name

    assert derived_folder_name("alice", "pxb-8.1", "topic") == "preview-alice-pxb-8.1-topic"


def test_derived_folder_name_fits_the_cap_and_stays_stable() -> None:
    from jenkins_preview.names import derived_folder_name

    branch = "PXB-3613-add-arm-platforms-to-jenkins"
    fitted = derived_folder_name("satya-bodapati", "pxb-8.1", branch)
    assert fitted == derived_folder_name("satya-bodapati", "pxb-8.1", branch)
    assert len(fitted) <= 64
    assert fitted.startswith("preview-satya-bodapati-pxb-8.1-")
    assert check_name(fitted, "derived folder name") == fitted


def test_two_long_branches_sharing_a_prefix_stay_distinct() -> None:
    from jenkins_preview.names import derived_folder_name

    prefix = "PXB-3613-" + "x" * 60
    first = derived_folder_name("satya-bodapati", "pxb-8.1", f"{prefix}-a")
    second = derived_folder_name("satya-bodapati", "pxb-8.1", f"{prefix}-b")
    assert first != second


def test_branches_that_slug_to_nothing_stay_distinct() -> None:
    """Non-ASCII branches slug to an empty string. Only the digest separates them."""
    from jenkins_preview.names import derived_folder_name

    first = derived_folder_name("alice", "pxb-8.1", "完全中文")
    second = derived_folder_name("alice", "pxb-8.1", "别的分支")
    assert first != second
    assert check_name(first, "derived folder name") == first


def test_an_email_style_user_id_is_slugged_and_digested() -> None:
    """Lossy user slugging appends the digest: `satya@percona.com` and a real
    `satya-percona.com` user must never share a folder."""
    from jenkins_preview.names import derived_folder_name

    fitted = derived_folder_name("satya@percona.com", "pxb-8.1", "topic")
    plain = derived_folder_name("satya-percona.com", "pxb-8.1", "topic")
    assert fitted.startswith("preview-satya-percona.com-pxb-8.1-topic-")
    assert fitted != plain
    assert check_name(fitted, "derived folder name") == fitted


def test_slug_collisions_stay_distinct_via_the_digest() -> None:
    """`feature/foo` and `feature-foo` slug identically. The digest of the raw
    branch keeps their folders apart."""
    from jenkins_preview.names import derived_folder_name

    slashed = derived_folder_name("alice", "pxb-8.1", "feature/foo")
    dashed = derived_folder_name("alice", "pxb-8.1", "feature-foo")
    assert slashed != dashed


def test_a_huge_user_id_is_fitted_not_refused() -> None:
    from jenkins_preview.names import derived_folder_name

    first = derived_folder_name("u" * 60, "pxb-8.1", "topic")
    second = derived_folder_name("v" * 60, "pxb-8.1", "topic")
    assert len(first) <= 64
    assert first != second
    assert check_name(first, "derived folder name") == first


def test_an_impossible_set_name_names_an_achievable_fix() -> None:
    from jenkins_preview.names import derived_folder_name

    with pytest.raises(Fail, match="set name"):
        derived_folder_name("alice", "s" * 60, "topic")


def test_pick_stage_walks_the_dag(job_set) -> None:
    assert job_set.pick_stage(set()) == "compile"
    assert job_set.pick_stage({"compile"}) == "test"
    assert job_set.pick_stage({"compile", "test"}) is None


def test_pick_stage_never_offers_a_consumer_early(job_set) -> None:
    """test consumes compile, so with nothing green only compile is offered."""
    assert job_set.pick_stage({"test"}) == "compile"


def test_local_context_reads_the_checkout(tmp_path) -> None:
    import subprocess

    from jenkins_preview.gitref import local_context

    repo = tmp_path / "clone"
    repo.mkdir()
    run = lambda *a: subprocess.run(  # noqa: E731
        ["git", *a], cwd=repo, check=True, capture_output=True
    )
    run("init", "-q", "-b", "my-topic")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    run("remote", "add", "origin", "https://github.com/someone/jenkins-pipelines")
    (repo / "f").write_text("x")
    run("add", "f")
    run("commit", "-q", "-m", "c")

    url, branch, head = local_context(repo)
    assert url == "https://github.com/someone/jenkins-pipelines"
    assert branch == "my-topic"
    assert len(head) == 40


def test_local_context_refuses_outside_a_checkout(tmp_path) -> None:
    from jenkins_preview.gitref import local_context

    with pytest.raises(Fail, match="not inside a git checkout"):
        local_context(tmp_path)


# --------------------------------------------------------------------------- #
# Security round three: URL composition and XML evasion
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "https://jenkins.example?",
        "https://jenkins.example#",
        "https://jenkins.example/createItem?name=production#",
        "https://jenkins.example/weird path",
        "https://jenkins.example/..%2f",
    ],
)
def test_base_url_shunts_are_refused(url: str) -> None:
    from jenkins_preview.client import Jenkins

    with pytest.raises(Fail):
        Jenkins(url, "me", "token")


def test_g5_refuses_duplicate_lightweight_elements(clean: str, job_set) -> None:
    """Gates read the first element, Jenkins binds the last. Duplicates are refused."""
    root = ET.fromstring(clean)
    definition = next(d for d in root.iter("definition"))
    ET.SubElement(definition, "lightweight").text = "true"
    with pytest.raises(Fail, match="occurs 2 times"):
        validate(
            {"a": ET.tostring(root, encoding="unicode")}, fork_url=FORK, sha=SHA, job_set=job_set
        )


def test_g5_refuses_duplicate_disabled_elements(clean: str, job_set) -> None:
    root = ET.fromstring(clean)
    ET.SubElement(root, "disabled").text = "false"
    with pytest.raises(Fail, match="occurs 2 times"):
        validate(
            {"a": ET.tostring(root, encoding="unicode")}, fork_url=FORK, sha=SHA, job_set=job_set
        )


def test_g5_refuses_a_namespaced_document(job_set) -> None:
    doc = '<flow-definition xmlns="http://example.com/evil"><disabled>true</disabled></flow-definition>'
    with pytest.raises(Fail, match="namespaces"):
        validate({"a": doc}, fork_url=FORK, sha=SHA, job_set=job_set)


def test_g5_refuses_a_qualified_root_with_no_pin(job_set) -> None:
    """A fully qualified root element must not skip the positive pin assertion."""
    doc = (
        "<org.jenkinsci.plugins.workflow.job.WorkflowJob>"
        "<triggers/><description>x</description><disabled>true</disabled>"
        "</org.jenkinsci.plugins.workflow.job.WorkflowJob>"
    )
    with pytest.raises(Fail, match="no CpsScmFlowDefinition"):
        validate({"a": doc}, fork_url=FORK, sha=SHA, job_set=job_set)


def test_g5_refuses_an_inline_definition_under_any_root(job_set) -> None:
    doc = (
        '<matrix-project><definition class="org.jenkinsci.plugins.workflow.cps.CpsFlowDefinition">'
        "<script>echo 1</script></definition>"
        "<triggers/><description>x</description><disabled>true</disabled></matrix-project>"
    )
    with pytest.raises(Fail, match="inline pipeline definition"):
        validate({"a": doc}, fork_url=FORK, sha=SHA, job_set=job_set)


def test_g5_refuses_a_multibranch_project(job_set) -> None:
    """A multibranch project keeps its remotes under <sources>, which no check
    inspects, so it would upload still pointing at production."""
    doc = (
        "<org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject>"
        "<sources><data><jenkins.branch.BranchSource>"
        "<source class='jenkins.plugins.git.GitSCMSource'>"
        "<remote>https://github.com/upstream/jenkins-pipelines</remote>"
        "</source></jenkins.branch.BranchSource></data></sources>"
        "<triggers/><description>x</description><disabled>true</disabled>"
        "</org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject>"
    )
    with pytest.raises(Fail, match="unsupported job type"):
        validate({"a": doc}, fork_url=FORK, sha=SHA, job_set=job_set)
