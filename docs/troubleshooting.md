# Troubleshooting

Every refusal names its own fix. This page covers the ones with context worth
expanding.

## Setup

**`JENKINS_URL is not set`** (also `JENKINS_USER`, `JENKINS_TOKEN`)
Two sources, checked per field: the environment, then `~/.config/jenkins-preview/credentials.yaml`. Export the variable, or add the field to the file (layout in the README). The token comes from `/me/configure` on your Jenkins. The tool never prompts.

**`credentials.yaml is readable by group or others`**
The file holds a token: `chmod 600 ~/.config/jenkins-preview/credentials.yaml`.

**`credentials.yaml defines N servers and no 'current'`**
Add `current: <name>` to the file, or export `JENKINS_SERVER=<name>` for this shell. A single server needs neither.

**`server '<name>' is not in credentials.yaml`**
`JENKINS_SERVER` (or `current`) names a server the file does not define. The refusal lists the defined ones.

**`refusing a credential on the command line`**
An argument lands in `ps` output, shell history and CI logs. Use the environment variable.

**`JENKINS_USER must not contain whitespace or '='`**
Use the user ID from the top of `/me/configure`, not the display name. A crafted id could otherwise displace ownership-marker fields.

**`jenkins-jobs (JJB) not found in this environment`**
Run the tool through `uv run jenkins-preview` or `uv tool install`, not a bare `python`. The pinned renderer only exists inside the environment uv builds.

**Build fails cloning `git@github.com:...`**
The zero-flag path publishes your `origin` URL as the job's remote, and agents have no SSH key for your fork. Use an https origin, or pass `--repo https://github.com/<you>/<repo>` explicitly.

**`/previews folder does not exist`**
Ask an admin to create a top-level folder named `previews`, then re-run `doctor`.

## Drafting

