"""One function per subcommand."""

import argparse
import json
import re
import shlex
import subprocess
import sys
import tempfile
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from . import PREVIEW_ROOT
from .client import Jenkins
from .creds import FIELDS, field_fix, resolve, user_problem
from .errors import Fail, die, mark_output, say
from .fidelity import Escape, blocking_digest, canonical_identities, repo_identity
from .fidelity import blocking as fidelity_blocking
from .fidelity import missing as fidelity_missing
from .fidelity import scan as fidelity_scan
from .folders import (
    FOLDER_XML,
    VIEW_XML,
    folder_marker,
    folder_name,
    folder_path,
    job_description,
    new_preview_id,
    parse_folder_marker,
    parse_view_marker,
    swap_name,
    view_job_names,
    view_marker,
)
from .gitref import branch_of, checkout_at, local_context, resolve_ref
from .multijob import (
    assert_no_phase_collisions,
    collision_snapshot,
    repair_stripped_projects,
)
from .names import SAFE_NAME, check_name, check_repo_url, derived_folder_name, slug_text
from .render import DEFINITION_BLOCK, discover_names, render
from .sets import (
    REPO_CONFIG,
    SEARCH_ORDER,
    SETS,
    JobSet,
    _git_toplevel,
    config_file,
    parse_sets_document,
    pick_set,
)
from .transform import sanitize, validate, verify_readback

_CREATED = (200, 201)


def _quote(name: str) -> str:
    """Quote a single path segment. `safe=""` so a slash can never survive."""
    return urllib.parse.quote(name, safe="")


def _source(args: argparse.Namespace) -> tuple[str, str, str | None]:
    """(repo, ref, local_head). Inferred from the current clone when flags are absent.

    The common case is an engineer standing inside their own fork checkout, so the
    flags are optional and the checkout answers for them. local_head is returned so
    the caller can warn when the checkout has commits the remote has not seen.
    """
    if args.repo and args.ref:
        return args.repo, args.ref, None
    if args.repo or args.ref:
        die(
            "pass both --repo and --ref, or neither",
            "with neither, they are read from the git checkout you are standing in",
        )
    repo, branch, local_head = local_context()
    # The inferred URL gets the same scrutiny a typed one would.
    check_repo_url(repo)
    say(f"source {repo} @ {branch} (inferred from this checkout)")
    return repo, branch, local_head


def _say_escapes(escapes: list[Escape], *, header: bool = False) -> None:
    """Disclose build-time fetches that leave the fork (see fidelity.py).

    A warning, not a gate: every current pxb pipeline script trips it, so a
    refusal would make the tool unusable against the repo it exists for.
    """
    if header and escapes:
        noun = "fetch leaves" if len(escapes) == 1 else "fetches leave"
        say(f"WARNING {len(escapes)} build-time {noun} your fork. Changes to")
        say("        what they fetch are NOT in this preview:")
    for escape in escapes:
        location = f"{escape.script}:{escape.line}" if escape.line else escape.script
        say(f"        {location} ({escape.kind}) {escape.detail}")


# delay, json and token steer Stapler's own build-trigger request binding, so a
# declared parameter with one of these names is bound twice and fails opaquely.
_RESERVED_PARAMS = frozenset({"delay", "json", "token"})
# Secret-carrying or non-form-encodable parameter types. A value for these on
# argv lands in `ps` and shell history (the exact leak gate G10 refuses for
# credentials), and a file cannot travel in a urlencoded body at all.
_FORBIDDEN_PARAM_TYPES = ("Password", "Credentials", "File")


def _parse_params(raw: list[str]) -> dict[str, str]:
    """`-p KEY=VALUE` pairs, every malformation refused before any network I/O."""
    params: dict[str, str] = {}
    for item in raw:
        key, sep, value = item.partition("=")
        if not sep or not key:
            die(
                f"-p {item!r} is not KEY=VALUE",
                "write -p NAME=value (an empty value is fine, a missing '=' is not)",
            )
        if re.search(r"[\s\x00-\x1f]", key):
            die(
                f"-p key {key!r} carries whitespace or control characters",
                "parameter names are plain words. Check the job's declared names",
            )
        if key in _RESERVED_PARAMS:
            die(
                f"-p {key} collides with Jenkins' own trigger request binding",
                "delay, json and token steer the endpoint itself. Rename the job's "
                "parameter, or start it from the preview's UI",
            )
        if key in params:
            die(
                f"-p {key} given twice",
                "one value per key. Silently keeping the last would hide the first",
            )
        if re.search(r"[\x00-\x1f\x7f]", value):
            die(
                f"-p {key} value carries control characters",
                "a control byte forges terminal output when the value is echoed "
                "back. Set such a value from the preview's UI",
            )
        params[key] = value
    return params


def _printable(value: str) -> str:
    """The echo of a parameter value, control characters made visible so a value
    cannot forge output lines."""
    return "".join(c if c.isprintable() or c == " " else repr(c)[1:-1] for c in value)


def _declared_definitions(
    jenkins: Jenkins, path: str, job: str, *, absent_ok: bool = False
) -> dict[str, dict] | None:
    """The job's declared parameter definitions by name, or None when the job
    carries no ParametersDefinitionProperty at all. A job missing from the
    folder outright is a different failure and dies as one, so it is never
    misread as parameterless; `absent_ok` softens that for callers that only
    lose a hint."""
    info = jenkins.get_json(
        f"{path}/job/{_quote(job)}",
        tree="property[_class,parameterDefinitions[name,type,choices]]",
    )
    if not info:
        if absent_ok:
            return None
        die(
            f"{job} is not in the preview folder",
            "the folder predates the current sets file. Republish with "
            "`up --set <set> --update`, or point --sets at the file it was "
            "published from",
        )
    for prop in info.get("property", []):
        if str(prop.get("_class", "")).endswith("ParametersDefinitionProperty"):
            return {str(d.get("name")): d for d in prop.get("parameterDefinitions", [])}
    return None


def _assert_params_declared(
    stage: str, params: dict[str, str], declared: dict[str, dict[str, dict] | None]
) -> None:
    """Every `-p` key must be declared by EVERY job the stage triggers, with a
    value valid for the declared type. Jenkins silently drops an undeclared
    parameter, so this refusal replaces an invisible no-op."""
    for job, defs in declared.items():
        if defs is None:
            die(
                f"stage '{stage}' includes {job}, which declares no parameters",
                "-p is all-or-nothing per stage. Split the stage in the sets file, or "
                "start the job from the preview's UI. A Jenkinsfile parameters{} block "
                "only registers after the job's first build",
            )
        for key, value in params.items():
            if key not in defs:
                names = ", ".join(sorted(defs)) or "none"
                die(
                    f"{job} does not declare parameter '{key}' (declared: {names})",
                    "Jenkins would silently drop it. Fix the name, or start the job "
                    "from the preview's UI",
                )
            _assert_param_value(job, key, value, defs[key])


def _assert_param_value(job: str, key: str, value: str, definition: dict) -> None:
    kind = str(definition.get("type") or definition.get("_class") or "")
    if any(marker in kind for marker in _FORBIDDEN_PARAM_TYPES):
        die(
            f"{job} declares '{key}' as {kind or 'a secret-carrying type'}",
            "an argv value lands in ps and shell history, and a file cannot travel "
            "in a form body. Set it in the preview's UI instead",
        )
    if "Boolean" in kind and value.lower() not in ("true", "false"):
        die(
            f"{job} parameter '{key}' is boolean, got {value!r}",
            "Jenkins reads anything but 'true' as false, silently. Pass true or false",
        )
    choices = definition.get("choices")
    if "Choice" in kind and isinstance(choices, list) and choices and value not in choices:
        die(
            f"{job} parameter '{key}' accepts {', '.join(str(c) for c in choices)}, got {value!r}",
            "pick one of the declared choices",
        )


