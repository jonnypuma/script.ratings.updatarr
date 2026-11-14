"""Optional scheduled ratings update. Does nothing until the schedule is enabled."""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from resources.lib.entry import service_main


if __name__ == "__main__":
    service_main()
