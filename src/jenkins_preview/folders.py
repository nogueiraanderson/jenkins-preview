"""Preview folder and view paths, ownership markers, and config templates."""

import re
import secrets
from datetime import UTC, datetime
from xml.sax.saxutils import escape

from . import MARKER, PREVIEW_ROOT
from .names import slug_text

FOLDER_MARKER = f"{MARKER}:folder"
VIEW_MARKER = f"{MARKER}:view"

# The marker must start the description, so a folder that merely quotes it in prose
# is not mistaken for ours. Deleting the wrong folder is the failure this prevents.
_FOLDER_MARKER_RE = re.compile(rf"^{re.escape(FOLDER_MARKER)}\s")
_VIEW_MARKER_RE = re.compile(rf"^{re.escape(VIEW_MARKER)}\s")

# A folder config with an empty <views/> element is accepted on creation and then
# makes the folder's own api/json return HTTP 500. A tree= query that does not ask
# for views still works, which hides it. This block is copied from a known-good
# folder config rather than hand-written.
FOLDER_XML = """<?xml version='1.1' encoding='UTF-8'?>
<com.cloudbees.hudson.plugins.folder.Folder plugin="cloudbees-folder">
  <description>{description}</description>
  <properties/>
  <folderViews class="com.cloudbees.hudson.plugins.folder.views.DefaultFolderViewHolder">
    <views>
      <hudson.model.AllView>
        <owner class="com.cloudbees.hudson.plugins.folder.Folder" reference="../../../.."/>
        <name>All</name>
        <filterExecutors>false</filterExecutors>
        <filterQueue>false</filterQueue>
        <properties class="hudson.model.View$PropertyList"/>
      </hudson.model.AllView>
    </views>
    <tabBar class="hudson.views.DefaultViewsTabBar"/>
  </folderViews>
  <healthMetrics/>
  <icon class="com.cloudbees.hudson.plugins.folder.icons.StockFolderIcon"/>
</com.cloudbees.hudson.plugins.folder.Folder>
"""

# Explicit job paths rather than an includeRegex: the job set is known at publish
# time, so listing it is both narrower and free of any regex-injection surface.
VIEW_XML = """<?xml version='1.1' encoding='UTF-8'?>
<hudson.model.ListView>
  <name>{name}</name>
  <description>{description}</description>
  <filterExecutors>false</filterExecutors>
  <filterQueue>false</filterQueue>
  <properties class="hudson.model.View$PropertyList"/>
  <jobNames>
    <comparator class="hudson.util.CaseInsensitiveComparator"/>
{job_names}
  </jobNames>
  <jobFilters/>
  <columns>
    <hudson.views.StatusColumn/>
    <hudson.views.WeatherColumn/>
    <hudson.views.JobColumn/>
    <hudson.views.LastSuccessColumn/>
    <hudson.views.LastFailureColumn/>
    <hudson.views.LastDurationColumn/>
    <hudson.views.BuildButtonColumn/>
  </columns>
  <recurse>true</recurse>
</hudson.model.ListView>
"""


def folder_path(slug: str) -> str:
    """API path of a preview folder. Always inside the preview root (gate G1)."""
    return f"/job/{PREVIEW_ROOT}/job/{slug}"


def folder_name(user: str, slug: str) -> str:
    """Folder names embed the owner so previews are attributable and reapable.

    The user id is slugged here so an email-style Jenkins id composes into a
    valid name on the --name path too. The marker keeps the raw id, and the
    marker, never the folder name, is the authority on ownership.
    """
    return f"preview-{slug_text(user)}-{slug}"


def new_preview_id() -> str:
    """A 128-bit id that ties a folder and its view together."""
    return secrets.token_hex(16)


def swap_name(folder: str) -> str:
    """A temporary sibling name for the swap-protocol update.

    The replacement publishes fully under this name, then renames over the
    final one, so no failure between teardown and publish can leave zero
    previews. Fits the 64-character cap whatever the folder name is.
    """
    return f"{folder[:55]}-sw{secrets.token_hex(3)}"


def folder_marker(
    *,
    preview_id: str,
    user: str,
    job_set: str,
    repo: str,
    sha: str,
    anchor: str,
    view: str,
    foreign: int = 0,
    ref: str = "",
    blocking: int = 0,
    ackdigest: str = "",
) -> str:
    # ref is the ref as the operator typed it (or the inferred branch), so sync
    # can tell a deliberate exact-SHA pin from a branch to follow. blocking and
    # ackdigest record what an --allow-foreign-fetch actually acknowledged;
    # foreign stays the informational count of every disclosed fetch.
    tail = f" ref={ref}" if ref else ""
    tail += f" blocking={blocking}"
    if ackdigest:
        tail += f" ackdigest={ackdigest}"
    return escape(
        f"{FOLDER_MARKER} id={preview_id} owner={user} set={job_set} repo={repo} "
        f"anchor={anchor} sha={sha} view={view} foreign={foreign}{tail} "
        f"created={datetime.now(UTC).isoformat()}"
    )


def view_marker(*, preview_id: str, folder: str, user: str) -> str:
    return escape(
        f"{VIEW_MARKER} id={preview_id} owner={user} folder={PREVIEW_ROOT}/{folder} "
        f"created={datetime.now(UTC).isoformat()}"
    )


def _parse(description: str | None, pattern: re.Pattern[str]) -> dict[str, str]:
    if not description or not pattern.match(description):
        return {}
    fields: dict[str, str] = {}
    for token in description.split():
        key, _, value = token.partition("=")
        if value:
            fields[key] = value
    return fields


def parse_folder_marker(description: str | None) -> dict[str, str]:
    """Marker fields, or an empty dict when this folder is not ours."""
    return _parse(description, _FOLDER_MARKER_RE)


def parse_view_marker(description: str | None) -> dict[str, str]:
    """Marker fields, or an empty dict when this view is not ours."""
    return _parse(description, _VIEW_MARKER_RE)


def view_job_names(folder: str, jobs: list[str]) -> str:
    """Full relative paths, one per job, as ListView expects for a recursive view."""
    return "\n".join(
        f"    <string>{escape(f'{PREVIEW_ROOT}/{folder}/{job}')}</string>" for job in sorted(jobs)
    )


def job_description(*, job_set: str, sha: str, repo: str, branch: str) -> str:
    return (
        f"{MARKER} preview of {job_set} at {sha[:12]} from {repo} (branch {branch}). "
        "Temporary, safe to delete. Not a production job."
    )
