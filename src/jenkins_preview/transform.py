"""Sanitising rendered configs, and the gates that prove the result is safe.

Sanitising is not optional and is not a flag. A preview that keeps a remote-build
token is externally triggerable, and a preview that keeps a timer trigger builds on
a schedule forever after its author forgot about it.

Known limit, stated rather than implied away: references and triggers declared
inside the pipeline script itself are invisible to a config-XML scan. A declarative
`triggers { cron(...) }` block re-installs a trigger on the preview's first build,
and `build job: '/some-production-job'` in the fork's Groovy reaches production. No
XML gate can prevent either. See docs/design.md.
"""

import re
import xml.etree.ElementTree as ET

from .errors import die
from .sets import JobSet

TRIGGER_TAGS = (
    "hudson.triggers.TimerTrigger",
    "hudson.triggers.SCMTrigger",
    "com.cloudbees.jenkins.GitHubPushTrigger",
    "jenkins.triggers.ReverseBuildTrigger",
    "org.jenkinsci.plugins.gwt.GenericTrigger",
)

CREDENTIAL_TAGS = ("authToken", "token")
"""Inbound remote-build token, and the outbound token of the remote-trigger plugin."""

SINGLE_VALUED_REFERENCES = (
    "projectName",
    "jobName",
    "parentJobName",
    "upstreamProjectName",
    "job",
)
"""One whole job name per element. Jenkins job names may contain spaces."""

LIST_REFERENCES = (
    "projects",
    "childProjects",
    "downstreamProjectNames",
    "joinProjects",
    "blockingJobs",
)
"""Comma-separated lists of job names."""

_CPS_SCM = "CpsScmFlowDefinition"
_REMOTE_CONFIG = "hudson.plugins.git.UserRemoteConfig"
_BRANCH_SPEC = "hudson.plugins.git.BranchSpec"
_CLONE_OPTION = "hudson.plugins.git.extensions.impl.CloneOption"
_WIDE_REFSPEC = "+refs/heads/*:refs/remotes/origin/*"
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")

# Elements whose text is a repository identifier rather than a job reference.
_BROWSER_MARKER = "browser"


def publishable_root(tag: str) -> bool:
    """Only types whose pin gate G5 can actually PROVE are publishable. A
    multibranch project, for one, keeps its remotes under <sources>, which
    the invariant checks never inspect, so it would upload still pointing at
    production. Views are not jobs at all."""
    return tag in {
        "flow-definition",
        "project",
        "matrix-project",
        "com.tikal.jenkins.plugins.multijob.MultiJobProject",
    } or tag.endswith("WorkflowJob")


def _child(parent: ET.Element, tag: str) -> ET.Element:
    found = parent.find(tag)
    return found if found is not None else ET.SubElement(parent, tag)


def _checkout_scms(root: ET.Element) -> list[ET.Element]:
    """The SCM elements that actually drive a checkout.

    Scoped on purpose: rewriting every remote in the document would repoint a second
    repository (a product repo, percona-qa) at the contributor's fork and pin it to a
    commit that does not exist there.
    """
    found: list[ET.Element] = []
    for definition in root.iter("definition"):
        found.extend(definition.findall("scm"))
    found.extend(root.findall("scm"))
    return found


def sanitize(xml_text: str, *, fork_url: str, sha: str, description: str) -> str:
    """Rewrite a rendered config into a safe, pinned preview config."""
    if not _SHA.match(sha):
        die(f"refusing to pin a non-commit value {sha!r}", "this is a bug in the tool")
    root = ET.fromstring(xml_text)

    # 1. no inbound or outbound build tokens survive, at any depth
    for parent in list(root.iter()):
        for child in list(parent):
            if child.tag in CREDENTIAL_TAGS:
                parent.remove(child)

    # 2. no automatic triggers of any kind
    for triggers in root.iter("triggers"):
        for child in list(triggers):
            triggers.remove(child)

    # 3. point the checkout remotes at the fork, pin the commit, and widen the fetch
    #    so the pinned commit is actually retrievable
    for scm in _checkout_scms(root):
        for remote in scm.iter(_REMOTE_CONFIG):
            _child(remote, "url").text = fork_url
            _child(remote, "refspec").text = _WIDE_REFSPEC
        for spec in scm.iter(_BRANCH_SPEC):
            _child(spec, "name").text = sha
        for clone_option in scm.iter(_CLONE_OPTION):
            _child(clone_option, "shallow").text = "false"
            _child(clone_option, "noTags").text = "false"
            _child(clone_option, "honorRefspec").text = "false"

    # 4. heavyweight checkout, always. A bare-commit pin with lightweight enabled
    #    dies at script load in about 300 ms with a misleading git error.
    for definition in root.iter("definition"):
        if _CPS_SCM in definition.get("class", ""):
            _child(definition, "lightweight").text = "false"

    # 5. publish disabled. The caller enables only after the whole set validates
    _child(root, "disabled").text = "true"

    # 6. provenance, so anyone finding this job knows what it is
    _child(root, "description").text = description

    return ET.tostring(root, encoding="unicode")


