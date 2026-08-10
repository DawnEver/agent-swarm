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
