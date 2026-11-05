"""Use the same entry point as the installed Python console script."""

import sys

from reviewctl.cli import main

sys.exit(main())
