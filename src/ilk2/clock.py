"""Clocks. Bounds use monotonic time, which does not advance while the Mac sleeps (docs/02 §2.10).

time.monotonic() on macOS is mach_absolute_time based and shared by every process on one boot,
so a value written by one process can be compared with another's on the same boot. The boot
time is stored next to it; values from a different boot are not comparable.
"""

from __future__ import annotations

import subprocess
import time
from functools import lru_cache


def wall() -> float:
    return time.time()


def mono() -> float:
    return time.monotonic()


@lru_cache(maxsize=1)
def boot_time() -> int:
    """kern.boottime seconds; changes only on reboot."""
    out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True,
                         check=True).stdout
    # "{ sec = 1760000000, usec = 123 } Mon Oct 13 ..."
    return int(out.split("sec =")[1].split(",")[0])
