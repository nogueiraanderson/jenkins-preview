"""Refusals.

Every failure this tool raises names a cause and a fix. That is the whole point:
the user is a database engineer, not a Jenkins administrator, and a stack trace
is not an actionable error message.
"""

from typing import NoReturn


class Fail(Exception):
    """A refusal carrying a cause and, where one exists, a fix."""

    def __init__(self, cause: str, fix: str = "") -> None:
        self.cause = cause
        super().__init__(f"{cause}\n  fix: {fix}" if fix else cause)


def die(cause: str, fix: str = "") -> NoReturn:
    """Refuse to continue.

    Typed NoReturn so a type checker proves the unreachability of anything after a
    call, rather than needing a dead `raise` to assert it.
    """
    raise Fail(cause, fix)


_output_written = False


def say(message: str) -> None:
    """Write a line of progress output."""
    global _output_written
    _output_written = True
    print(message, flush=True)


def mark_output() -> None:
    """Record output that bypassed say(), like an interactive prompt, so the
    error separator still lands on its own line."""
    global _output_written
    _output_written = True


def output_written() -> bool:
    """Whether this invocation printed progress yet. The error handler uses this
    to separate ERROR from real output without ever leading with a blank line."""
    return _output_written


def reset_output() -> None:
    """Start-of-invocation reset. Only main() calls this, once per run."""
    global _output_written
    _output_written = False
