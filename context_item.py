"""Context menu: update the selected item, its media type, or all video."""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from resources.lib.entry import context_main


if __name__ == "__main__":
    context_main()
