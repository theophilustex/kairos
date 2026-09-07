#!/usr/bin/env python3
"""Run the CalDAV diagnostic from a source checkout.

The same thing is built into the application, so anyone using the AppImage
can run it without a checkout::

    ./Kairos-x86_64.AppImage --diagnose https://dav.example.com/ you@example.com
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kairos.diagnostics import run  # noqa: E402

if __name__ == "__main__":
    sys.exit(run())