def doctor(jenkins: Jenkins | None, args: argparse.Namespace) -> int:
    """Preflight. Writes nothing, never prompts. Connectivity and
    authentication come first, and a credential gap is a reported finding
    like any other, with the auth-dependent checks skipped, never a refusal
    that hides the rest of the diagnosis."""
    healthy = True
    try:
        values, sources = resolve(require=False)
        file_problem = None
    except Fail as exc:
        # A malformed credentials file is a finding, never a refusal that
        # hides the rest of the diagnosis.
        values = {"url": None, "user": None, "token": None}
        sources = dict.fromkeys(values, "missing")
        file_problem = exc

    say(f"jenkins    {jenkins.url if jenkins else values['url'] or 'JENKINS_URL is not set'}")
    provided = ", ".join(
        f"{field} from {sources[field]}" for field, _ in FIELDS if sources[field] != "missing"
    )
    if provided:
        say(f"           creds: {provided}")
        origins = {
            sources[field].split(" ")[0] for field, _ in FIELDS if sources[field] != "missing"
        }
        if len(origins) > 1:
            say("           note: these fields mix the environment and credentials.yaml,")
            say("           and the environment wins per field. Unset the JENKINS_* variables")
            say("           or set JENKINS_SERVER to draw all three from one source.")
    if jenkins is None:
        missing = [(field, env) for field, env in FIELDS if values[field] is None]
        if file_problem is not None:
            say(f"  FAIL  {file_problem}")
        for field, env in missing:
            say(f"  FAIL  {env} is not set")
            say(f"        fix: {field_fix(field)}")
        if not missing and (problem := user_problem(values["user"] or "")):
            say(f"  FAIL  JENKINS_USER {problem}")
            say("        fix: use the Jenkins user ID from the top of /me/configure")
        elif not missing and file_problem is None:
            # Every field is present, so the refusal happened constructing the
            # client (a malformed URL). Reconstruct it to surface the cause.
            try:
                jenkins = Jenkins(values["url"] or "", values["user"] or "", values["token"] or "")
            except Fail as exc:
                say(f"  FAIL  {exc}")
    if jenkins is None:
        needed = (
            ", ".join(env for field, env in FIELDS if values[field] is None) or "valid credentials"
        )
        say(f"  SKIP  /me authentication check requires {needed}")
        say(f"  SKIP  /{PREVIEW_ROOT} folder check requires {needed}")
        healthy = False
    else:
        if who := jenkins.get_json("/me"):
            say(f"  PASS  authenticated as {who.get('id') or who.get('fullName')}")
        else:
            say("  FAIL  cannot read /me")
            healthy = False

        if jenkins.exists(f"/job/{PREVIEW_ROOT}"):
            say(f"  PASS  /{PREVIEW_ROOT} folder exists")
        else:
            say(f"  FAIL  /{PREVIEW_ROOT} folder does not exist")
            say(f"        fix: ask a Jenkins admin to create a top-level folder '{PREVIEW_ROOT}'")
            healthy = False

        say("           note: permissions are not probed here, because doctor never writes.")
        say("           A 403 can still appear on the first `up`.")

    try:
        job_set = pick_set(args.set if args.set is not None else _infer_set())
    except Fail as exc:
        say("sets")
        say(f"  FAIL  {exc}")
        say("")
        say("NOT READY")
        return 1
    try:
        repo, ref, _ = _source(args)
    except Fail as exc:
        say("source")
        say(f"  FAIL  {exc}")
        say("")
        say("NOT READY")
        return 1

    say(f"ref        {repo} @ {ref}")
    try:
        sha, anchor = resolve_ref(repo, ref)
    except Fail as exc:
        say(f"  FAIL  {exc}")
        say("")
        say("NOT READY")
        return 1
    say(f"  PASS  resolves to {sha} via {anchor}")

    say(f"sets       {config_file()}")
    say(f"render     set {job_set.name} ({len(job_set.jobs)} jobs)")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            work, out = Path(tmp) / "repo", Path(tmp) / "out"
            checkout_at(repo, sha, anchor, work)
            rendered = render(work, job_set, out)
        except Fail as exc:
            say(f"  FAIL  {exc}")
            healthy = False
        else:
            say(f"  PASS  all {len(rendered)} requested jobs rendered")
            escapes = fidelity_scan(
                rendered, work, repo, sha=sha, canonical=canonical_identities(rendered)
            )
            if gone := fidelity_missing(escapes):
                say(f"  FAIL  {len(gone)} pipeline scripts do not exist at this commit:")
                _say_escapes(gone)
                healthy = False
            if risky := fidelity_blocking(escapes):
                noun = "fetch leaves" if len(risky) == 1 else "fetches leave"
                say(f"  WARN  {len(risky)} build-time {noun} the fork (`up` refuses these")
                say("        without --allow-foreign-fetch):")
                _say_escapes(risky)
            if extern := [e for e in escapes if e.kind == "external"]:
                say(f"  NOTE  {len(extern)} fetches of other repos (disclosed on `up`)")
            if not escapes:
                say("  PASS  every build-time fetch stays on the fork")

    say("")
    say("READY" if healthy else "NOT READY")
    return 0 if healthy else 1


def up(jenkins: Jenkins, args: argparse.Namespace) -> int:
    """Render, sanitise, validate, then publish transactionally."""
    job_set = pick_set(args.set if args.set is not None else _infer_set())
    repo, ref, local_head = _source(args)
    named = None
    if args.name:
        slug = check_name(args.name, "preview name")
        named = check_name(folder_name(jenkins.user, slug), "preview folder name")
    return _publish_flow(
        jenkins,
        job_set=job_set,
        repo=repo,
        ref=ref,
        local_head=local_head,
        folder=named,
        root_view=args.root_view,
        update=args.update,
        dry_run=args.dry_run,
        allow_foreign_fetch=args.allow_foreign_fetch,
    )


