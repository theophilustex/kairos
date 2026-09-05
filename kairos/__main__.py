"""Lets you run the app straight from a checkout: ``python3 -m kairos``."""

import sys

from kairos.app import main

if __name__ == "__main__":
    sys.exit(main())
