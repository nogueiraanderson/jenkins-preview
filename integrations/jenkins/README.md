# Jenkins admin jobs (the no-install path)

Self-service jobs that give any Jenkins user the tool without a local install.
Live on pxb.cd since 2026-08-19 under the root **Previews** tab:

| Item | Purpose |
|---|---|
| `create-preview.xml` | `previews/create-preview`: clone FORK_URL@BRANCH, publish SET as `preview-<service>-<NAME>`. The build installs the tool itself via `uvx --from git+...@main`, so runs always use current main. Falls back to a curated sets file when the fork has no `.jenkins-preview.json`. |
| `delete-preview.xml` | `previews/delete-preview`: teardown by NAME. |
| `previews-view.xml` | Root ListView `Previews`, recurses over `/previews`. |

## Contract

- Runs on an agent with git, curl and outbound https (`launcher-x64` on pxb).
- Auth: a folder-scoped username/password credential with id
  `jenkins-preview-service` inside `/previews` (a real Jenkins account plus a
  dedicated API token; the username is authenticated, it is not a label).
- All Jenkins-side writes attribute to that account. Who ran a preview is the
  build's "Started by user" record, so keep the jobs' build history.
- The curated fallback sets are pipelines-only on purpose: `*-param` and
  `*-multijob` names collide with live MultiJob phase references and the
  collision gate refuses them (see `src/jenkins_preview/multijob.py`).

## Develop and maintain

These XMLs are the canonical copies. To change a job: edit the XML here,
review, then push with `jenkins -i <inst> job update previews/<job> -c <file>`
and read back. To adopt on another master: create the `/previews` folder, the
credential, the view, then `job create` the two jobs. Keep this directory in
sync with live (fetch with `jenkins job config -o`); drift here means the live
job was hand-edited and needs review.
