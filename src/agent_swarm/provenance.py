"""Where an installed `agent_swarm` came from, read from the install itself.

WHY THIS EXISTS. `agent-swarm` is a dependency of motronics' gate, and **no motronics verdict records
which version of it decided the result.** A gate log names `tree=<motronics sha>` while a second,
unrecorded tree runs inside the same interpreter. That is the declaration-that-lies shape at the
level of an environment rather than a function: the report is true about what it mentions and silent
about what it omits.

`__version__` CANNOT ANSWER THIS. It is `0.1.0` on every commit so far, so two interpreters holding
completely different code report the same string. The only thing that distinguishes them is what
pip wrote at install time -- `direct_url.json` in the `.dist-info` -- which records the source and,
crucially, whether the install is EDITABLE.

WHAT AN EDITABLE INSTALL WOULD MEAN HERE, and why it is worth refusing rather than documenting: an
editable install makes the interpreter follow a working tree. Every uncommitted edit in this repo
would then be live inside every gate on that box, and a verdict would be decided partly by code that
exists in no commit and can be reproduced by nobody.

WHAT THIS DOES NOT COVER, said plainly because a check that oversells itself is worse than none:
**it does not cover the failure that actually occurred.** A non-editable reinstall landing in the
middle of a gate was enough to change the interpreter under a running verdict -- no editable install
was involved. Refusing editable installs closes one door in a room with two. The other door is
timing, and only an install-time interlock or a provenance line in the gate log can close it.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Provenance:
    """What an installed distribution says about its own origin.

    `direct_url` is kept as RAW TEXT as well as parsed, because a gate log should print what pip
    wrote rather than this module's interpretation of it -- a summary is a place for a bug to hide,
    and the verbatim line is the thing another engineer can compare against their own box.
    """

    dist_info: Path
    direct_url_text: str | None
    editable: bool
    url: str | None

    @property
    def recorded(self) -> bool:
        """Did the install record its origin at all? A wheel from an index does not."""
        return self.direct_url_text is not None


def read_provenance(site_packages: Path, distribution: str = 'agent_swarm') -> Provenance | None:
    """What `site_packages` holds for `distribution`, or ``None`` if it is not installed there.

    ``None`` MEANS NOT INSTALLED, and callers must not read it as "installed and fine". This returns
    a value rather than raising because "absent" is a legitimate answer -- a source checkout running
    with `PYTHONPATH=src` has no dist-info at all -- and the caller decides whether that is
    acceptable in its context.
    """
    matches = sorted(site_packages.glob(f'{distribution.replace("-", "_")}-*.dist-info'))
    if not matches:
        matches = sorted(site_packages.glob(f'{distribution.replace("_", "-")}-*.dist-info'))
    if not matches:
        return None
    dist_info = matches[-1]
    direct_url = dist_info / 'direct_url.json'
    if not direct_url.is_file():
        return Provenance(dist_info=dist_info, direct_url_text=None, editable=False, url=None)

    text = direct_url.read_text(encoding='utf-8')
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Unparseable is NOT "not editable". Reporting a corrupt provenance record as a clean
        # install is the one answer this function must never give.
        return Provenance(dist_info=dist_info, direct_url_text=text, editable=True, url=None)
    return Provenance(
        dist_info=dist_info,
        direct_url_text=text,
        editable=bool(parsed.get('dir_info', {}).get('editable', False)),
        url=parsed.get('url'),
    )


def running_provenance() -> str:
    """One line describing where the `agent_swarm` in THIS interpreter came from.

    WHY THIS IS A FUNCTION AND NOT A DISCIPLINE. A wall-clock figure measured against a working tree,
    quoted next to an interpreter pinned three commits behind it, is a number that LOOKS reproducible
    and is not -- the same defect as an install landing mid-gate, pointed at a number instead of a
    verdict. The instruction that follows from it is "always quote the two together", and an
    instruction I have to remember is one I will eventually not.

    So every timing this suite prints calls this, and the qualification travels with the figure
    instead of with the person who measured it. A number that cannot say where its code came from
    should not be quoted, and now it cannot be printed without saying.

    It names the SOURCE tree and its sha when running from a checkout, and the recorded install
    otherwise -- and it says `dirty` when the tree has uncommitted changes, because that is precisely
    the state in which a figure is unreproducible by anyone else.
    """
    module_dir = Path(__file__).resolve().parent
    installed = read_provenance(module_dir.parent)
    if installed is not None and installed.recorded:
        return f'agent_swarm INSTALLED: {installed.direct_url_text}'

    repo = module_dir.parent.parent
    try:
        sha = subprocess.run(
            ['git', '-C', str(repo), 'rev-parse', '--short', 'HEAD'],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        ).stdout.strip()
        dirty = subprocess.run(
            ['git', '-C', str(repo), 'status', '--porcelain'],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):  # pragma: no cover -- git absent is not a failure here
        return f'agent_swarm from SOURCE {module_dir} (sha unknown)'
    if not sha:
        return f'agent_swarm from SOURCE {module_dir} (not a git checkout)'
    return f'agent_swarm from SOURCE {module_dir} @ {sha}{" DIRTY" if dirty else ""}'
