"""Writing a file so that a crash cannot leave half of one. One spelling, used by two callers.

EXTRACTED RATHER THAN COPIED. `spool` learned this first and `item_index` needs exactly the same
thing; a second copy would be one scheme with two spellings, which this project treats as the defect
and the drifted copy as merely its symptom. It also breaks a real import cycle -- `spool` knows
about the forge, and `forge_store` must not have to import `spool` to write a file.

WHAT IT DOES AND DOES NOT BUY, stated here because everything that persists anything relies on it:
`kill -9` is covered completely -- the bytes are fsync'd and the swap into place is atomic, so no
reader ever sees a partial file. A POWER CUT additionally needs the directory entry to be durable,
and Windows has no directory fsync at all. See :data:`DIRECTORY_FSYNC_AVAILABLE`, which is `False`
there. Our runners are Windows, so that limit is live rather than theoretical.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Can this platform make a DIRECTORY entry durable? False on Windows, which has no directory
#: fsync at all. Exported rather than hidden inside the helper so that a caller deciding how much to
#: trust this spool can READ the answer instead of inferring it from a silent `except OSError`.
DIRECTORY_FSYNC_AVAILABLE = os.name != 'nt'

#: What a half-written file is called until it is whole. Readers must not glob for it.
SCRATCH_SUFFIX = '.tmp'


def _fsync_directory(directory: Path) -> None:
    """Make the directory entry itself durable -- ON PLATFORMS THAT HAVE THAT OPERATION.

    **ON WINDOWS THIS FUNCTION DOES NOTHING, and that is not a bug to be fixed here.** Windows
    exposes no directory fsync; `os.open` on a directory fails outright. The early return is
    deliberate and is named, because the alternative -- letting the call fall into a bare
    `except OSError: pass` -- would look like an attempt that happened to fail rather than an
    operation the platform does not have. A reader skimming for durability would see an fsync call
    and believe it.

    CONSEQUENCE, stated at the code rather than only in the module docstring: on Windows the spool
    survives `kill -9` completely (the file contents are fsync'd and `os.replace` is atomic) but a
    POWER CUT can lose the directory entry of a just-recorded verdict. Our runners are Windows, so
    this is a live limit and not a theoretical one.
    """
    if not DIRECTORY_FSYNC_AVAILABLE:
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes) -> None:
    """Write `path` so that no reader ever sees a partial one.

    PUBLIC because `item_index` needs exactly this and a second copy would be one scheme with two
    spellings -- the defect this project names first, with the drifted copy as merely its symptom.

    The scratch file carries a suffix that readers do not glob for, so a crash mid-write leaves
    litter rather than a truncated file -- and litter is not corruption, so it must not trip an
    alarm. Callers that scan a directory must therefore glob for their own suffix, never for `*`.
    """
    scratch = path.with_name(f'{path.name}{SCRATCH_SUFFIX}')
    with open(scratch, 'wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(scratch, path)
    _fsync_directory(path.parent)