def _publish_flow(
    jenkins: Jenkins,
    *,
    job_set: JobSet,
    repo: str,
    ref: str,
    local_head: str | None,
    folder: str | None,
    root_view: bool,
    update: bool,
    dry_run: bool,
    allow_foreign_fetch: bool,
    inherited_digest: str | None = None,
    expect_id: str | None = None,
    ack_hint: str = "acknowledge with `up ... --allow-foreign-fetch`",
) -> int:
    """The publish pipeline `up` and `sync` share.

    `folder` is the EXACT target when given (a --name publish, or sync driving
    the folder it read its marker from). Only a bare `up` derives one from the
    branch. `expect_id` makes a replacement refuse when the target changed
    identity between the caller's marker read and the teardown.
    """
    sha, anchor = resolve_ref(repo, ref)
    if local_head and local_head.lower() != sha.lower():
        say(f"NOTE   your checkout is at {local_head[:12]} but the remote branch tip is")
        say(f"       {sha[:12]}. Unpushed commits are not in this preview. Push first")
        say("       and re-run if you meant to test them.")

    # Branch-derived, not commit-derived: republishing the same branch reuses the
    # same folder name, so the iterate loop does not mint a sibling per push. The
    # derived name always fits the cap. A typed --name is refused loudly instead.
    if folder is None:
        folder = derived_folder_name(jenkins.user, job_set.name, branch_of(anchor))
    path = folder_path(folder)

    say(f"set    {job_set.name} ({len(job_set.jobs)} jobs)")
    say(f"ref    {ref} -> {sha} (anchor {anchor})")
    say(f"target {path}")
    if root_view:
        say(f"view   {folder} (root tab)")

    replace_existing = False
    if not dry_run:
        existing = jenkins.get_json(path, tree="description,jobs[name,lastBuild[building]]")
        if existing and not update:
            die(
                f"{folder} already exists",
                f"republish in place with `jenkins-preview up ... --update`, or tear it "
                f"down first: jenkins-preview down {folder}",
            )
        existing_marker: dict[str, str] = {}
        if existing and update:
            # In-flight builds refuse EARLY, before the costly clone, render
            # and sibling publish. The swap teardown would refuse anyway, but
            # only after the work, and with down's fix text, not up's.
            running = [
                job["name"]
                for job in existing.get("jobs", [])
                if ((job.get("lastBuild") or {}).get("building"))
            ]
            if running:
                die(
                    f"builds still running: {', '.join(running)}",
                    f"wait for them, or `jenkins-preview down {folder} --force` and publish fresh",
                )
            existing_marker = parse_folder_marker(existing.get("description"))
            if not existing_marker:
                die(
                    f"{folder} exists but carries no ownership marker",
                    "refusing to update a folder this tool did not create",
                )
            if existing_marker.get("owner") != jenkins.user:
                die(
                    f"{folder} belongs to {existing_marker.get('owner', '?')}, not {jenkins.user}",
                    "a preview is updated only by its owner. Publish under a different --name",
                )
            if existing_marker.get("set") not in (None, "", job_set.name):
                die(
                    f"{folder} was published from set {existing_marker.get('set')}, "
                    f"not {job_set.name}",
                    "tear it down first, or publish under a different --name",
                )
            replace_existing = True
            say(f"update {folder} (replaced only after the replacement publishes fully)")
        if root_view and (view_text := jenkins.view_description(folder)) is not None:
            # An update's own paired tab is expected here and is replaced by the
            # teardown. Anything else is somebody's view and is never adopted.
            view_id = parse_view_marker(view_text).get("id")
            paired = replace_existing and view_id and view_id == existing_marker.get("id")
            if not paired:
                die(
                    f"a root view named {folder} already exists",
                    "delete it first, or publish with a different --name. This tool never "
                    "adopts a view it did not create",
                )

    # Same-named copies are safe to CREATE, but tearing them down later fires
    # the multijob plugin's folder-blind delete listener against production
    # (multijob.py). Refused here, before anything is written, dry-run included,
    # so a --dry-run rehearsal reports the collision too.
    assert_no_phase_collisions(jenkins, job_set.jobs)

    description = job_description(
        job_set=job_set.name, sha=sha, repo=repo, branch=branch_of(anchor)
    )

    with tempfile.TemporaryDirectory() as tmp:
        work, out = Path(tmp) / "repo", Path(tmp) / "out"
        checkout_at(repo, sha, anchor, work)
        rendered = render(work, job_set, out)

        # Fidelity runs on the RAW rendered configs: after sanitising, the SCM
        # remotes already read as the fork and the canonical identity is gone.
        escapes = fidelity_scan(
            rendered, work, repo, sha=sha, canonical=canonical_identities(rendered)
        )
        if gone := fidelity_missing(escapes):
            _say_escapes(gone)
            die(
                f"{len(gone)} pipeline scripts do not exist at the pinned commit",
                "the build would die at script load. Fix the scriptPath or the branch, "
                "then republish",
            )
        foreign = [escape for escape in escapes if escape.kind != "missing"]
        _say_escapes(foreign, header=True)
        risky = fidelity_blocking(escapes)
        digest = blocking_digest(escapes)
        acknowledged = allow_foreign_fetch
        if risky and not acknowledged and inherited_digest:
            if digest == inherited_digest:
                # The operator saw and acknowledged exactly these escapes at
                # publish time. Say so rather than inheriting silently.
                acknowledged = True
                noun = "fetch" if len(risky) == 1 else "fetches"
                say(
                    f"ack    inheriting the acknowledgment stamped at publish "
                    f"({len(risky)} blocking {noun}, unchanged)"
                )
            else:
                die(
                    "the new tip changes the blocking build-time fetches, so the "
                    "stamped acknowledgment no longer covers them",
                    ack_hint,
                )
        if risky and not acknowledged:
            die(
                f"{len(risky)} build-time fetches would silently test code outside your fork",
                f"point git fetches at your fork (`checkout scm`), add sibling jobs to the "
                f"set, or {ack_hint}",
            )

        configs = {
            job: sanitize(xml, fork_url=repo, sha=sha, description=description)
            for job, xml in rendered.items()
        }
        validate(configs, fork_url=repo, sha=sha, job_set=job_set)
        say(f"gates  all pre-upload gates passed for {len(configs)} jobs")

        if dry_run:
            first = sorted(configs)[0]
            say("")
            say("DRY RUN, nothing was created. First rendered config:")
            say(f"--- {first} ---")
            say(configs[first][:1500])
            return 0

        if not replace_existing:
            _publish(
                jenkins,
                folder=folder,
                configs=configs,
                job_set=job_set,
                repo=repo,
                sha=sha,
                anchor=anchor,
                root_view=root_view,
                foreign=len(foreign),
                ref=ref,
                blocking=len(risky),
                ackdigest=digest if (acknowledged and risky) else "",
            )
        else:
            # Swap protocol: the replacement publishes FULLY under a sibling
            # name, only then does the old preview go, and a rename slots the
            # replacement into place. No failure on this path leaves zero
            # previews: an early failure keeps the old one, a late failure
            # keeps the fully working replacement under the sibling name.
            temp = swap_name(folder)
            say(f"swap   publishing the replacement as {temp} first")
            preview_id = _publish(
                jenkins,
                folder=temp,
                configs=configs,
                job_set=job_set,
                repo=repo,
                sha=sha,
                anchor=anchor,
                root_view=False,
                foreign=len(foreign),
                ref=ref,
                blocking=len(risky),
                ackdigest=digest if (acknowledged and risky) else "",
                view_name=folder if root_view else "none",
            )
            # The id compared inside _teardown's own marker read: sync passes the
            # id it read, a plain --update passes the one from the pre-checks, so
            # a folder replaced mid-flight by a concurrent publish never gets
            # deleted by this one.
            old_id = expect_id or existing_marker.get("id")
            try:
                _teardown(jenkins, folder, force=False, expect_id=old_id)
            except BaseException as teardown_error:
                # A delete can land while its response is lost. Probe before
                # rolling back: an old folder that is really gone means the
                # teardown DID complete, and removing the replacement now would
                # leave zero previews, the exact outcome the swap exists to
                # prevent. The probe itself can die on the same outage; then
                # nothing is rolled back, and the error must name both folders.
                try:
                    old_alive = jenkins.exists(path)
                except Exception:
                    say("NOTE   the teardown failed and so did the probe after it. Nothing was")
                    say(f"       rolled back: the replacement is complete at {temp}, and")
                    say(f"       {folder} may still be standing. Check `jenkins-preview list`")
                    raise teardown_error from None
                if old_alive:
                    _rollback(jenkins, folder=temp, preview_id=preview_id, view_possible=False)
                    raise
                say("NOTE   the old preview is gone although its teardown reported an error.")
                say("       Continuing the swap, the replacement is intact")
            _rename_folder(jenkins, temp, folder)
            if root_view:
                _create_replacement_view(
                    jenkins, folder=folder, configs=configs, preview_id=preview_id
                )

    say("")
    say(f"published {jenkins.url}{path}/")
    if root_view:
        say(f"tab       {jenkins.url}/view/{_quote(folder)}/")
    say(f"pinned    {sha}")
    if foreign:
        say(f"foreign   {len(foreign)} build-time fetches outside your fork (WARNING above)")
    say(f"run       jenkins-preview run {folder}")
    say(f"teardown  jenkins-preview down {folder}")
    return 0


