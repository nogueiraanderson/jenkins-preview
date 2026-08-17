# Examples

These sessions are copy-pasteable. Output is trimmed only where marked (`...`).

Set names like `pxb-8.1` are keys from the session's own sets file, not tool
vocabulary. Your file defines your names: bare `jenkins-preview sets` lists
them, and `--set` accepts only those.

## First-time setup

```bash
uv tool install git+https://github.com/nogueiraanderson/jenkins-preview

export JENKINS_URL=https://ps3.cd.percona.com
export JENKINS_USER=you                   # your Jenkins user id, not the display name
export JENKINS_TOKEN=...          # from /me/configure. Or use credentials.yaml, see the README

cd ~/jenkins-pipelines            # your fork checkout, on the branch under test
jenkins-preview sets --example > .jenkins-preview.json   # once. Edit, then commit it

jenkins-preview doctor                        # set and source inferred from the checkout
```

```
jenkins    https://ps3.cd.percona.com
           creds: url from env, user from env, token from env
  PASS  authenticated as nogueiraanderson
  PASS  /previews folder exists
           note: permissions are not probed here, because doctor never writes.
           A 403 can still appear on the first `up`.
set: pxb-v2-jenkins (the only one in the sets file)
source https://github.com/nogueiraanderson/jenkins-pipelines @ fix-compile (inferred from this checkout)
ref        https://github.com/nogueiraanderson/jenkins-pipelines @ fix-compile
  PASS  resolves to dbf83e015fd459d2015fe5d62d20fd83cc49459d via refs/heads/fix-compile
...
render     set pxb-v2-jenkins (16 jobs)
  PASS  all 16 requested jobs rendered
  WARN  6 build-time fetches leave the fork (`up` refuses these
        without --allow-foreign-fetch):
        pxb/v2/jenkins/percona-xtrabackup-2.4-compile-pipeline.groovy:72 (canonical) https://github.com/Percona-Lab/jenkins-pipelines
        pxb/v2/jenkins/percona-xtrabackup-2.4-test-pipeline.groovy:75 (sibling) percona-xtrabackup-2.4-compile-param
        ...

READY
```

Authentication comes first. Without a sets file, doctor still reports the
auth result, then the finding:

```
jenkins    https://ps3.cd.percona.com
           creds: url from env, user from env, token from env
  PASS  authenticated as nogueiraanderson
  PASS  /previews folder exists
           note: permissions are not probed here, because doctor never writes.
           A 403 can still appear on the first `up`.
sets
  FAIL  --set was not given and no sets file was found
  fix: from inside your pipelines clone, draft one: jenkins-preview sets --example > .jenkins-preview.json

NOT READY
```

Run `doctor` whenever something feels off, it writes nothing. The WARN block
lists code a preview would still fetch from the main repo; `up` needs
`--allow-foreign-fetch` to accept that ([gates.md](gates.md)).

## The whole loop, arguments inferred

Inside the checkout, the set and the folder are inferred, so no names are
needed. The session below uses two throwaway smoke jobs:

```bash
jenkins-preview doctor && jenkins-preview up --allow-foreign-fetch
```

```
set: param-smoke (the only one in the sets file)
source https://github.com/nogueiraanderson/jenkins-pipelines @ preview-param-smoke (inferred from this checkout)
...
gates  all pre-upload gates passed for 2 jobs
create preview-nogueiraanderson-param-smoke-preview-param-smoke
verify read-back clean for 2 jobs
enable 2 jobs, verified buildable
```

`run` finds the preview from the current branch, shows the cost, and asks for
confirmation:

```bash
jenkins-preview run
```

```
folder: preview-nogueiraanderson-param-smoke-preview-param-smoke (your preview of branch preview-param-smoke)
stage  one (picked automatically, green so far: none)
builds 1: smoke-echo-param
cost   each build provisions its own worker, plus a shared launcher. Expect up to 2 machines.
proceed? [y/N] aborted, nothing triggered
```

When two previews match, the tool stops and lists both. `status` and `sync`
infer the same way, and a bare `down` asks for confirmation before deleting a
name you never typed:

```bash
jenkins-preview down
```

```
delete preview-nogueiraanderson-param-smoke-preview-param-smoke? [y/N] aborted, nothing deleted
```

`--yes` skips the confirmations:

