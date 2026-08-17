# Gates

The tool refuses rather than warns. Each gate is exercised by tests that
assert it fires on a violation and stays quiet on clean input. Decisions and
the Jenkins mechanics behind the gates: [design.md](design.md).

| Gate | Refuses when | Prevents |
|---|---|---|
| G1 | A write path does not address a single safe name inside `/job/previews/`, or contains `..`, `//`, `%2f`, `%2e` (either case) or a backslash. Asserted in the one request method every write passes through. | Modifying a production job at all, including by traversal. |
| G2 | The ref does not resolve to a commit. | Publishing something with no fixed identity. |
| G3 | No branch or tag tip resolves to the requested commit. | A publish that succeeds and then dies at checkout. |
| G4 | Any requested job fails to render, renders nothing, or is defined in two files. | A partial or ambiguous set, where the missing half hangs a consumer. |
| G5 | The final config carries a trigger (recognised or not), an auth token, the wrong remote or commit, a narrowed refspec, a shallow clone, `honorRefspec`, `noTags`, lightweight checkout, no SCM-backed definition, or an unsupported job type. | Externally triggerable or unpinned previews, and types (multibranch) whose remotes no check can prove. |
| G6 | Any G5 assertion fails against what Jenkins actually stored, re-read after upload. | Trusting what was sent instead of what landed. |
| G7 | A cross-job reference starts with `/`, contains `..`, or names a job outside the published set. A remote-trigger plugin element is refused outright: it starts builds on another controller, where set membership proves nothing. | A preview driving production jobs through its config. |
| G8 | Anything fails between folder creation and the tab. The view is removed, then the folder, each removal verified. | A half-published folder, or a global tab advertising one. |
| G9 | A folder or view description does not begin with this tool's marker, or the paired markers disagree on the preview id. | Deleting something the tool did not create. |
| G10 | A credential on the command line, a repository URL carrying embedded credentials or a query string, or a `JENKINS_USER` carrying whitespace or `=`. | Tokens in `ps` and logs, credentials in job configs, forged marker fields. |
| G11 | A root view write is anything other than creating or deleting this tool's own generated view. | Touching the shared root views everyone else uses. |
| G12 | A pipeline script would fetch canonical, library or sibling code at build time, or could not be scanned, without `--allow-foreign-fetch`. Fetches of unrelated repos are disclosed, never blocking. A script missing at the pin refuses with no override. | A green preview that silently tested production's copy of the code. |

## Notes

**G3 and the anchor.** A commit no ref points at must first get a branch
pushed at it. Stricter than Jenkins needs: it guarantees the local clone
contains the commit and gives `status` an anchor to compare against.

**G7 and set membership.** If a job legitimately references a sibling, add
the sibling to the set. A selector like `sibling/BUILD_TYPE=debug` is
allowed, only the leading segment names a job.

**G8 and updates.** An update never tears down first. The replacement
publishes fully under a sibling name, then the old preview goes, then a rename
slots the replacement in. An early failure keeps the old preview, a late one
keeps the fully working replacement under the sibling name, which the error
names. Only the tab can be missing after a late failure, never the jobs. A
teardown whose response is lost is probed rather than assumed: an old folder
that is really gone lets the swap continue, one still standing rolls the
replacement back and keeps the old preview. When the probe itself fails on the
same outage, nothing is rolled back on a guess and the error names both
surviving folders.

**G9 and the markers.** Folder and view carry paired markers sharing a random
128-bit preview id. A marker prevents accidental collision, not deliberate
forgery (Jenkins offers no compare-and-delete primitive). `down` also compares
the marker's owner to the caller, so deleting a colleague's preview takes
`reap` (the operator sweep) or the colleague. One id mismatch is tolerated:
a view whose own marker pairs it with a different, still-existing folder is
left alone, so the leftover of a crashed swap can always be deleted.

**G11 and permissions.** Root views are on by default so reviewers find the
preview from the landing page (`--no-root-view` skips the tab). They need
root `View/Create` and `View/Delete` permissions. Lacking them, the default
`up` fails at the view step and rolls the whole preview back.

**G12 and the fidelity scan.** The config gates prove what Jenkins stores.
G12 reads each pipeline's script at the pinned checkout and classifies what
it would fetch once running.

- `canonical`: the pinned repo's own name under any owner, and any remote the
  rendered configs themselves name. Blocks.
- `library`: a shared-library load not proven to be the fork at the pinned
  commit. Blocks.
- `sibling`: a `build job:` or `projectName:` literal naming a job outside
  the set. Jenkins resolves these folder-first and then towards the root,
  where production lives. Blocks.
- `uninspectable`: the script could not be read or parsed. Blocks, because
  unscanned must never read as clean.
- `external`: any other repo. Disclosed, never blocking.
- `missing`: the scriptPath does not exist at the pinned commit. Refuses with
  no override.

`--allow-foreign-fetch` acknowledges the blocking kinds. The count of every
disclosed fetch, blocking or not, is stamped into the folder marker and echoed
by `status`. The scan is line-based and literal, so a URL built from variables
is invisible to it.

Pinning the canonical repo itself (`--repo` at the canonical URL) is
supported. Its own in-script git fetches then stay inside the pinned repo,
so those stop blocking. They still fetch whatever ref each script names,
not the pin, and a shared-library load keeps its own rule: it blocks unless
it names the pinned repo at the pinned commit.

## What no gate can cover

- A `triggers { cron(...) }` in the fork's Jenkinsfile re-installs a trigger
  on the first build. The uploaded XML is clean, the job is not clean after
  it runs.
- A URL or job name built from variables at run time.
- Anything the running build does with the credentials and agents it
  legitimately holds.

The gates confine writes to the `/previews` folder. They do not sandbox the code that runs, so publish previews of code you trust.

## Running the gate tests

```bash
just test
```

No network and no Jenkins required. The gates are ordinary functions, usable
directly:

```python
from jenkins_preview.client import assert_write_path

assert_write_path("/createItem?name=x")  # raises Fail
```

An empty XML element serialises with whitespace inside it, so a test that
injects a violation with a naive string replace can match nothing and pass
while proving nothing. Inject through ElementTree and assert the injection
took effect.