def _publish(
    jenkins: Jenkins,
    *,
    folder: str,
    configs: dict[str, str],
    job_set: JobSet,
    repo: str,
    sha: str,
    anchor: str,
    root_view: bool,
    foreign: int = 0,
    ref: str = "",
    blocking: int = 0,
    ackdigest: str = "",
    view_name: str | None = None,
) -> str:
    """Gate G8. Create disabled, verify, enable, then the tab last.

    The view is created last so no global tab ever advertises a partial preview, and
    rollback removes the view before the folder for the same reason. `view_name`
    overrides what the marker records as the paired tab, for a swap publish whose
    tab arrives only after the rename. Returns the preview id.
    """
    path = folder_path(folder)
    preview_id = new_preview_id()

    try:
        marker = folder_marker(
            preview_id=preview_id,
            user=jenkins.user,
            job_set=job_set.name,
            repo=repo,
            sha=sha,
            anchor=anchor,
            view=view_name if view_name is not None else (folder if root_view else "none"),
            foreign=foreign,
            ref=ref,
            blocking=blocking,
            ackdigest=ackdigest,
        )
        status, _ = jenkins.post(
            f"/job/{PREVIEW_ROOT}/createItem?name={_quote(folder)}",
            FOLDER_XML.format(description=marker).encode(),
            "application/xml",
        )
        if status == 400:
            # The pre-publish existence check passed, so the name appeared in
            # the gap: almost always a concurrent publish from another terminal.
            die(
                f"{folder} was created concurrently by another publish",
                "wait for it to finish, then --update if it is yours, or pick another --name",
            )
        jenkins.expect(status, path, _CREATED)
        say(f"create {folder}")

        for job, xml in configs.items():
            status, _ = jenkins.post(
                f"{path}/createItem?name={_quote(job)}", xml.encode(), "application/xml"
            )
            jenkins.expect(status, f"{path}/{job}", _CREATED)

        for job in configs:
            code, live = jenkins.get_text(f"{path}/job/{_quote(job)}/config.xml")
            if code != 200:
                die(f"cannot read back {job} (HTTP {code})", "nothing was left published")
            verify_readback(job, live, fork_url=repo, sha=sha)
        say(f"verify read-back clean for {len(configs)} jobs")

        for job in configs:
            status, _ = jenkins.post(f"{path}/job/{_quote(job)}/enable")
            jenkins.expect(status, f"{path}/{job}/enable", (*_CREATED, 302))
        # A 302 from enable is indistinguishable from an auth-proxy redirect, so the
        # result is read back rather than trusted: buildable means enabled.
        for job in configs:
            info = jenkins.get_json(f"{path}/job/{_quote(job)}", tree="buildable")
            if info.get("buildable") is not True:
                die(
                    f"{job} is still disabled after enabling (gate G8)",
                    "nothing was left published",
                )
        say(f"enable {len(configs)} jobs, verified buildable")

        if root_view:
            _create_view_and_verify(jenkins, folder=folder, configs=configs, preview_id=preview_id)

    except BaseException:
        # BaseException on purpose: Ctrl-C mid-publish must still roll back.
        _rollback(jenkins, folder=folder, preview_id=preview_id, view_possible=root_view)
        raise
    return preview_id


def _create_view_and_verify(
    jenkins: Jenkins, *, folder: str, configs: dict[str, str], preview_id: str
) -> None:
    """The root tab, created and proven to list exactly the preview's jobs."""
    xml = VIEW_XML.format(
        name=folder,
        description=view_marker(preview_id=preview_id, folder=folder, user=jenkins.user),
        job_names=view_job_names(folder, list(configs)),
    )
    status, _ = jenkins.create_view(folder, xml)
    jenkins.expect(status, f"/view/{folder}", (*_CREATED, 302))
    listed = jenkins.get_json(f"/view/{_quote(folder)}", tree="jobs[name,url]")
    shown = {job["name"] for job in listed.get("jobs", [])}
    marker_prefix = f"/job/{PREVIEW_ROOT}/job/{folder}/"
    stray = [job["url"] for job in listed.get("jobs", []) if marker_prefix not in job["url"]]
    if shown != set(configs) or stray:
        die(
            f"the root view resolves {sorted(shown)} (stray: {stray}), expected "
            f"exactly the {len(configs)} preview jobs",
            "the view does not show what was published",
        )
    say(f"tab    {folder} shows {len(shown)} jobs")


def _rename_folder(jenkins: Jenkins, temp: str, folder: str) -> None:
    """Slot the fully published replacement into the final name.

    A failure here never rolls the replacement back: it is the only copy of a
    working preview, so the refusal names where it lives instead.
    """
    status, _ = jenkins.post(f"{folder_path(temp)}/confirmRename?newName={_quote(folder)}")
    renamed = (
        status in (200, 302)
        and jenkins.exists(folder_path(folder))
        and not jenkins.exists(folder_path(temp))
    )
    if not renamed:
        die(
            f"renaming {temp} to {folder} did not complete (HTTP {status})",
            f"the replacement is fully working at {temp}. Rename it in the UI, or "
            f"`jenkins-preview down {temp}` and republish",
        )
    say(f"rename {temp} -> {folder}")


def _create_replacement_view(
    jenkins: Jenkins, *, folder: str, configs: dict[str, str], preview_id: str
) -> None:
    """The swapped-in preview's tab. A failure keeps the folder: it is a
    complete, verified preview, and only the tab is missing."""
    try:
        _create_view_and_verify(jenkins, folder=folder, configs=configs, preview_id=preview_id)
    except BaseException:
        try:
            text = jenkins.view_description(folder)
            if text is not None and parse_view_marker(text).get("id") == preview_id:
                jenkins.delete_view(folder)
        except Fail:
            # Best-effort removal of a half-made tab. The original error below
            # is the one that matters and must not be masked by this cleanup.
            pass
        say(f"NOTE   the preview at {folder} is published and working, only its tab failed")
        raise


def _rollback(jenkins: Jenkins, *, folder: str, preview_id: str, view_possible: bool) -> None:
    """Undo a partial publish, deleting ONLY what carries this run's preview id.

    Rollback is not gated on created-flags: a create whose response was lost still
    needs undoing, and a create that returned 409 belongs to someone else and must
    not be touched. The marker id distinguishes the two cases, so it is the marker
    id that authorises each delete.
    """
    if view_possible:
        try:
            view_meta = parse_view_marker(jenkins.view_description(folder))
            if view_meta.get("id") == preview_id:
                jenkins.delete_view(folder)
                if jenkins.view_description(folder) is not None:
                    say(f"WARNING could not remove the root view {folder}. Delete it by hand")
                else:
                    say(f"rolled back the root view {folder}")
            elif view_meta:
                say(f"NOTE   a root view {folder} exists but is not this run's. Leaving it")
        except Fail as exc:
            say(f"WARNING rollback of view {folder} failed: {exc.cause}")

    try:
        data = jenkins.get_json(folder_path(folder), tree="description")
        meta = parse_folder_marker(data.get("description")) if data else {}
        if meta.get("id") == preview_id:
            jenkins.post(f"{folder_path(folder)}/doDelete")
            if jenkins.exists(folder_path(folder)):
                say(
                    f"WARNING could not remove the partial folder {folder}. It will block "
                    f"republishing under the same name. Delete it by hand"
                )
            else:
                say(f"rolled back the folder {folder}")
        elif data:
            say(f"NOTE   {folder} exists but is not this run's preview. Leaving it")
    except Fail as exc:
        say(f"WARNING rollback of folder {folder} failed: {exc.cause}")


