"""Argument parsing, credential handling, and dispatch."""

import argparse
import sys
from collections.abc import Callable

from . import __version__
from .client import Jenkins
from .commands import doctor, down, preview_list, reap, run, sets_cmd, status, sync, up
from .creds import resolve
from .errors import Fail, output_written, reset_output
from .names import check_repo_url
from .sets import CONFIG_ENV, REPO_CONFIG, SETS, initialize

type Command = Callable[[Jenkins | None, argparse.Namespace], int]

COMMANDS: dict[str, Command] = {
    "doctor": doctor,
    "up": up,
    "status": status,
    "run": run,
    "sync": sync,
    "down": down,
    "list": preview_list,
    "reap": reap,
    "sets": sets_cmd,
}

# Commands that never talk to Jenkins, so they must not demand credentials.
OFFLINE_COMMANDS = frozenset({"sets"})

CREDENTIAL_FLAGS = ("--token", "--api-token", "--password", "--pass")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jenkins-preview",
        description="Publish throwaway Jenkins job previews pinned to your own fork.",
        epilog=(
            "Credentials come from JENKINS_URL, JENKINS_USER and JENKINS_TOKEN, "
            "or from ~/.config/jenkins-preview/credentials.yaml. A token is "
            "never accepted as a command-line argument."
        ),
    )
    parser.add_argument("--version", action="version", version=f"jenkins-preview {__version__}")
    parser.add_argument(
        "--sets",
        metavar="PATH",
        help=f"sets file to use (default: ${CONFIG_ENV}, then {REPO_CONFIG} at the "
        "checkout root, then ~/.config/jenkins-preview/sets.json)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def command(name: str, **kwargs) -> argparse.ArgumentParser:
        """A subcommand that also accepts --sets AFTER its own name.

        The value is consumed by the pre-parse scan in main() long before this
        parser runs. Declaring it everywhere only stops argparse rejecting the
        natural `jenkins-preview sets --sets FILE` spelling.
        """
        leaf = sub.add_parser(name, **kwargs)
        leaf.add_argument("--sets", help=argparse.SUPPRESS)
        return leaf

    # Both help texts survive an empty registry: with no sets file found yet,
    # --help must still render and point at the scaffold command.
    set_names = ", ".join(sorted(SETS)) or "the sets in your sets file (none found yet)"
    stages = (
        ", ".join(sorted({stage for job_set in SETS.values() for stage in job_set.stage_order}))
        or "a stage from your sets file"
    )

    def add_source(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--set",
            help=f"one of: {set_names} (default: the only set in the file, "
            "else the one this checkout points at)",
        )
        target.add_argument(
            "--repo",
            help="git URL of your fork (default: this checkout's origin remote)",
        )
        target.add_argument(
            "--ref",
            help="branch, tag, or a commit a branch points at (default: this "
            "checkout's current branch)",
        )

    add_source(command("doctor", help="preflight checks, writes nothing"))

    publish = command("up", help="publish a preview folder")
    add_source(publish)
    publish.add_argument("--name", help="folder slug (default: preview-<user>-<set>-<branch>)")
    publish.add_argument("--dry-run", action="store_true", help="render and gate, create nothing")
    publish.add_argument(
        "--update",
        action="store_true",
        help="republish over an existing preview of the same name",
    )
    publish.add_argument(
        "--allow-foreign-fetch",
        action="store_true",
        help="publish even when the pipeline fetches canonical, library or sibling "
        "code at build time, or cannot be scanned (the preview then does not "
        "test that code)",
    )
    publish.add_argument(
        "--root-view",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="create a root-level tab listing the preview jobs",
    )

    folder_help = (
        "preview folder name, as printed by `up` and `list` "
        "(default: your preview of this checkout's branch)"
    )
    command("status", help="show a preview's jobs and pin").add_argument(
        "folder", nargs="?", help=folder_help
    )

    trigger = command("run", help="trigger the next stage (or --stage to pick one)")
    trigger.add_argument("folder", nargs="?", help=folder_help)
    trigger.add_argument(
        "--stage",
        help=f"pick a stage explicitly ({stages}). Without it, run works "
        "out the next stage itself, producer stages first",
    )
    trigger.add_argument(
        "-p",
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="build parameter for the stage's jobs, repeatable. Needs --stage. "
        "Refused for keys the jobs do not declare",
    )
    trigger.add_argument("--yes", action="store_true", help="skip the cost confirmation")

    refresh = command("sync", help="republish a preview at its branch's current tip")
    refresh.add_argument("folder", nargs="?", help=folder_help)
    refresh.add_argument(
        "--allow-foreign-fetch",
        action="store_true",
        help="acknowledge blocking build-time fetches the new tip introduces "
        "(unchanged ones inherit the acknowledgment stamped at publish)",
    )

    teardown = command("down", help="delete a preview folder and its tab")
    teardown.add_argument("folder", nargs="?", help=folder_help)
    teardown.add_argument("--force", action="store_true", help="delete even with builds running")
    teardown.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation an inferred folder gets",
    )

    command("list", help="list all previews")

    reaper = command("reap", help="delete previews older than N days")
    reaper.add_argument(
        "--older-than",
        type=int,
        default=7,
        metavar="DAYS",
        help="age threshold in days (default: 7)",
    )
    reaper.add_argument(
        "--dry-run", action="store_true", help="list what would be reaped, delete nothing"
    )
    reaper.add_argument("--force", action="store_true", help="reap even with builds running")

    catalog = command("sets", help="list available job sets and where each came from")
    drafting = catalog.add_mutually_exclusive_group()
    drafting.add_argument(
        "--example",
        action="store_true",
        help="draft a sets file from this checkout's own YAML, finding the "
        "directory from where you stand and what your branch edits "
        f"(e.g. `jenkins-preview sets --example > {REPO_CONFIG}`)",
    )
    drafting.add_argument(
        "--discover",
        metavar="YAML_DIR",
        help="like --example, with the directory picked explicitly "
        f"(e.g. `jenkins-preview sets --discover pxb/v2/jenkins > {REPO_CONFIG}`)",
    )

    return parser


