"""``python -m canopener`` starts the desktop GUI; ``python -m canopener.cli`` the command line."""

import sys

from .gui.app import main

sys.exit(main())