def status(jenkins: Jenkins, args: argparse.Namespace) -> int:
    folder = check_name(
        args.folder if args.folder is not None else _infer_folder(jenkins),
        "preview folder name",
    )
    path = folder_path(folder)
    data = jenkins.get_json(
        path, tree="description,jobs[name,color,lastBuild[number,result,building]]"
    )
    if not data:
        die(f"{folder} not found under /{PREVIEW_ROOT}", "check `jenkins-preview list`")

    meta = parse_folder_marker(data.get("description"))
    say(f"folder {folder}")
    say(f"set    {meta.get('set', '?')}")
    say(f"pinned {meta.get('sha', '?')}")
    say(f"repo   {meta.get('repo', '?')} (anchor {meta.get('anchor', '?')})")
    if (view := meta.get("view")) and view != "none":
        say(f"tab    {jenkins.url}/view/{_quote(view)}/")
    if (foreign := meta.get("foreign")) and foreign != "0":
        say(f"NOTE   {foreign} build-time fetches leave the fork, so that code is")
        say("       production's, not this preview's. `up` listed them at publish time.")

    if (sha := meta.get("sha")) and (anchor := meta.get("anchor")) and (repo := meta.get("repo")):
        try:
            tip, _ = resolve_ref(repo, branch_of(anchor))
        except Fail:
            say("NOTE   could not compare against the branch tip")
        else:
            if tip == sha:
                say("branch tip matches the pin")
            else:
                say(
                    f"NOTE   the branch has moved: tip is now {tip[:12]}, this preview stays "
                    f"pinned at {sha[:12]}"
                )
                marker_ref = meta.get("ref", "")
                syncable = (
                    anchor.startswith("refs/heads/")
                    and marker_ref
                    and not re.fullmatch(r"[0-9a-fA-F]{40}", marker_ref)
                )
                if syncable:
                    say(f"       republish: jenkins-preview sync {folder}")
                else:
                    # sync would refuse this preview (exact pin, tag anchor, or a
                    # pre-0.6.0 marker), so the hint must not point at it.
                    say(
                        f"       republish: jenkins-preview up --set {meta.get('set', '?')} "
                        "--update (same flags as the original publish)"
                    )

    say("")
    say(f"{'job':<48} {'color':<12} last build")
    for job in data.get("jobs", []):
        last = job.get("lastBuild") or {}
        result = "running" if last.get("building") else (last.get("result") or "-")
        number = f"build {last['number']}" if last.get("number") else "-"
        say(f"{job['name']:<48} {job.get('color', '-'):<12} {number} {result}")
    return 0


def _green_stages(jenkins: Jenkins, path: str, job_set: JobSet) -> set[str]:
    """The stages whose every job has a successful build."""
    green: set[str] = set()
    for stage in job_set.stage_order:
        jobs_green = True
        for job in job_set.stage(stage):
            info = jenkins.get_json(f"{path}/job/{_quote(job)}", tree="lastSuccessfulBuild[number]")
            if not (info.get("lastSuccessfulBuild") or {}).get("number"):
                jobs_green = False
                break
        if jobs_green:
            green.add(stage)
    return green