```bash
jenkins-preview run --yes && jenkins-preview down --yes
```

```
queued smoke-echo-param
...
deleted folder preview-nogueiraanderson-param-smoke-preview-param-smoke
clean
```

Explicit arguments work everywhere, and `up` prints the exact `run` and
`down` commands for its preview.

## Test a branch on your fork, no pull request

From inside the fork checkout, on the branch under test:

```bash
jenkins-preview up --set pxb-8.1
```

On the current pxb pipelines this first refuses, because the scripts fetch
the canonical repo mid-build:

```
source https://github.com/nogueiraanderson/jenkins-pipelines @ preview-docs-session (inferred from this checkout)
...
WARNING 3 build-time fetches leave your fork. Changes to
        what they fetch are NOT in this preview:
        pxb/v2/jenkins/percona-xtrabackup-8.1-compile-pipeline.groovy:67 (canonical) https://github.com/Percona-Lab/jenkins-pipelines
...

ERROR: 3 build-time fetches would silently test code outside your fork
  fix: point git fetches at your fork (`checkout scm`), add sibling jobs to the set, or acknowledge with `up ... --allow-foreign-fetch`
```

If those files are not what you are changing, acknowledge and publish:

```bash
jenkins-preview up --set pxb-8.1 --allow-foreign-fetch
```

```
...
set    pxb-8.1 (6 jobs)
ref    preview-docs-session -> 0bf51418a372b328a2d72b35a7e922fe034c3b5c (anchor refs/heads/preview-docs-session)
...
WARNING 3 build-time fetches leave your fork. Changes to
        what they fetch are NOT in this preview:
...
gates  all pre-upload gates passed for 6 jobs
create preview-nogueiraanderson-pxb-8.1-preview-docs-session
verify read-back clean for 6 jobs
enable 6 jobs, verified buildable
tab    preview-nogueiraanderson-pxb-8.1-preview-docs-session shows 6 jobs

published https://ps3.cd.percona.com/job/previews/job/preview-nogueiraanderson-pxb-8.1-preview-docs-session/
tab       https://ps3.cd.percona.com/view/preview-nogueiraanderson-pxb-8.1-preview-docs-session/
pinned    0bf51418a372b328a2d72b35a7e922fe034c3b5c
foreign   3 build-time fetches outside your fork (WARNING above)
run       jenkins-preview run preview-nogueiraanderson-pxb-8.1-preview-docs-session
teardown  jenkins-preview down preview-nogueiraanderson-pxb-8.1-preview-docs-session
```

The preview is pinned to the commit the branch pointed at. Pushing more
commits changes nothing until you republish.

## Run the stages, in order

```bash
jenkins-preview run preview-nogueiraanderson-pxb-8.1-preview-docs-session --yes
```

```
stage  compile (picked automatically, green so far: none)
builds 1: percona-xtrabackup-8.1-compile-pipeline
cost   each build provisions its own worker, plus a shared launcher. Expect up to 2 machines.
queued percona-xtrabackup-8.1-compile-pipeline

watch  jenkins-preview status preview-nogueiraanderson-pxb-8.1-preview-docs-session
then   jenkins-preview run preview-nogueiraanderson-pxb-8.1-preview-docs-session --stage test   (once this stage is green)
```

The next stage is picked automatically, producers first. Starting the test
stage first is refused:

```
ERROR: stage 'test' consumes 'compile', which has no successful build
  fix: run the producer first: jenkins-preview run preview-nogueiraanderson-pxb-8.1-preview-docs-session --stage compile. Starting mid-stage makes the consumer wait for an artifact that does not exist, burning a 240-minute timeout
```

## Watch it, and iterate

After pushing one more commit, `status` shows the preview still pinned at
the old commit:

```bash
jenkins-preview status preview-nogueiraanderson-pxb-8.1-preview-docs-session
```

