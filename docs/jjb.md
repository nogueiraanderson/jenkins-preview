# How this differs from JJB

It does not compete with Jenkins Job Builder, it contains it. JJB (pinned,
`jenkins-job-builder==6.5.0`) is the rendering engine. Everything around it
exists because JJB stops at rendering and deploying.

## Why it exists

With JJB alone, a change has exactly two places to run. The production job,
which `jenkins-jobs update` overwrites in place. Or a second staging Jenkins
someone must keep identical.

There is no built-in "run MY branch's version of these six jobs, next to
production, and delete it after". That missing third place is this tool.

## Division of labor

| | JJB | jenkins-preview |
|---|---|---|
| Purpose | Manage the production job estate | Test a change to it, disposably |
| Deploy target | The real job names, in place | An isolated `/previews` copy, production unreachable through this tool's writes (G1) |
| Source | Your working tree, as is | A fork ref pinned to one commit, fetched fresh |
| Job selection | Paths and names on each invocation | A committed sets file, drafted by `sets --example`. Set and folder are inferred from the checkout |
| Safety | Trusts the operator | Gates that refuse rather than guess: sanitising, read-back, markers, rollback |
| Validation | `test` renders XML, stops | Also verifies what Jenkins stored, and scans what each script would fetch at build time (G12) |
| Republish | `update` overwrites in place | `sync` and `up --update` publish the replacement fully, then swap. A failed republish never leaves you without a preview |
| Lifecycle | Jobs live until deleted | `down` and `reap`, only marker-carrying folders deletable, never blocked by a broken sets file |
| Execution | Out of scope | Stage ordering, producer gate, cost confirmation, per-job trigger endpoint |
| Scope | Whole directories | Only the requested set's files copied in |

## Rendering

The JJB version is pinned as a dependency, so every user renders a commit the
same way. A system JJB is only a fallback.

JJB also runs with `--conf /dev/null`. Ambient config cannot make two
machines render the same commit differently.

## The trade

The traditional JJB-only answer is a second, staging Jenkins (the Linux
Foundation sandbox pattern, wiped weekly). This tool replaces the second
controller with an isolated folder on the real one, which keeps the real
agents, credentials and labels in play.

That trade is also the limit. The folder confines what the tool writes, and
the code that runs inside it is trusted code with real agents and
credentials. See [design.md](design.md) and [gates.md](gates.md).
