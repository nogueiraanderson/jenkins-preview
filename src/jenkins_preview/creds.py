"""Credential resolution: the environment first, then the YAML config file.

Two sources only, resolved per field, so the token can come from the
environment while url and user live in the file. The tool never writes
credentials anywhere, never prompts for them, and never accepts them on argv
(gate G10).
"""

import os
import re
from pathlib import Path

import yaml

from .errors import die

ENV_URL = "JENKINS_URL"
ENV_USER = "JENKINS_USER"
ENV_TOKEN = "JENKINS_TOKEN"
ENV_SERVER = "JENKINS_SERVER"

FIELDS = (("url", ENV_URL), ("user", ENV_USER), ("token", ENV_TOKEN))

_FIXES = {
    "url": "https://jenkins.example.com",
    "user": "<your jenkins user id>",
    "token": "<api token from /me/configure on your Jenkins>",
}


def field_fix(field: str) -> str:
    env = dict(FIELDS)[field]
    return (
        f"export {env}={_FIXES[field]}, or add {field} to "
        f"{credentials_path()} (layout in the README)"
    )


def credentials_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "jenkins-preview" / "credentials.yaml"


def user_problem(user: str) -> str | None:
    """Why a user id cannot be accepted, or None when it can.

    The ownership marker is a whitespace-delimited key=value line. A user id
    carrying whitespace or '=' would let a crafted id displace marker fields
    (owner=x id=attacker), which silently defeats rollback and teardown.
    """
    if re.search(r"\s", user) or "=" in user:
        return "must not contain whitespace or '='"
    return None


def _selected_server(path: Path) -> tuple[dict, str]:
    """The chosen server mapping and its name from the credentials file."""
    mode = path.stat().st_mode
    if mode & 0o077:
        die(
            f"{path} is readable by group or others",
            f"it holds a token: chmod 600 {path}",
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        die(f"{path} is not valid YAML: {exc}", "fix the file, the layout is in the README")
    servers = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(servers, dict) or not servers:
        die(
            f"{path} must map 'servers' to at least one named server",
            "layout: servers -> <name> -> url, user, token (see the README)",
        )
    name = os.environ.get(ENV_SERVER) or data.get("current")
    if name is None:
        if len(servers) == 1:
            name = next(iter(servers))
        else:
            die(
                f"{path} defines {len(servers)} servers and no 'current'",
                f"add current: <name> to the file, or export {ENV_SERVER}=<name>. "
                f"Servers: {', '.join(sorted(servers))}",
            )
    if name not in servers:
        die(
            f"server {name!r} is not in {path}",
            f"one of: {', '.join(sorted(servers))}",
        )
    server = servers[name]
    if not isinstance(server, dict):
        die(f"server {name!r} in {path} must be a mapping", "url, user and token keys")
    return server, str(name)


def resolve(*, require: bool = True) -> tuple[dict[str, str | None], dict[str, str]]:
    """Field values and where each came from.

    Returns ({url, user, token}, {field: "env" | "credentials.yaml (<server>)"
    | "missing"}). With `require`, the first missing field dies naming both
    fixes; without it, callers (doctor) report the gaps themselves.
    """
    path = credentials_path()
    server, name = _selected_server(path) if path.exists() else ({}, "")

    values: dict[str, str | None] = {}
    sources: dict[str, str] = {}
    for field, env in FIELDS:
        if from_env := os.environ.get(env):
            values[field], sources[field] = from_env, "env"
        elif from_file := server.get(field):
            values[field], sources[field] = str(from_file), f"credentials.yaml ({name})"
        else:
            values[field], sources[field] = None, "missing"

    if require:
        for field, env in FIELDS:
            if values[field] is None:
                die(f"{env} is not set", field_fix(field))
        if problem := user_problem(values["user"] or ""):
            die(
                f"JENKINS_USER {problem}",
                "use the Jenkins user ID from the top of /me/configure, not the display name",
            )
    return values, sources
