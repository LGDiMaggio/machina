"""Replace a file's contents atomically, through a temp file of its own.

Connectors that write their data files back (the Excel/CSV connector's
work-order sheets) use :func:`replace_atomically` so a crash, a power loss
or a concurrent write never leaves a truncated or interleaved file.
"""

from __future__ import annotations

import contextlib
import os
import stat
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def replace_atomically(path: Path, write: Callable[[Path], None]) -> None:
    """Write ``path`` through a temp file of its own, then atomically replace it.

    The temp file has a unique name, so concurrent or abandoned writes never
    share one. It takes the target's permission bits (a new file gets the
    umask default), is flushed to disk before the replace, and the directory
    entry is flushed after it, so neither a crash nor a power loss leaves a
    truncated file. A symlink is written through, not replaced.
    """
    target = path.resolve()
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    os.close(os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666))
    try:
        write(tmp)
        with contextlib.suppress(FileNotFoundError):
            os.chmod(tmp, stat.S_IMODE(target.stat().st_mode))
        with tmp.open("rb+") as f:
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_directory(target.parent)


def _fsync_directory(directory: Path) -> None:
    """Flush a directory entry change to disk (a no-op where unsupported, e.g. Windows)."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        with contextlib.suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)