```
folder preview-nogueiraanderson-pxb-8.1-preview-docs-session
set    pxb-8.1
pinned 72fe4e5eab680e82759ddd5b5a2bec1e89d8d4f2
repo   https://github.com/nogueiraanderson/jenkins-pipelines (anchor refs/heads/preview-docs-session)
tab    https://ps3.cd.percona.com/view/preview-nogueiraanderson-pxb-8.1-preview-docs-session/
NOTE   3 build-time fetches leave the fork, so that code is
       production's, not this preview's. `up` listed them at publish time.
NOTE   the branch has moved: tip is now db331bc5142f, this preview stays pinned at 72fe4e5eab68
       republish: jenkins-preview sync preview-nogueiraanderson-pxb-8.1-preview-docs-session

job                                              color        last build
percona-xtrabackup-8.1-compile-param             notbuilt     - -
percona-xtrabackup-8.1-compile-pipeline          notbuilt     - -
...
```

`sync` reads everything from the preview itself, the acknowledgment
included:

```bash
jenkins-preview sync preview-nogueiraanderson-pxb-8.1-preview-docs-session
```

```
sync   preview-nogueiraanderson-pxb-8.1-preview-docs-session: 72fe4e5eab68 -> db331bc5142f on preview-docs-session
set    pxb-8.1 (6 jobs)
ref    preview-docs-session -> db331bc5142ff1b56472cac88497f79123bbcc3f (anchor refs/heads/preview-docs-session)
...
update preview-nogueiraanderson-pxb-8.1-preview-docs-session (replaced only after the replacement publishes fully)
...
ack    inheriting the acknowledgment stamped at publish (3 blocking fetches, unchanged)
gates  all pre-upload gates passed for 6 jobs
swap   publishing the replacement as preview-nogueiraanderson-pxb-8.1-preview-docs-session-swf8e2da first
create preview-nogueiraanderson-pxb-8.1-preview-docs-session-swf8e2da
verify read-back clean for 6 jobs
enable 6 jobs, verified buildable
deleted view preview-nogueiraanderson-pxb-8.1-preview-docs-session
deleted folder preview-nogueiraanderson-pxb-8.1-preview-docs-session
rename preview-nogueiraanderson-pxb-8.1-preview-docs-session-swf8e2da -> preview-nogueiraanderson-pxb-8.1-preview-docs-session
tab    preview-nogueiraanderson-pxb-8.1-preview-docs-session shows 6 jobs
...
pinned    db331bc5142ff1b56472cac88497f79123bbcc3f
```

The republish keeps the folder and the tab and moves only the pin. A failed
republish never leaves you without a preview. Build history starts over. A new blocking fetch on the
tip refuses instead of inheriting (`sync --allow-foreign-fetch` acknowledges
it), and an exact-commit pin (`--ref <sha>`) never syncs.

## Add a job set

Each entry has two parts. `yaml_dir` and `jobs` tell `up` which jobs to
publish. `stages`, `stage_order` and `consumes` tell `run` which jobs a stage
triggers, in what order, and which stage waits for another stage's artifact.
Draft the file with `sets --example`, then edit the stage keys.

`sets --example` renders the checkout you are standing in, run here at the
root of a branch that changes a pxb pipeline:

```bash
jenkins-preview sets --example > .jenkins-preview.json
```

```
yaml dir: pxb/v2/jenkins (this branch edits it)
discovered 16 jobs in pxb/v2/jenkins (MultiJobProject: 2, flow-definition: 8, matrix-project: 4, project: 2). Every job is
published. Only stage jobs are triggered by `run`: split `main` into
real stages, order them in stage_order, and wire consumes. Then save
as .jenkins-preview.json at the checkout root and commit it
skipped 3 files that do not render:
    percona-xtrabackup-2.4-compile-param.yml: 63:11: While formatting string 'DOCKER_OS=${DOCKER_OS}\nCMAKE_BUILD_TYPE=${CMAKE_BUILD_TYPE}\n...': Missing parameter: 'DOCKER_OS'
    percona-xtrabackup-2.4-test-param.yml: 81:11: While formatting string 'DOCKER_OS=${DOCKER_OS}\nCMAKE_BUILD_TYPE=${CMAKE_BUILD_TYPE}\nXTRABACKUP_TARGET=${...': Missing parameter: 'DOCKER_OS'
    percona-xtrabackup-2.4-trunk.yml: 41:24: While formatting string '${TRIGGERED_BUILD_NUMBERS_percona_xtrabackup_2_4_compile_param}...': Missing parameter: 'TRIGGERED_BUILD_NUMBERS_percona_xtrabackup_2_4_compile_param'
left out, this tool cannot publish them: percona-xtrabackup-2.4-multijob (references percona-xtrabackup-2.4-compile-param, which is not in this draft)
```

