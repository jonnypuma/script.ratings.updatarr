"""Run a ratings update from Programs or from RunScript()."""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from resources.lib.entry import main


if __name__ == "__main__":
    main(sys.argv)
