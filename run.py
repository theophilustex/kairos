#!/usr/bin/env python3
"""Run Kairos straight from a checkout, without installing it.

    ./run.py            start the app
    ./run.py --debug    ... with verbose logging

This is exactly equivalent to ``python3 -m kairos``; it exists so that the
project has an obvious "how do I run this" entry point.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kairos.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
