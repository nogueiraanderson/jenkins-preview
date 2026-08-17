# Design

Decisions, and the Jenkins mechanics that forced them.

## What gets created

The tool invents no job type. It renders whatever the YAML defines. For a
six-job set that is a `Folder`, pipeline jobs (`CpsScmFlowDefinition`),
`matrix-project` jobs and a `MultiJobProject`.

Which transformations apply where:

| Transformation | Applies to |
|---|---|
| Force heavyweight checkout | Pipeline jobs (only they have `<lightweight>`) |
| Rewrite remote, pin the commit | The checkout-driving SCMs: the pipeline definition's and the job's top-level one |
| Strip tokens and triggers, create disabled, stamp provenance | All types |

Nothing is renamed. See basenames below.

## Who clones what

Two clones, neither uploads a workspace. The tool uploads config XML only.

Locally, at publish time, a blobless non-shallow single-branch clone renders
the YAML exactly as it exists at the pinned commit. Cheap on a large repo,
and non-shallow on purpose: a shallow clone could miss the pinned commit.

On the agent, at build time, the published job performs its own heavyweight
checkout of the fork and resolves the pinned SHA. This is why the commit must
be reachable from a branch or tag.

## Jenkins mechanics

### A bare-SHA pin requires heavyweight checkout

With `<lightweight>true</lightweight>` and a 40-hex `BranchSpec`, the script
load asks for `refs/heads/<sha>`, which does not exist, and the build dies in
about 300 ms:

```
git fetch ... +refs/heads/<sha>:refs/remotes/origin/<sha>
fatal: couldn't find remote ref refs/heads/<sha>
```

With lightweight off, the wide refspec is fetched and the SHA resolved
locally (`DefaultBuildChooser` special-cases a 6-40 hex `BranchSpec`). So the
commit only needs to be reachable, not a branch tip. A dangling commit cannot
be fetched at all, which is why the tool demands an anchor ref. A shallow
clone can miss the commit even on this path, so non-shallow is forced too.

### A branch-name pin floats

Pinning to a branch name works, and then silently moves on the next push. The
tool therefore resolves to a SHA at publish time, and `status` reports when
the branch has moved past the pin.

### Job basenames must not change

Jenkins resolves a bare `projectName` folder-first. Keeping original
basenames and isolating only by folder is what keeps `copyArtifacts` and
multijob phase links pointing inside the preview.

### Consumer stages hang rather than fail

A test stage globs the newest successful producer artifact. With no match it
spins in `until <fetch>; sleep 5` for the stage timeout, 240 minutes on one
job family. The tool refuses to start a consumer whose producer has no
successful build.

### A folder with no views returns HTTP 500

A folder config with an empty `<views/>` is accepted on creation, then its
own `api/json` returns 500. Folder configs must carry an `AllView`.

### Rendering the whole directory is fragile

One unrelated definition that does not render standalone blocks every set.
The tool stages only the files defining the requested jobs, found by the job
names inside them, not by file name, plus every defaults, macro and template
file beside them. Discovery (`sets --example`) probes each definition file
alone for the same reason, and skips what does not render with JJB's reason.

## Root views, the tab

The view lists job paths explicitly instead of an `includeRegex`. A
user-supplied slug inside a regex could match every job on the controller.
An explicit list is immune and narrower.

Order matters both ways. The view is created last, after the jobs are
verified, so no global tab ever advertises a half-built preview. Teardown
deletes the view first, because the reverse can strand an empty tab.

The tab is on by default, because it is how a reviewer finds the preview.
`--no-root-view` skips it. Both directions stay bounded by G11.

## Decisions

| Decision | Rationale |
|---|---|
| Previews run trusted code | Fork pipeline code picks its own agents and credentials, and only server-side preview agents could constrain that. The folder boundary confines writes only. |
| Curated job sets, no graph inference | Parsing pipelines to infer a graph is a tarpit. `sets --example` drafts the job list from what renders. The stage graph stays a small table, honest and reviewable. |
| Resolve any ref to a SHA, pin the SHA | Convenience of a branch, reproducibility of a commit. |
| Heavyweight forced, never a flag | Exposing it hands the user a 300 ms failure with a misleading error. |
| Render from the repo, never copy live configs | Live configs drift and carry other people's experiments. |
| Sanitising mandatory | Tokens and triggers must never reach a preview. |
| Publish disabled, enable after read-back | No half-built folder ever looks usable. |
| Credentials from the environment or a read-only YAML file, never a prompt | Attributable, revocable, never in `ps`. Every Jenkins CLI reads env or a file; a diagnostics tool that collects secrets surprises. The tool never writes the file, so there is nothing for it to go stale on. |
| Render only the requested jobs | An unrelated broken definition cannot block the set. |
| Fidelity refusal over a warning | A foreign fetch makes a green preview a lie. The `--allow-foreign-fetch` acknowledgment is explicit and stamped on the preview. |
| Update renders before it destroys | A broken ref can never cost the working preview. |
| Derived names always fit | Lossy or long inputs get a stable digest. A typed `--name` is refused loudly, never rewritten. |
| JJB runs hermetically | `--conf /dev/null`, or two machines could render one commit differently. |
| Ambiguity refused, not resolved | A job in two files, or a duplicated JSON key, would silently pick a winner. |
| Set and folder inferred from the checkout | Explicit arguments always win. Ambiguity stops and lists the candidates, and an inferred `down` confirms before deleting. |
| Parameters validated against declarations | Jenkins silently drops an undeclared key. A refusal replaces an invisible no-op. |
| Sync inherits only a stamped, unchanged acknowledgment | The operator consented to an enumerated list of fetches. Anything new re-refuses. |
| Updates swap, never gap | The replacement publishes fully under a sibling name before the old preview goes. No mid-publish failure can leave zero previews. |

## Layout

```
src/jenkins_preview/
  __init__.py     version, preview root, ownership marker
  errors.py       Fail, die, say
  sets.py         job sets, loaded from a required external file
  names.py        safe names, slugs, fitted folder names
  folders.py      paths, markers, config templates
  client.py       REST client and the write gate (G1)
  gitref.py       ref resolution, blobless non-shallow checkout
  render.py       staging and JJB (G4)
  transform.py    sanitise, validate, verify_readback (G5, G6, G7)
  fidelity.py     the build-time fetch scan (G12)
  commands.py     one function per subcommand
  cli.py          parsing, credentials, dispatch
tests/            one module per concern, plus test_spec_coverage.py,
                  which holds docs/SPEC.md and the suite in lockstep
```

G1 lives in `client.py` on purpose. It sits in the single function every
write funnels through, so a new command cannot bypass it.

## Trade-offs taken

- JJB is a dependency, not reimplemented. Rendering fidelity is the main
  technical risk, and half-reimplementing a template engine is worse.
- It started as one file to resist scope creep, and grew into a package for
  a console entry point, tests and CI. The resistance now has to come from
  the maintainer.
- No cloud-cost control. The tool prints the machine count a run implies and
  asks.