def run(jenkins: Jenkins, args: argparse.Namespace) -> int:
    """Trigger the next stage, or a named one. One stage per invocation.

    Several stages in one command cannot honour producer-to-consumer ordering
    without blocking for the length of a build, and a CLI that silently queues a
    consumer with no artifact reproduces the exact 240-minute hang this tool
    exists to avoid. So `run` is one verb: it works out which stage is next.
    """
    params = _parse_params(args.param)
    if params and not args.stage:
        die(
            "-p needs an explicit --stage",
            "parameters aim at one named stage: run <folder> --stage <s> -p KEY=VALUE",
        )
    folder = check_name(
        args.folder if args.folder is not None else _infer_folder(jenkins),
        "preview folder name",
    )
    path = folder_path(folder)
    data = jenkins.get_json(path, tree="description,jobs[name]")
    if not data:
        die(f"{folder} not found", "check `jenkins-preview list`")

    meta = parse_folder_marker(data.get("description"))
    set_name = meta.get("set", "")
    job_set = SETS.get(set_name)
    if job_set is None:
        if set_name:
            # The preview is intact. Only THIS invocation cannot see its set.
            # The old advice here said "tear it down", which destroys a healthy
            # preview over a local configuration gap.
            die(
                f"set '{set_name}' is not in your sets file ({config_file() or 'none found'})",
                f"point --sets at the file defining '{set_name}'. The preview itself is fine",
            )
        die(
            f"cannot tell which set {folder} came from",
            "the folder description lost its marker. Tear it down and republish",
        )

    if args.stage:
        stage = args.stage
        job_set.stage(stage)
        _assert_producer_ran(jenkins, path, job_set, stage, folder)
    else:
        green = _green_stages(jenkins, path, job_set)
        stage = job_set.pick_stage(green)
        if stage is None:
            say(f"all stages are green at {meta.get('sha', '?')[:12]}")
            say("next   merge or update your pull request. Production jobs are")
            say(f"       untouched by this tool. Then: jenkins-preview down {folder}")
            return 0
        say(f"stage  {stage} (picked automatically, green so far: {sorted(green) or 'none'})")

    to_start = job_set.stage(stage)
    if args.stage:
        say(f"stage  {stage}")
    already = [
        job
        for job in to_start
        if (
            (jenkins.get_json(f"{path}/job/{_quote(job)}", tree="lastBuild[building]") or {}).get(
                "lastBuild"
            )
            or {}
        ).get("building")
    ]
    if already:
        die(
            f"stage '{stage}' is already building: {', '.join(already)}",
            f"one build per stage at a time, or every re-run doubles the cloud "
            f"workers. Watch it: jenkins-preview status {folder}",
        )
    declared: dict[str, dict[str, dict] | None] = {}
    if params:
        for job in to_start:
            declared[job] = _declared_definitions(jenkins, path, job)
        _assert_params_declared(stage, params, declared)
    say(f"builds {len(to_start)}: {', '.join(to_start)}")
    if params:
        for key, value in params.items():
            say(f"param  {key}={_printable(value)}")
        left = sorted({name for defs in declared.values() if defs for name in defs} - set(params))
        if left:
            say(f"default {len(left)} other declared parameters keep their defaults")
    say(
        f"cost   each build provisions its own worker, plus a shared launcher. "
        f"Expect up to {len(to_start) + 1} machines."
    )
    try:
        confirmed = args.yes or input("proceed? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        confirmed = False
    if not confirmed:
        say("aborted, nothing triggered")
        return 0

    body = urllib.parse.urlencode(params).encode() if params else b""
    if params:
        # Definitions can change while the prompt sits open. Re-read and
        # re-validate EVERY stage job before the first trigger, so a stage is
        # never part-queued against stale knowledge.
        for job in to_start:
            fresh = _declared_definitions(jenkins, path, job)
            if fresh is None:
                die(
                    f"{job} lost its parameters between the check and the trigger",
                    "the job was edited concurrently. Re-run, definitions are re-read every time",
                )
            declared[job] = fresh
        _assert_params_declared(stage, params, declared)
    for job in to_start:
        # Each endpoint refuses the other kind with HTTP 400: /build answers a
        # parameterized job with "Nothing is submitted", /buildWithParameters
        # answers a parameterless one the same way. Ask the job which it is.
        # With -p, the post-prompt sweep above just proved every job
        # parameterized, so its answer stands.
        if params:
            parameterized = True
        else:
            info = jenkins.get_json(
                f"{path}/job/{_quote(job)}", tree="property[_class,parameterDefinitions[name]]"
            )
            # Jenkins counts a job as parameterized when the property exists,
            # even with an empty definition list, so presence decides.
            parameterized = any(
                str(p.get("_class", "")).endswith("ParametersDefinitionProperty")
                for p in info.get("property", [])
            )
        endpoint = "buildWithParameters" if parameterized else "build"
        content_type = "application/x-www-form-urlencoded; charset=UTF-8" if params else None
        status_code, _ = jenkins.post(
            f"{path}/job/{_quote(job)}/{endpoint}",
            body,
            content_type,
        )
        if status_code == 400:
            die(
                f"{job} refused the {endpoint} trigger with HTTP 400",
                "most likely the job's parameter definitions changed between the "
                "check and the trigger. Re-run, the endpoint is chosen fresh every time",
            )
        jenkins.expect(status_code, f"{path}/job/{job}/{endpoint}", (*_CREATED, 302))
        say(f"queued {job}")

    say("")
    say(f"watch  jenkins-preview status {folder}")
    if (nxt := job_set.next_stage(stage)) is not None:
        # Carry the parameters forward only when the next stage's jobs declare
        # every one of them, or the printed command would refuse when pasted.
        carried = ""
        if params:
            next_defs = [
                _declared_definitions(jenkins, path, job, absent_ok=True)
                for job in job_set.stage(nxt)
            ]
            if all(defs is not None and all(key in defs for key in params) for defs in next_defs):
                carried = "".join(
                    f" -p {shlex.quote(f'{key}={value}')}" for key, value in params.items()
                )
        say(
            f"then   jenkins-preview run {folder} --stage {nxt}{carried}   "
            "(once this stage is green)"
        )
    return 0


def _assert_producer_ran(
    jenkins: Jenkins, path: str, job_set: JobSet, stage: str, folder: str
) -> None:
    """Refuse a consumer stage whose producer never succeeded."""
    required = job_set.consumes.get(stage)
    if not required:
        return
    for producer in job_set.stage(required):
        info = jenkins.get_json(
            f"{path}/job/{_quote(producer)}", tree="lastSuccessfulBuild[number]"
        )
        if not (info.get("lastSuccessfulBuild") or {}).get("number"):
            die(
                f"stage '{stage}' consumes '{required}', which has no successful build",
                f"run the producer first: jenkins-preview run {folder} --stage {required}. "
                "Starting mid-stage makes the consumer wait for an artifact that does not "
                "exist, burning a 240-minute timeout",
            )


def sync(jenkins: Jenkins, args: argparse.Namespace) -> int:
    """Republish a preview at its anchor branch's current tip.

    Everything is read back from the marker the preview carries: the exact
    folder (a --name publish keeps its name), the repo, the branch, whether a
    tab exists, and what blocking fetches were acknowledged. Only a branch
    anchor can move, and an unchanged acknowledged blocking set is inherited,
    with a line saying so.
    """
    folder = check_name(
        args.folder if args.folder is not None else _infer_folder(jenkins),
        "preview folder name",
    )
    path = folder_path(folder)
    data = jenkins.get_json(path, tree="description,jobs[name,lastBuild[building]]")
    if not data:
        die(f"{folder} not found", "check `jenkins-preview list`")
    meta = parse_folder_marker(data.get("description"))
    if not meta:
        die(
            f"{folder} carries no ownership marker",
            "this tool syncs only previews it created",
        )
    if meta.get("owner") != jenkins.user:
        die(
            f"{folder} belongs to owner '{meta.get('owner', '?')}'",
            "a preview is synced only by its owner",
        )
    set_name = meta.get("set", "")
    job_set = SETS.get(set_name)
    if job_set is None:
        die(
            f"set '{set_name}' is not in your sets file ({config_file() or 'none found'})",
            f"point --sets at the file defining '{set_name}'. The preview itself is fine",
        )
    anchor = meta.get("anchor", "")
    if not anchor.startswith("refs/heads/"):
        die(
            f"{folder} is anchored at {anchor or '?'}, not a branch",
            "only a branch-anchored preview can follow its tip. Move it: up ... --update",
        )
    as_typed = meta.get("ref", "")
    if not as_typed:
        # A pre-0.6.0 marker: without the as-typed ref there is no telling a
        # deliberate exact-sha pin from a branch to follow. Refuse rather than guess.
        die(
            f"{folder} was published before 0.6.0 and its marker records no ref",
            "republish it once (up --set <set> --update, same --repo/--ref/--name "
            "flags as the original publish), sync works from then on",
        )
    if re.fullmatch(r"[0-9a-fA-F]{40}", as_typed):
        die(
            f"{folder} was pinned to the exact commit {as_typed[:12]}",
            "a deliberate pin never moves on its own. Repeat the original up "
            "command with the new --ref and add --update",
        )
    # In-flight builds refuse EARLY, before the costly clone and render.
    running = [
        job["name"]
        for job in data.get("jobs", [])
        if ((job.get("lastBuild") or {}).get("building"))
    ]
    if running:
        die(
            f"builds still running: {', '.join(running)}",
            f"wait for them, or `jenkins-preview down {folder} --force` and republish",
        )
    repo = meta.get("repo", "")
    check_repo_url(repo)
    branch = branch_of(anchor)
    sha, _ = resolve_ref(repo, branch)
    if sha.lower() == meta.get("sha", "").lower():
        say(f"already at the branch tip {sha[:12]}, nothing to sync")
        say(
            f"       set or job-list changes go via: jenkins-preview up --set {set_name} "
            "--update (same flags as the original publish)"
        )
        return 0
    say(f"sync   {folder}: {meta.get('sha', '?')[:12]} -> {sha[:12]} on {branch}")
    return _publish_flow(
        jenkins,
        job_set=job_set,
        repo=repo,
        ref=branch,
        local_head=None,
        folder=folder,
        root_view=meta.get("view", "none") != "none",
        update=True,
        dry_run=False,
        allow_foreign_fetch=args.allow_foreign_fetch,
        inherited_digest=meta.get("ackdigest") or None,
        expect_id=meta.get("id"),
        ack_hint=f"acknowledge the new set: jenkins-preview sync {folder} --allow-foreign-fetch",
    )


def _teardown(
    jenkins: Jenkins,
    folder: str,
    *,
    force: bool,
    expect_owner: str | None = None,
    expect_id: str | None = None,
) -> None:
    """The one teardown path, shared by `down`, `reap` and the update swap.

    View first, then folder: the reverse order can leave a prominent empty root tab.
    `down` passes expect_owner so one user cannot delete another's preview by
    name. `reap` does not: age-based cleanup is the operator sweep. The swap
    passes expect_id, compared against THIS read's marker, so a folder replaced
    by a concurrent publish is never the one deleted.
    """
    path = folder_path(folder)
    data = jenkins.get_json(path, tree="description,jobs[name,lastBuild[building]]")
    if not data:
        # The folder can be gone while its tab survives (a hand deletion in the
        # UI). The only safe escape is a view carrying this tool's own marker
        # naming exactly this folder. Anything else stays untouched.
        view_meta = parse_view_marker(jenkins.view_description(folder))
        if view_meta.get("folder") == f"{PREVIEW_ROOT}/{folder}":
            jenkins.delete_view(folder)
            if jenkins.view_description(folder) is not None:
                die(f"could not delete the orphaned root view {folder}", "delete it by hand")
            say(f"deleted view {folder} (orphaned, its folder was already gone)")
            return
        die(f"{folder} not found", "check `jenkins-preview list`")

    meta = parse_folder_marker(data.get("description"))
    if not meta:
        die(
            f"{folder} carries no ownership marker",
            "this tool only deletes folders it created. Remove it by hand if you are sure",
        )
    if expect_owner is not None and meta.get("owner") != expect_owner:
        die(
            f"{folder} belongs to owner '{meta.get('owner', '?')}'",
            "only its owner deletes a preview through `down`. Age-based cleanup "
            "goes through `reap`, or ask them to run down themselves",
        )
    if expect_id is not None and meta.get("id") != expect_id:
        die(
            f"{folder} changed identity since its marker was read",
            "another publish replaced it mid-flight. Re-run",
        )

    running = [
        job["name"] for job in data.get("jobs", []) if (job.get("lastBuild") or {}).get("building")
    ]
    if running and not force:
        die(
            f"builds still running: {', '.join(running)}",
            "wait for them, or pass --force to delete anyway",
        )

    view = meta.get("view")
    if view and view != "none":
        folder_id = meta.get("id") or ""
        view_description = jenkins.view_description(view)
        view_meta = parse_view_marker(view_description)
        if view_description is None:
            say(f"NOTE   root view {view} is already gone")
        elif not view_meta:
            # Refuse rather than guess: the folder points at a view this tool cannot prove it
            # owns. Deleting the folder anyway would orphan an unowned tab.
            die(
                f"root view {view} exists but carries no ownership marker",
                "remove the view by hand, then re-run down",
            )
        elif (paired := view_meta.get("folder", "")) != f"{PREVIEW_ROOT}/{folder}" and (
            paired.startswith(f"{PREVIEW_ROOT}/")
            and jenkins.exists(folder_path(paired.removeprefix(f"{PREVIEW_ROOT}/")))
        ):
            # A swap that crashed between teardown and rename leaves a temp
            # folder whose marker names the final view, one it never owned. Once
            # the final preview is republished, that view belongs to the new
            # publish, and dying here would wedge the temp beyond down AND reap.
            say(f"NOTE   root view {view} is paired with a live preview, leaving it alone")
        elif not folder_id or view_meta.get("id") != folder_id:
            die(
                f"root view {view} belongs to a different preview (marker id mismatch)",
                "remove the view by hand if it is stale, then re-run down",
            )
        elif view_meta.get("folder") != f"{PREVIEW_ROOT}/{folder}":
            die(
                f"root view {view} is not linked to {folder}",
                "remove the view by hand if it is stale, then re-run down",
            )
        else:
            jenkins.delete_view(view)
            if jenkins.view_description(view) is not None:
                die(f"could not delete the root view {view}", "delete it by hand, then retry")
            say(f"deleted view {view}")

    # Previews published before the up-time collision gate can still carry
    # copies whose deletion strips live multijob phases (multijob.py). Bracket
    # the delete: snapshot the projects at risk, then restore any the listener
    # rewrote.
    snapshot = collision_snapshot(jenkins, [job["name"] for job in data.get("jobs", [])])
    jenkins.post(f"{path}/doDelete")
    if jenkins.exists(path):
        die(f"{folder} still exists after delete", "check permissions and retry")
    say(f"deleted folder {folder}")
    repair_stripped_projects(jenkins, snapshot)


def down(jenkins: Jenkins, args: argparse.Namespace) -> int:
    folder = args.folder
    if folder is None:
        folder = _infer_folder(jenkins)
        # A deletion of a name the user never typed gets one confirmation. The
        # marker triple (owner, repo, anchor) narrows hard, but it is data
        # anyone with Configure under /previews can write.
        mark_output()
        try:
            confirmed = args.yes or input(f"delete {folder}? [y/N] ").strip().lower() in (
                "y",
                "yes",
            )
        except EOFError:
            confirmed = False
        if not confirmed:
            say("aborted, nothing deleted")
            return 0
    _teardown(
        jenkins,
        check_name(folder, "preview folder name"),
        force=args.force,
        expect_owner=jenkins.user,
    )
    say("clean")
    return 0


def preview_list(jenkins: Jenkins, args: argparse.Namespace) -> int:  # noqa: ARG001
    data = jenkins.get_json(f"/job/{PREVIEW_ROOT}", tree="jobs[name,description]")
    if not data:
        die(f"/{PREVIEW_ROOT} not found", "ask a Jenkins admin to create it")

    rows = []
    for job in data.get("jobs", []):
        meta = parse_folder_marker(job.get("description"))
        rows.append(
            (
                job["name"],
                meta.get("owner", "?"),
                meta.get("set", "?"),
                (meta.get("created") or "?")[:19],
                meta.get("sha", "?")[:12],
                "yes" if meta.get("view", "none") != "none" else "no",
            )
        )
    if not rows:
        say("no previews")
        return 0

    say(f"{'folder':<44} {'owner':<18} {'set':<10} {'created':<20} {'sha':<14} tab")
    for name, owner, job_set, created, sha, tab in sorted(rows):
        say(f"{name:<44} {owner:<18} {job_set:<10} {created:<20} {sha:<14} {tab}")
    return 0


def reap(jenkins: Jenkins, args: argparse.Namespace) -> int:
    """Delete previews older than a threshold, through the shared teardown path."""
    if args.older_than < 0:
        die(
            f"--older-than must be zero or more days, got {args.older_than}",
            "a negative age would reap every marked preview on the controller",
        )
    data = jenkins.get_json(f"/job/{PREVIEW_ROOT}", tree="jobs[name,description]")
    now = datetime.now(UTC)
    reaped = 0

    for job in data.get("jobs", []):
        name = job["name"]
        meta = parse_folder_marker(job.get("description"))
        if not meta.get("created"):
            say(f"skip   {name} (no ownership marker, not ours)")
            continue
        try:
            created = datetime.fromisoformat(meta["created"])
        except ValueError:
            say(f"skip   {name} (unparseable creation stamp)")
            continue
        if created.tzinfo is None:
            # A hand-edited naive stamp would raise on subtraction mid-loop.
            say(f"skip   {name} (creation stamp has no timezone)")
            continue
        age_days = (now - created).days
        if age_days < args.older_than:
            say(f"keep   {name} ({age_days}d old)")
            continue
        if args.dry_run:
            say(f"would reap {name} ({age_days}d old)")
            continue
        try:
            _teardown(jenkins, name, force=args.force)
        except Fail as exc:
            say(f"WARNING could not reap {name}: {exc.cause}")
            continue
        reaped += 1

    say(f"{reaped} reaped")
    return 0


def discover_document(checkout: Path, yaml_dir: str) -> str:
    """A valid, deliberately minimal sets file drafted from what a directory
    renders. The developer curates stages, order and consumes afterwards.

    Grouping is not inferred (that is the graph-inference tarpit the design
    rejects). Every rendered job lands in `jobs`, and the pipeline jobs land in
    one `main` stage as the starting point to split.
    """
    yaml_dir = yaml_dir.rstrip("/") or yaml_dir  # tab completion appends a slash
    segments = yaml_dir.split("/")
    if yaml_dir.startswith("/") or "\\" in yaml_dir or ".." in segments or "" in segments:
        die(
            f"yaml dir {yaml_dir!r} must be a plain path relative to the checkout root",
            "no leading slash, no backslash, no '..' and no empty segments",
        )
    names, skipped, unpublishable = discover_names(checkout / yaml_dir)
    pipelines = sorted(name for name, root in names.items() if root == "flow-definition")
    stage_jobs = pipelines or [next(iter(names))]
    slug = slug_text("-".join(segments))
    set_name = slug if SAFE_NAME.fullmatch(slug) else "my-set"
    document = {
        "sets": {
            set_name: {
                "yaml_dir": yaml_dir,
                "jobs": sorted(names),
                "stages": {"main": stage_jobs},
                "stage_order": ["main"],
            }
        }
    }
    # The draft must survive this tool's own loader before a user ever edits it.
    parse_sets_document(document, "the discovered draft")
    kinds = ", ".join(
        f"{root.split('.')[-1]}: {sum(1 for r in names.values() if r == root)}"
        for root in sorted(set(names.values()))
    )
    guidance = (
        f"discovered {len(names)} jobs in {yaml_dir} ({kinds}). Every job is\n"
        f"published. Only stage jobs are triggered by `run`: split `main` into\n"
        f"real stages, order them in stage_order, and wire consumes. Then save\n"
        f"as {REPO_CONFIG} at the checkout root and commit it"
    )
    if skipped:
        reasons = "".join(f"\n    {name}: {reason}" for name, reason in skipped)
        guidance += f"\nskipped {len(skipped)} files that do not render:{reasons}"
    if unpublishable:
        guidance += f"\nleft out, this tool cannot publish them: {', '.join(unpublishable)}"
    print(guidance, file=sys.stderr)
    return json.dumps(document, indent=2)


def _detect_yaml_dirs(top: Path) -> list[str]:
    """Repo-relative directories holding JJB job definitions, for --example.

    Deliberately lenient: unreadable entries are someone else's problem, the
    chosen directory gets the strict treatment in discover_names.
    """
    found: set[str] = set()
    for path in sorted(top.rglob("*.y*ml")):
        rel = path.relative_to(top)
        if any(part.startswith(".") for part in rel.parts) or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        if DEFINITION_BLOCK.search(text):
            found.add(rel.parent.as_posix())
    return sorted(found)


def _branch_touched(top: Path, candidates: list[str]) -> list[str]:
    """The candidates this branch edits files in. Pipeline scripts live next to
    the YAML that pins them, so any changed file counts, not just YAML. Best
    effort: no upstream base to diff against means no narrowing, never a
    refusal."""
    for base in ("origin/master", "origin/main"):
        probe = subprocess.run(
            ["git", "-C", str(top), "merge-base", base, "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode != 0:
            continue
        # Diffed against the working tree, not HEAD, so the edit being tested
        # counts before it is ever committed.
        diff = subprocess.run(
            ["git", "-C", str(top), "diff", "--name-only", probe.stdout.strip()],
            capture_output=True,
            text=True,
            check=False,
        )
        if diff.returncode != 0:
            return []
        touched = {
            PurePosixPath(line).parent.as_posix() for line in diff.stdout.splitlines() if line
        }
        # Exact directory matches only. An edit in a scripts directory NEXT TO
        # a job directory (pxb/v2/docker beside pxb/v2/jenkins) must fall
        # through to the candidate listing, never guess an ancestor that
        # happens to hold unrelated YAML.
        return [candidate for candidate in candidates if candidate in touched]
    return []


def _narrow_dirs(top: Path, candidates: list[str]) -> tuple[list[str], str]:
    """Candidates narrowed by where the user stands, then by what their branch
    edits. The second element says which signal decided ("cwd", "branch"), and
    is empty when neither leaves exactly one."""
    scoped = candidates
    try:
        rel = Path.cwd().resolve().relative_to(top.resolve()).as_posix()
    except ValueError:
        rel = "."
    if rel != ".":
        within = [c for c in candidates if c == rel or c.startswith(rel + "/")]
        if within:
            scoped = within
    if len(scoped) == 1:
        return scoped, "cwd"
    touched = _branch_touched(top, scoped)
    if len(touched) == 1:
        return touched, "branch"
    return scoped, ""


def _example_yaml_dir(top: Path) -> tuple[str, str]:
    """(directory to draft from, how it was chosen). Narrows by where the user
    stands, then by what their branch edits, and refuses only when both leave
    more than one candidate, listing each as a ready-to-run command."""
    candidates = _detect_yaml_dirs(top)
    if not candidates:
        die(
            "no JJB job definitions found in this checkout",
            "this is probably not your pipelines clone. cd into the fork checkout "
            "that holds the job YAML, on the branch under test, then rerun",
        )
    scoped, how = _narrow_dirs(top, candidates)
    if how:
        return scoped[0], {
            "cwd": "under your working directory",
            "branch": "this branch edits it",
        }[how]
    listing = "\n".join(f"    jenkins-preview sets --discover {c}" for c in scoped)
    die(
        f"this checkout holds {len(scoped)} directories of job definitions:\n{listing}",
        "cd into the one you work on and rerun, or run one of the lines above",
    )
    raise AssertionError  # unreachable, die() raised; ruff RET503 cannot see NoReturn


def _infer_set() -> str:
    """The set --set would have named: the only one, else the one whose
    directory this checkout points at. Power is untouched, --set always wins."""
    if not SETS:
        die(
            "--set was not given and no sets file was found",
            "from inside your pipelines clone, draft one: "
            f"jenkins-preview sets --example > {REPO_CONFIG}",
        )
    if len(SETS) == 1:
        only = next(iter(SETS))
        print(f"set: {only} (the only one in the sets file)", file=sys.stderr)
        return only
    top = _git_toplevel()
    if top is not None:
        dirs = sorted({job_set.yaml_dir for job_set in SETS.values()})
        scoped, how = _narrow_dirs(top, dirs)
        if how:
            matching = sorted(name for name, s in SETS.items() if s.yaml_dir == scoped[0])
            if len(matching) == 1:
                why = {
                    "cwd": "its directory is under your working directory",
                    "branch": "this branch edits its directory",
                }[how]
                print(f"set: {matching[0]} ({why})", file=sys.stderr)
                return matching[0]
    die(
        "--set was not given and this checkout does not single one out",
        f"pass --set, one of: {', '.join(sorted(SETS))}",
    )
    raise AssertionError  # unreachable, die() raised; ruff RET503 cannot see NoReturn


_SWAP_SUFFIX = re.compile(r"-sw[0-9a-f]{6}$")


def _infer_folder(jenkins: Jenkins) -> str:
    """The preview this checkout means: owned by this user, anchored to this
    branch, published from this repo. The live markers are the authority, so
    folders minted with --name match too, and a broken sets file changes
    nothing here."""
    top = _git_toplevel()
    if top is None:
        die(
            "no folder given and this is not a git checkout",
            "pass the preview folder, `jenkins-preview list` shows them",
        )
    probe = subprocess.run(
        ["git", "-C", str(top), "branch", "--show-current"],
        capture_output=True,
        text=True,
        check=False,
    )
    branch = probe.stdout.strip()
    if probe.returncode != 0 or not branch:
        die(
            "no folder given and this checkout has no current branch",
            "pass the preview folder, `jenkins-preview list` shows them",
        )
    origin_probe = subprocess.run(
        ["git", "-C", str(top), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    origin = origin_probe.stdout.strip() if origin_probe.returncode == 0 else ""
    if not origin:
        # Without an origin the repo leg of the match cannot be verified, and
        # a half-verified guess must not pick a deletion target.
        die(
            "no folder given and this checkout has no origin remote to match previews against",
            "pass the preview folder, `jenkins-preview list` shows them",
        )
    data = jenkins.get_json(f"/job/{PREVIEW_ROOT}", tree="jobs[name,description]")
    if not data:
        die(f"/{PREVIEW_ROOT} not found", "ask a Jenkins admin to create it")
    mine = []
    for job in data.get("jobs", []):
        meta = parse_folder_marker(job.get("description"))
        # The anchor is the resolved ref, not the spelling the operator typed,
        # so `--ref refs/heads/x` matches and a tag named like the branch never
        # does. Legacy markers carry it too.
        if meta.get("owner") != jenkins.user or meta.get("anchor") != f"refs/heads/{branch}":
            continue
        if repo_identity(meta.get("repo", "")) != repo_identity(origin):
            # Two clones of different forks can share a branch name. The other
            # fork's preview must never be selected, let alone deleted, from
            # this one.
            continue
        if _SWAP_SUFFIX.search(job["name"]) and meta.get("view") not in ("", "none", job["name"]):
            # A swap temp records the FINAL folder as its view, so the marker
            # disagreeing with the folder's own name is the corroboration. A
            # hand-named look-alike records itself or none, and stays
            # inferrable for its whole life.
            continue
        mine.append(job["name"])
    if len(mine) == 1:
        print(f"folder: {mine[0]} (your preview of branch {branch})", file=sys.stderr)
        return mine[0]
    if not mine:
        die(
            f"no preview of yours pins branch {branch} on this Jenkins",
            "run `jenkins-preview up` first, pass the folder, or check `jenkins-preview list`",
        )
    die(
        f"branch {branch} has {len(mine)} previews of yours: {', '.join(sorted(mine))}",
        "pass the one you mean",
    )
    raise AssertionError  # unreachable, die() raised; ruff RET503 cannot see NoReturn


def sets_cmd(jenkins: Jenkins | None, args: argparse.Namespace) -> int:  # noqa: ARG001
    """List the loaded sets and the file they came from. Needs no credentials."""
    if args.example or args.discover:
        top = _git_toplevel()
        if top is None:
            die(
                "this command drafts from the checkout you are standing in, and "
                "this is not a git checkout",
                "cd into your pipelines clone first",
            )
        if args.example:
            yaml_dir, why = _example_yaml_dir(top)
            print(f"yaml dir: {yaml_dir} ({why})", file=sys.stderr)
        else:
            yaml_dir = args.discover
        say(discover_document(top, yaml_dir))
        return 0

    if not SETS:
        if where := config_file():
            say(f"sets file {where} defines no sets")
        else:
            say("no sets file found. The tool looks, in order:")
            for i, location in enumerate(SEARCH_ORDER, start=1):
                say(f"  {i}. {location}")
        say("")
        say(
            "start one from inside your pipelines clone: "
            f"jenkins-preview sets --example > {REPO_CONFIG}"
        )
        return 1

    say(f"sets file: {config_file()}")
    say("")
    say(f"{'set':<12} {'jobs':<5} stages")
    for name in sorted(SETS):
        job_set = SETS[name]
        say(f"{name:<12} {len(job_set.jobs):<5} {' -> '.join(job_set.stage_order)}")
    return 0
