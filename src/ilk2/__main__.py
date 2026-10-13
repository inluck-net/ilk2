"""python -m ilk2

`supervise` starts the supervisor without importing the CLI: an upgrade drains only when code
the running supervisor loaded changed (docs/02 §2.7), and CLI changes are common.
"""

import sys

if sys.argv[1:2] == ["supervise"]:
    from ilk2.supervisor import main as supervise

    stop_after = None
    if "--stop-after" in sys.argv:
        stop_after = float(sys.argv[sys.argv.index("--stop-after") + 1])
    raise SystemExit(supervise(stop_after))

from ilk2.cli import main  # noqa: E402

raise SystemExit(main())
