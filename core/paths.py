"""Where the app's non-code resources live, independent of how it was packaged.

In a py2app bundle the modules live inside Contents/Resources/lib/python312.zip,
so `dirname(__file__)` is a path INTO a zip archive and no data file resolved
against it ever exists. py2app sets RESOURCEPATH to Contents/Resources, where
`data_files` (delegate-harness.md, settings.html) are shipped; in dev that var is
unset, so fall back to the repo root — the directory that holds `core/`.
"""
import os

CORE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CORE_DIR)

# Evaluated once at import, exactly as the old tools.py / settings.py did.
RESOURCE_DIR = os.environ.get("RESOURCEPATH") or PROJECT_ROOT