Guidance goes to stderr, the JSON draft to stdout, so the redirect gets a
clean file. The draft is valid as printed. Split `main` into real stages,
wire `consumes`, delete jobs you never want published.

When neither your directory nor your branch narrows it to one, the tool
lists every candidate as a runnable command and stops:

```
ERROR: this checkout holds 34 directories of job definitions:
    jenkins-preview sets --discover IaC
    jenkins-preview sets --discover cloud/jenkins
    ...
    jenkins-preview sets --discover pxb/v2/jenkins
    ...
  fix: cd into the one you work on and rerun, or run one of the lines above
```

`sets --discover <yaml_dir>` is the same drafting with the directory named
explicitly.

A curated set:

```json
{
  "sets": {
    "pxb-8.1": {
      "yaml_dir": "pxb/v2/jenkins",
      "jobs": [
        "percona-xtrabackup-8.1-compile-param",
        "percona-xtrabackup-8.1-compile-pipeline",
        "percona-xtrabackup-8.1-test-param",
        "percona-xtrabackup-8.1-test-pipeline",
        "percona-xtrabackup-8.1-multijob",
        "percona-xtrabackup-8.1-test-cloud-pipeline"
      ],
      "stages": {
        "compile": [
          "percona-xtrabackup-8.1-compile-pipeline"
        ],
        "test": [
          "percona-xtrabackup-8.1-test-pipeline"
        ]
      },
      "stage_order": [
        "compile",
        "test"
      ],
      "consumes": {
        "test": "compile"
      }
    }
  }
}
```

Jobs outside every stage (param, multijob, cloud) are published and gated
like the rest, but `run` never triggers them: start those from the preview's
own UI. `consumes` makes `run` refuse a consumer stage until its producer has
a successful build. One committed file can hold a set per job family.

| Key | Meaning |
|---|---|
| `yaml_dir` | Repo-relative directory holding the JJB YAML |
| `jobs` | Every job to publish, matching the `name:` inside the YAML |
| `stages` | Stage name to the jobs `run` triggers for it |
| `stage_order` | Producer stages before consumer stages |
| `consumes` | Consumer stage to its producer stage. Optional |

Keep `jobs` complete: a published job referencing a missing sibling refuses
the publish.

### Changing a set after a publish

Editing the sets file changes the next command, never what is already
published:

| You change | What happens | The way forward |
|---|---|---|
| Add a job to the set | `run` refuses, the folder does not carry it | `up --set <set> --update` republishes with the new list |
| Edit the set, branch tip unmoved | `sync` reports nothing to sync, it reacts to commits only | `up --set <set> --update` |
| Edit the set AND push commits | `sync` republishes at the new tip with the current set definition, both land | nothing extra |
| Delete or rename the entry | `run` and `sync` refuse naming the missing set | restore the entry, or point `--sets` at the right file |
| Break the file's JSON | `status`, `down`, `list` and `reap` keep working from the markers | fix the file when you next need `run` or `sync` |

## Two previews side by side

Folder names embed the owner and the slug, so distinct names publish side
by side:

```bash
jenkins-preview up --set pxb-8.1 --allow-foreign-fetch --no-root-view --name cand-a
jenkins-preview up --set pxb-8.1 --allow-foreign-fetch --no-root-view --name cand-b
jenkins-preview list
```

```
folder                                       owner              set        created              sha            tab
preview-nogueiraanderson-cand-a              nogueiraanderson   pxb-8.1    2026-08-13T22:22:03  81e16786ea62   no
preview-nogueiraanderson-cand-b              nogueiraanderson   pxb-8.1    2026-08-13T22:22:17  81e16786ea62   no
preview-nogueiraanderson-pxb-8.1-preview-docs-session nogueiraanderson   pxb-8.1    2026-08-13T22:21:48  81e16786ea62   yes
```

## Pin an exact commit

```bash
jenkins-preview up --set pxb-8.1 \
  --repo https://github.com/nogueiraanderson/jenkins-pipelines \
  --ref 81e16786ea621a27cd0aaf999b593cddec571e42 \
  --allow-foreign-fetch --no-root-view --name pin-demo
```