def credentials() -> Jenkins:
    """Resolve credentials: environment first, then the YAML config file,
    per field. Never prompted for, never accepted on argv."""
    values, _ = resolve()
    return Jenkins(values["url"], values["user"], values["token"])


# Commands that read only the ownership markers on Jenkins. Teardown and
# inventory must never be hostage to a broken sets file.
MARKER_ONLY_COMMANDS = frozenset({"status", "down", "list", "reap"})


def _subcommand(argv: list[str]) -> str | None:
    """The first positional token, skipping flags and the --sets value."""
    skip_next = False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg == "--sets":
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        return arg
    return None


def _prescan_sets(argv: list[str]) -> str | None:
    """Find --sets before argparse runs, because the parser itself is built from
    the sets registry (the --set choices and --stage help text come from the
    loaded sets)."""
    found = None
    for i, arg in enumerate(argv):
        if arg == "--sets" and i + 1 < len(argv):
            found = argv[i + 1]
        elif arg.startswith("--sets="):
            found = arg.split("=", 1)[1]
    return found


def main(argv: list[str]) -> int:
    reset_output()
    # Checked before argparse, which would otherwise reject an unknown flag with a
    # generic message and never explain why a credential on the command line is refused.
    if any(arg.split("=")[0] in CREDENTIAL_FLAGS for arg in argv):
        print(
            "refusing a credential on the command line: it leaks into ps output, shell "
            "history and CI logs.\n  fix: export JENKINS_TOKEN instead, or put it in "
            "~/.config/jenkins-preview/credentials.yaml.",
            file=sys.stderr,
        )
        return 2

    try:
        try:
            initialize(_prescan_sets(argv))
        except Fail:
            # `sets --example` and `sets --discover` are the repair path for a
            # broken config file, so they must keep working when loading that very
            # file fails, and --help and --version must never be hostage to a
            # broken discovered file either. Marker-only commands never consult
            # the registry, so a broken file must not stop a teardown. Every
            # other command still refuses on one.
            survives = {"--example", "--help", "-h", "--version"} & set(argv) or any(
                arg == "--discover" or arg.startswith("--discover=") for arg in argv
            )
            if _subcommand(argv) not in MARKER_ONLY_COMMANDS and not survives:
                raise
        args = build_parser().parse_args(argv)
        if repo := getattr(args, "repo", None):
            check_repo_url(repo)
        if args.cmd in OFFLINE_COMMANDS:
            client = None
        elif args.cmd == "doctor":
            # Doctor diagnoses missing credentials instead of dying on them.
            try:
                client = credentials()
            except Fail:
                client = None
        else:
            client = credentials()
        return COMMANDS[args.cmd](client, args)
    except Fail as exc:
        separator = "\n" if output_written() else ""
        print(f"{separator}ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        separator = "\n" if output_written() else ""
        print(f"{separator}interrupted", file=sys.stderr)
        return 130


def main_entry() -> None:
    """Console-script entry point."""
    sys.exit(main(sys.argv[1:]))


if __name__ == "__main__":
    main_entry()
