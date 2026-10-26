"""``python -m canopener`` starts the desktop GUI; ``python -m canopener.cli`` the command line."""

import os
import sys

if getattr(sys.modules.get("canopener"), "__file__", None) is None:
    # Started in a folder holding a "canopener" folder (from the home folder, where the logs folder
    # ~/canopener lives): that folder was imported instead of this package. Start again from the
    # folder the package is in. (Menu entries made before 0.2.0 start from the home folder.)
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.execv(sys.executable, [sys.executable, "-m", "canopener", *sys.argv[1:]])

from .gui.app import main  # noqa: E402  (after the check above, which must run first)

sys.exit(main())
