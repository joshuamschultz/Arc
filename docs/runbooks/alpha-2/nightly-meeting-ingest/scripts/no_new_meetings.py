"""Empty-night result for nightly-meeting-ingest.

Runs when filter_new finds nothing new. Deterministic and side-effect free, so
re-running it is always safe. It touches no file and no network, and it leaves
the watermark alone (that node is skipped with the archive branch).
"""

from __future__ import annotations

import json
import sys


def main() -> int:
    sys.stdout.write(json.dumps({"status": "nothing_new"}, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