```
...
ref    81e16786ea621a27cd0aaf999b593cddec571e42 -> 81e16786ea621a27cd0aaf999b593cddec571e42 (anchor refs/heads/preview-docs-session)
...
pinned    81e16786ea621a27cd0aaf999b593cddec571e42
```

Accepted when a branch or tag resolves to that commit (the anchor). A commit
nothing points at is refused with instructions to push an anchor first. A
pull-request ref (`refs/pull/N/head`) is refused too: build agents fetch
branches and tags only, so preview the PR's source branch instead.

## Preview a branch of the canonical repo

The `--repo` flag does not have to point at a personal fork. Pinning the
canonical repo itself works the same way:

```bash
jenkins-preview up --set pxb-8.1 \
  --repo https://github.com/Percona-Lab/jenkins-pipelines --ref master \
  --no-root-view --name canon-master
```

```
set    pxb-8.1 (6 jobs)
ref    master -> 5141824597333cac10d6806fe0edcae74abc9124 (anchor refs/heads/master)
...
gates  all pre-upload gates passed for 6 jobs
create preview-nogueiraanderson-canon-master
...
pinned    5141824597333cac10d6806fe0edcae74abc9124
```

No acknowledgment was needed: the scripts' own fetches of
`Percona-Lab/jenkins-pipelines` no longer leave the pinned repo, so nothing
blocks ([gates.md](gates.md)). They still fetch the ref the script names,
not the pin.

## Inspect before publishing

```bash
jenkins-preview up --set pxb-8.1 --dry-run --allow-foreign-fetch
```

```
...
gates  all pre-upload gates passed for 6 jobs

DRY RUN, nothing was created. First rendered config:
--- percona-xtrabackup-8.1-compile-param ---
<matrix-project>
  ...
```

## Clean up

```bash
jenkins-preview down preview-nogueiraanderson-pxb-8.1-preview-docs-session
```

```
deleted view preview-nogueiraanderson-pxb-8.1-preview-docs-session
deleted folder preview-nogueiraanderson-pxb-8.1-preview-docs-session
clean
```

Teardown refuses running builds without `--force`, refuses anything without
this tool's marker, and refuses previews owned by someone else. After tearing
everything down, `list` prints `no previews`. For the rest:

```
$ jenkins-preview reap --older-than 7 --dry-run
keep   preview-nogueiraanderson-cand-a (0d old)
...
0 reaped
```

Without `--dry-run`, `reap` deletes what it lists, tabs included.

## Pass parameters to a stage

From a two-job smoke set on branch `preview-param-smoke`: `smoke-echo-param`
declares a string `CLOUD`, a boolean `FLAG` and a choice `PICK`, `smoke-plain`
declares nothing. `-p` needs an explicit `--stage`:

```bash
jenkins-preview run preview-nogueiraanderson-param-smoke-preview-param-smoke \
  --stage one -p 'CLOUD=AWS café &=+ probe' -p PICK=beta --yes
```

```
stage  one
builds 1: smoke-echo-param
param  CLOUD=AWS café &=+ probe
param  PICK=beta
default 1 other declared parameters keep their defaults
cost   each build provisions its own worker, plus a shared launcher. Expect up to 2 machines.
queued smoke-echo-param

watch  jenkins-preview status preview-nogueiraanderson-param-smoke-preview-param-smoke
then   jenkins-preview run preview-nogueiraanderson-param-smoke-preview-param-smoke --stage two   (once this stage is green)
```

Values arrive exactly as passed, and omitted parameters keep their defaults.
The `then` hint carries parameters forward only when the next stage's jobs
declare them. Each of the following is refused before anything is triggered:

```
ERROR: smoke-echo-param does not declare parameter 'TYPO' (declared: CLOUD, FLAG, PICK)
  fix: Jenkins would silently drop it. Fix the name, or start the job from the preview's UI

ERROR: stage 'two' includes smoke-plain, which declares no parameters
  fix: -p is all-or-nothing per stage. Split the stage in the sets file, or start the job from the preview's UI. A Jenkinsfile parameters{} block only registers after the job's first build

ERROR: smoke-echo-param parameter 'FLAG' is boolean, got 'yes'
  fix: Jenkins reads anything but 'true' as false, silently. Pass true or false

ERROR: smoke-echo-param parameter 'PICK' accepts alpha, beta, got 'zzz'
  fix: pick one of the declared choices
```
