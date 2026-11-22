"""Entry point of the packaged download (PyInstaller): the command line, or the window without arguments."""

import sys

from arcane_patcher.cli import main

sys.exit(main())
