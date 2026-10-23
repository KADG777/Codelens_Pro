"""Bounded atomic publication for Windows readers briefly holding a file open."""

import time
from pathlib import Path


def replace_report(temporary: Path, target: Path) -> None:
    for attempt in range(5):
        try:
            temporary.replace(target)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(.05 * (attempt + 1))