def validate(configs: dict[str, str], *, fork_url: str, sha: str, job_set: JobSet) -> None:
    """Gates G5 and G7, on the final XML, before anything is uploaded."""
    published = set(configs)
    for job, text in configs.items():
        root = ET.fromstring(text)
        _assert_invariants(job, root, fork_url=fork_url, sha=sha, gate="G5")
        _assert_references_resolve(job, root, published=published, job_set=job_set)


def verify_readback(job: str, live_xml: str, *, fork_url: str, sha: str) -> None:
    """Gate G6. Deliberately the same assertions as G5, against what Jenkins stored.

    G6 exists to catch Jenkins mangling an upload, so anything weaker than G5 would
    defeat its purpose.
    """
    _assert_invariants(job, ET.fromstring(live_xml), fork_url=fork_url, sha=sha, gate="G6")


def _bug(job: str, what: str, gate: str) -> None:
    die(f"{job}: {what} (gate {gate})", "this is a bug in the tool, nothing was published")


def _sole_text(job: str, parent: ET.Element, tag: str, gate: str) -> str:
    """The text of a child that must occur exactly once.

    Duplicate children are refused outright: these gates read the FIRST match while
    Jenkins' XStream binds the LAST, so a duplicated element is a way to show the
    gate one value and run with another.
    """
    found = parent.findall(tag)
    if len(found) > 1:
        _bug(job, f"element <{tag}> occurs {len(found)} times", gate)
    return (found[0].text or "").strip() if found else ""


def _assert_invariants(job: str, root: ET.Element, *, fork_url: str, sha: str, gate: str) -> None:
    # A default-namespaced document turns every tag into "{ns}tag" and would make
    # each check below match nothing. Refuse rather than validate a no-op.
    if any("}" in element.tag for element in root.iter()):
        die(
            f"{job}: the config uses XML namespaces (gate {gate})",
            "namespaced job configs are not supported, so this refuses to publish",
        )

    if not publishable_root(root.tag):
        die(
            f"{job}: unsupported job type <{root.tag}> (gate {gate})",
            "only pipeline, freestyle, matrix and multijob configs can be proven "
            "pinned to your fork",
        )

    if list(root.iter("authToken")):
        _bug(job, "a remote-build token survived sanitising", gate)
    if list(root.iter("token")):
        _bug(job, "an outbound trigger token survived sanitising", gate)

    for tag in TRIGGER_TAGS:
        if list(root.iter(tag)):
            _bug(job, f"trigger {tag} survived sanitising", gate)
    for triggers in root.iter("triggers"):
        if len(list(triggers)):
            die(
                f"{job}: an unrecognised trigger remains (gate {gate})",
                "the tool refuses to publish a config it cannot prove clean",
            )

    # Both gates run BEFORE enabling, so the stored flag must be exactly "true".
    # Accepting "false" here would bless a job that was enabled prematurely.
    if _sole_text(job, root, "disabled", gate) != "true":
        _bug(job, "the job is not disabled at validation time", gate)

    # An inline pipeline definition is refused wherever it appears, whatever the
    # root element is called: it has no commit to pin.
    for definition in root.iter("definition"):
        cls = definition.get("class", "")
        if "CpsFlowDefinition" in cls and _CPS_SCM not in cls:
            die(
                f"{job}: inline pipeline definition (gate {gate})",
                "this tool can only preview pipelines whose script comes from SCM, and "
                "an inline pipeline has no commit to pin",
            )

    # Positive assertion: a pipeline job MUST carry a pinned git checkout INSIDE its
    # own definition. Aggregating remotes across the whole document would let a
    # decoy SCM elsewhere satisfy the check while the definition stays unpinned.
    # Matched on more than the exact root tag: a fully qualified root element must
    # not skip the assertion.
    is_pipeline = root.tag == "flow-definition" or root.tag.endswith("WorkflowJob")
    if is_pipeline:
        cps = [d for d in root.iter("definition") if _CPS_SCM in d.get("class", "")]
        if not cps:
            die(
                f"{job}: pipeline job has no {_CPS_SCM}, so nothing pins it to your fork "
                f"(gate {gate})",
                "this tool can only preview pipelines whose script comes from SCM, and an "
                "inline pipeline has no commit to pin",
            )
        for definition in cps:
            if _sole_text(job, definition, "lightweight", gate) != "false":
                die(
                    f"{job}: lightweight checkout is not disabled (gate {gate})",
                    "a bare-commit pin with lightweight enabled dies at script load in "
                    "about 300 ms, so this refuses to publish",
                )
            own_scms = definition.findall("scm")
            remotes = [r for scm in own_scms for r in scm.iter(_REMOTE_CONFIG)]
            specs = [s for scm in own_scms for s in scm.iter(_BRANCH_SPEC)]
            if not remotes or not specs:
                _bug(job, "the pipeline definition's own scm has no remote or branch spec", gate)

    # Conformance: everything present in any checkout SCM must be correct.
    for scm in _checkout_scms(root):
        for remote in scm.iter(_REMOTE_CONFIG):
            if _sole_text(job, remote, "url", gate) != fork_url:
                _bug(job, "a checkout remote is not the requested fork", gate)
            # The refspec must be PRESENT and wide: an absent refspec inherits
            # whatever the job or plugin defaults to, which is unprovable here.
            if _sole_text(job, remote, "refspec", gate) != _WIDE_REFSPEC:
                _bug(job, "checkout refspec is missing or may not fetch the pinned commit", gate)
        for spec in scm.iter(_BRANCH_SPEC):
            if _sole_text(job, spec, "name", gate) != sha:
                _bug(job, "a branch spec is not the pinned commit", gate)
        for clone_option in scm.iter(_CLONE_OPTION):
            if _sole_text(job, clone_option, "shallow", gate) == "true":
                _bug(job, "a shallow clone may not contain the pinned commit", gate)
            if _sole_text(job, clone_option, "honorRefspec", gate) == "true":
                _bug(job, "honorRefspec would narrow the fetch away from the pinned commit", gate)
            if _sole_text(job, clone_option, "noTags", gate) == "true":
                _bug(job, "noTags would break a tag-anchored pin", gate)


