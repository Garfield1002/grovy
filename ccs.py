#!/usr/bin/env python3
"""Pre-rename alias of grovy.py. Claude sessions started before the rename call `.../ccs.py hook NAME`
from their --settings file; keep this until none of those sessions is running."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from grovy import *  # noqa: F401,F403,E402
from grovy import main  # noqa: E402

if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main())
