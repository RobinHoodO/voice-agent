#!/usr/bin/env python3
"""py2app entry point for Thrivbe Voice.

The app itself is `mac/agent.py`; this file exists because py2app's `APP` must be a
loose script, and because a bundled script runs as `__main__` — not as part of a
package — so it cannot use package-relative imports of its own. It does one thing:
put the bundle's Resources directory (this file's own directory, which holds `core/`
and `mac/`) at the front of sys.path, then hand off.

It is deliberately NOT called agent.py: py2app's modulegraph keys modules by basename,
so a boot script named agent.py is registered as an alias for `mac.agent` and neither
ends up in the bundle — the built app then fails at launch with `No module named mac`.
"""
import os
import sys

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
except Exception:
    pass

from mac.agent import main  # noqa: E402

if __name__ == "__main__":
    main()