def _assert_references_resolve(
    job: str, root: ET.Element, *, published: set[str], job_set: JobSet
) -> None:
    """Gate G7. Every cross-job reference must resolve inside the published set."""

    def check(name: str, tag: str) -> None:
        name = name.strip()
        if not name:
            return
        if name.startswith("/") or ".." in name:
            die(
                f"{job}: escaping job reference '{name}' in <{tag}> (gate G7)",
                "an absolute or traversing path would drive production jobs from a "
                "preview, so this refuses to publish",
            )
        # A folder or selector separator: only the leading segment names a job.
        head = name.split("/", 1)[0]
        if head not in published:
            die(
                f"{job}: <{tag}> references '{head}', which is not in this set (gate G7)",
                f"add it to the '{job_set.name}' set, or the preview would reach outside "
                "its own folder",
            )

    for element in root.iter():
        tag_lower = element.tag.lower()
        # A remote-trigger plugin starts jobs on ANOTHER controller, where "the name
        # is in this set" proves nothing. Refuse the plugin outright.
        if "remotetrigger" in tag_lower or "remotebuildconfiguration" in tag_lower:
            die(
                f"{job}: <{element.tag}> triggers builds on a remote controller (gate G7)",
                "a preview must not reach outside this Jenkins. Remove the remote "
                "trigger from the job definition",
            )

    for name, tag in cross_job_references(root):
        check(name, tag)


def cross_job_references(root: ET.Element) -> list[tuple[str, str]]:
    """(name, tag) of every cross-job reference in a rendered config. The G7
    gate refuses what escapes the set, discovery leaves it out of the draft:
    one scan, two consumers."""
    # A repository browser's <projectName> is a repo identifier, not a job reference.
    # The element sits INSIDE the browser element, so the whole subtree is excluded.
    inside_browser = {
        id(descendant)
        for element in root.iter()
        if _BROWSER_MARKER in element.tag.lower()
        for descendant in element.iter()
    }

    references: list[tuple[str, str]] = []
    for element in root.iter():
        if id(element) in inside_browser:
            continue
        # copyartifact's builder stores its source job in <project>, a tag too
        # generic to check globally, so it is matched by its parent element.
        if element.tag.endswith("CopyArtifact"):
            for project in element.findall("project"):
                references.append((project.text or "", "project"))
        if element.tag in SINGLE_VALUED_REFERENCES:
            references.append((element.text or "", element.tag))
        elif element.tag in LIST_REFERENCES:
            references.extend((name, element.tag) for name in (element.text or "").split(","))
    return references
