"""Publish throwaway copies of Jenkins job sets pinned to your own fork.

Everything the tool writes lives inside one Jenkins folder, /previews, and
gate G1 confines every write to it. A pipeline loaded from a fork still
chooses its own agent label and credentials, so the folder boundary is
isolation for trusted code only.
"""

__version__ = "0.8.2"

PREVIEW_ROOT = "previews"
"""Top-level Jenkins folder that every write must land inside (gate G1)."""

MARKER = "jenkins-preview:v1"
"""Ownership marker written into a preview folder's description (gate G9)."""