**`no JJB job definitions found in this checkout`**
The checkout you stand in holds no job YAML anywhere, so it is probably the wrong repo (the tool's own clone, a dotfiles repo). cd into your fork checkout of the pipelines repo, on the branch under test, and rerun. `--discover` cannot help here: there is nothing in this checkout to discover.

**`this checkout holds N directories of job definitions`**
Neither your working directory nor your branch's edits single one out. Each candidate is listed as a runnable `sets --discover <yaml_dir>` line: run one, or cd into the directory you work on and rerun.

**`<dir> rendered zero jobs`**
No definition file in the directory renders, each failure listed with JJB's reason. Nested directories are not scanned, so name the directory holding the YAML itself, like `pxb/v2/jenkins`. An empty or freshly created directory gives the same error: check you stand inside your pipelines clone.

**`<file> escapes the yaml directory via a symlink`**
A symlinked definition could stage a file from anywhere on disk. Keep job definitions as plain files inside the directory.

**`uses a JJB !include tag, which the copy step cannot carry`**
The render stages a copy where the include target does not exist, so JJB would quietly drop the content. Inline it.

**`the files render alone but not together`**
Each file passed its own render probe, so two files define a clashing template, macro or defaults block.

## Inference

**`--set was not given and no sets file was found`**
No sets file exists anywhere in the lookup order. From inside your pipelines clone, draft one: `sets --example > .jenkins-preview.json`.

**`--set was not given and this checkout does not single one out`**
With several sets, inference picks the one whose `yaml_dir` your working directory sits under, else the one your branch edits. Neither narrowed it to one: pass `--set`, the message lists the choices.

**`no folder given and this is not a git checkout`**, or no current branch, or no origin remote
The folder is inferred by matching this checkout's branch and origin against the live preview markers. With any leg missing, pass the folder, `jenkins-preview list` shows them.

**`no preview of yours pins branch <branch> on this Jenkins`**
Inference matches owner, branch and origin repo, so a preview of another fork or another user never matches. Publish first with `up`, or pass the folder.

**`branch <branch> has N previews of yours`**
Several previews (a `--name` publish next to the derived one) pin the same branch. Pass the one you mean.

## Permissions

**`Jenkins refused the request (403)`**
You can read but not write. Ask for the permissions in the README's [Setup](../README.md#setup-on-the-jenkins-side) section inside `previews`, quoting your login. With GitHub-backed auth your Jenkins identity is your GitHub login.

**`Jenkins rejected the credentials (401)`**
The token is wrong or revoked. Regenerate it at `/me/configure`.

## Refs

**`commit <sha> is not reachable through any branch or tag tip`**
Jenkins cannot fetch an object nothing points at. Push a branch at that commit and pass the branch name.

**`ref '<name>' not found`**
The branch must exist on the fork you passed, not only locally. `git ls-remote <repo>` lists what the remote exposes.

**`ref '<name>' is a pull-request ref, not a branch or tag`**
`refs/pull/N/head` resolves on GitHub, but only branches and tags can anchor a preview. Preview the PR's source branch, with `--repo` pointing at its fork when the PR comes from one. A real branch that happens to be named `pull/N/head` still resolves.

## Rendering

**`JJB failed to render the job definitions at this ref`**
A template, macro or defaults block does not resolve at that commit. The last lines of the renderer's own error name the file. Fix the YAML on your branch, re-run `doctor`.

**`these jobs are not defined anywhere in <dir> at this ref`**
The set names a job your branch does not define. The set is stale, or the branch renamed the job.

**`defined more than once`** or **`duplicate key`**
The same job lives in two YAML files, or the sets file repeats a key. Either would silently pick a winner, so both are refused naming the offender.

## Publishing

**`<folder> already exists`**
Republish over it with `up ... --update`, which replaces it only after the new ref passes every gate. Or `down` it first.

**`<folder> was created concurrently by another publish`**
Two publishes raced the same name and the other won. Wait, then `--update` if it is yours, or pick another `--name`.

**`N build-time fetches would silently test code outside your fork`**
Gate G12. Point git fetches at your fork (`checkout scm`), add missing sibling jobs to the set, or acknowledge with `--allow-foreign-fetch` when that code is not what you are testing. The count stays visible in `status`.

**`N pipeline scripts do not exist at the pinned commit`**
The rendered `scriptPath` names a file your branch lacks, so the build would die at script load. No acknowledgment applies.

**`unsupported job type <...>`**
Only pipeline, freestyle, matrix and multijob configs can be proven pinned.

**A `gate G5` or `G6` message whose fix line reads `this is a bug in the tool`, e.g. `a remote-build token survived sanitising`**
A bug in the tool, not something to work around. Nothing was published. Report it with the job name. The other G5 refusals, like `unsupported job type` above, name their own fix and are yours to make.

**`rolled back the folder <name>` after an error**
The tool removed what it had created, verified the removal, and printed the underlying error. Rollback deletes only items carrying this run's own preview id. On an update, the old preview was still standing when this happened, because the replacement publishes fully under a sibling name before anything is destroyed.

**`renaming <temp> to <folder> did not complete`**
The old preview is gone, but the replacement is complete, verified and working under the sibling name the error printed. Rename it in the Jenkins UI, or `down` it and republish.

**A NOTE that `the teardown failed and so did the probe after it`**
The controller became unreachable mid-swap and the tool could not tell whether the old preview survived. Nothing was rolled back: the replacement is complete under its sibling name, and the old folder may stand next to it. When the controller is back, `jenkins-preview list`, then `down` whichever of the two is stale.

**`the preview at <folder> is published and working, only its tab failed`**
Everything but the root tab succeeded. Recreate the tab with `up --set <set> --update`, or live without it.

## Running

**`<job> refused the <endpoint> trigger with HTTP 400`**
The job's parameter definitions changed between the check and the trigger. Re-run, the endpoint is chosen fresh every time.

**`stage '<x>' is already building: <job>`**
One build per stage at a time, or every re-run doubles the cloud workers. Watch the running one with `status`.

**`stage '<x>' consumes '<y>', which has no successful build`**
Run the producer stage first. A consumer with no producer artifact does not fail, it waits out the stage timeout. On some families that is 240 minutes.

**`-p '<x>' is not KEY=VALUE`** or **`-p <k> given twice`**
Each `-p` sets one parameter, `NAME=value`, once. An empty value is fine (`NAME=`), a missing `=` or a repeat is refused before anything is sent.

**`<job> does not declare parameter '<k>' (declared: ...)`**
Jenkins silently drops a parameter the job does not declare, so the tool refuses instead. Fix the name, or start the job from the preview's UI. A `parameters {}` block inside the Jenkinsfile only registers after the job's first build: run the stage once without `-p`, then again with it.

**`stage '<x>' includes <job>, which declares no parameters`**
`-p` is all-or-nothing per stage. Split the stage in the sets file, or start that job from the preview's UI.

**`<job> declares '<k>' as Password...`**, or as Credentials or File
An argv value lands in `ps` and shell history, and a file cannot travel in a form body. Set the value in the preview's UI.

**`-p <k> value carries control characters`**
A control byte forges terminal output wherever the value is echoed back. Set such a value from the preview's UI.

**`<job> is not in the preview folder`**
The sets file names a job the published folder does not carry, usually because the set changed after the publish. Republish with `up --set <set> --update`, or point `--sets` at the file the preview was published from.

**`the new tip changes the blocking build-time fetches`**
`sync` inherits the acknowledgment only while the blocking fetches stay exactly what was acknowledged. Review the WARNING list, then `sync <folder> --allow-foreign-fetch`.

**`<folder> was pinned to the exact commit <sha>`**
A `--ref <sha>` publish is a deliberate pin, so `sync` never moves it. Move it yourself: repeat the original `up` command (keep `--repo` and `--name`), change `--ref`, add `--update`.

**`set '<name>' is not in your sets file`**
The preview is fine. Point `--sets` or `$JENKINS_PREVIEW_SETS` at the file that defines it.

**A build dies in about 300 ms with `couldn't find remote ref`**
Should be impossible through the tool, which forces heavyweight checkout. If seen, the config was edited after publication. Republish with `up ... --update`.

## Teardown

**`delete <folder>? [y/N]`**
A bare `down` deletes a name you never typed, so it asks once. `--yes` answers for you.

**`<folder> belongs to owner '<name>'`**
`down` deletes only your own previews. Age-based cleanup goes through `reap`, which sweeps every marked preview regardless of owner.

**`<folder> carries no ownership marker`**
The tool deletes only folders it created. If the description was edited, remove the folder through the Jenkins UI.

**`builds still running`**
Wait, or pass `--force`. Only `down` and `reap` have that flag: when `up --update` or `sync` hits this, `down --force` first, then republish. `sync` checks before it clones anything, so the refusal is immediate. Forcing while a build holds an agent leaves the agent to your cloud's reaper.

**A root view without its folder**
`down <name>` deletes an orphaned tab when the view carries this tool's own marker naming that folder. Anything unprovable is left alone.
