"""Name validation.

Names supplied by a user reach three places where a loose value is dangerous: a
Jenkins API path, a Jenkins view's `includeRegex`, and the folder description that
carries the ownership marker. One strict charset covers all three, so validation
lives here rather than being re-derived at each call site.
"""

import hashlib
import json
import re
from urllib.parse import urlsplit

from .errors import die

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
"""Conservative on purpose: no slashes, no dots-only, no leading punctuation.

A slash would let a path escape the previews folder. A regex metacharacter would
let a view's includeRegex match jobs outside the preview folder.
"""

_SCP_LIKE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:[A-Za-z0-9._/~-]+$")
"""git@host:org/repo.git. No scheme, no way to embed a password."""


def check_name(value: str, what: str) -> str:
    """Return `value` if it is a safe single path segment, else refuse."""
    # fullmatch, not match: with match, `$` accepts a trailing newline.
    if not SAFE_NAME.fullmatch(value) or value in {".", ".."}:
        die(
            f"invalid {what}: {value!r}",
            "use only letters, digits, dot, dash and underscore, starting with a "
            "letter or digit, at most 64 characters. A name containing a slash or a "
            "regex character could reach jobs outside the preview folder",
        )
    return value


def slug_text(text: str) -> str:
    """Unsafe characters become dashes. Leading and trailing punctuation goes."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.")


def derived_folder_name(user: str, set_name: str, branch: str) -> str:
    """`preview-<user>-<set>-<branch>`, always fitted inside the 64-character cap.

    The zero-flag path must never die on length. A branch too long for the cap is
    truncated and completed with a short digest of the FULL branch name, so the
    same branch keeps republishing into the same folder while two long branches
    sharing a prefix stay distinct. An explicit --name skips all of this and is
    refused loudly instead, because a name the user typed must not be rewritten.
    """
    user_slug = slug_text(user) or "user"
    branch_slug = slug_text(branch)
    lossless = user_slug == user and branch_slug == branch and branch_slug
    if lossless:
        folder = f"preview-{user_slug}-{set_name}-{branch_slug}"
        if len(folder) <= 64:
            return check_name(folder, "preview folder name")

    # Slugging lost information (or the name overflows), so identity comes from
    # a digest of the RAW inputs, serialized unambiguously: `feature/foo` and
    # `feature-foo`, or two users slugging alike, must never share a folder.
    digest = hashlib.sha256(json.dumps([user, set_name, branch]).encode()).hexdigest()[:12]
    user_part, branch_part = user_slug, branch_slug

    def compose() -> str:
        tail = f"{branch_part}-{digest}" if branch_part else digest
        return f"preview-{user_part}-{set_name}-{tail}"

    if (overflow := len(compose()) - 64) > 0:
        branch_part = branch_part[: max(0, len(branch_part) - overflow)].rstrip("-.")
    if (overflow := len(compose()) - 64) > 0:
        user_part = user_part[: max(0, len(user_part) - overflow)].rstrip("-.")
        if not user_part:
            die(
                f"the set name {set_name!r} leaves no room for a preview name",
                "shorten the set name in the sets file, or pass a short --name",
            )
    return check_name(compose(), "preview folder name")


def check_repo_url(repo: str) -> str:
    """Refuse a repository URL that carries embedded credentials.

    A URL like https://user:token@host/repo would be passed to git in argv, written
    into every published job config, and written into the folder description that
    `list` and `status` show to everyone on the controller.
    """
    # SCP-style SSH (`git@host:org/repo.git`) has no scheme and cannot carry a
    # password, so it is accepted as-is.
    if _SCP_LIKE.fullmatch(repo):
        return repo

    parsed = urlsplit(repo)
    # A password is never legitimate here. A username is normal for SSH (`git@host`)
    # but on http/https it is the usual way a token gets smuggled into a URL. A query
    # string is how the other kind gets smuggled (?access_token=...), and no git
    # remote needs one.
    if parsed.password or (parsed.username and parsed.scheme in {"http", "https"}):
        die(
            "the repository URL contains embedded credentials",
            "pass a plain URL. For a private repository, configure a Jenkins "
            "credential and reference it by id, or use SSH",
        )
    if parsed.query or parsed.fragment:
        die(
            "the repository URL carries a query string or fragment",
            "pass the plain clone URL. Tokens do not belong in it",
        )
    if parsed.scheme not in {"http", "https", "ssh", "git"}:
        die(
            f"unsupported repository scheme {parsed.scheme!r}",
            "pass an http, https, ssh or git URL, or SCP-style git@host:path",
        )
    return repo
