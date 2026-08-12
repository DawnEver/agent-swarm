"""Resolving WHICH REPOSITORY a fleet serves, without this package ever naming one.

THE DEFECT THIS IS SHAPED BY, and it is this package's own. `forge.default_forge` used to carry
`DEFAULT_REPO = 'Tianjie-Zou-Team/motronics-studio'`, so every caller that omitted the argument
scheduled that one project and nothing ever revealed it -- the coupling was invisible BECAUSE the
default worked. Deleting the constant made `repo` required and pushed the answer out to consumers,
which was right. What it did NOT do was give them anywhere to put the RESOLUTION, so the first
consumer wrote it, and the second one would have copied it: environment override, declared value,
and the refusal when the declaration disagrees with `origin`. MEASURED 2026-08-12 in motronics:
139 lines, of which ZERO named that project.

WHAT IS HERE AND WHAT IS NOT. Here: the decision -- precedence, and the origin cross-check. Not
here: WHERE a consumer's policy file lives and what it is called. That is a project fact and it
stays with the project, which is why this takes a MAPPING that somebody else parsed rather than a
path it opens. The package's own rule -- "this layer decides; it does not reach" -- is what draws
that line, and it draws it in the useful place: a consumer reading TOML, JSON or a database answers
the same question with the same code.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping

#: RESOLVED ONCE via PATH. `['git', ...]` is a partial executable path, and resolving it is the
#: honest answer rather than suppressing the finding: `shutil.which` consults the operator's own
#: PATH, so nothing is pinned to one install.
_GIT = shutil.which('git') or 'git'


class RepoUndeclared(RuntimeError):
    """Nothing names the repository this fleet serves, and there is NO DEFAULT on purpose.

    A default repository is how a fleet silently serves the wrong project -- it works, so nobody
    finds out, until the day it writes to somebody else's issue tracker.
    """


class RepoDisagreesWithOrigin(RuntimeError):
    """The declared repository is not the one `origin` points at.

    WORK WOULD BE CLAIMED IN ONE REPOSITORY AND ITS OBJECTS FETCHED FROM ANOTHER, and the symptom is
    an unfetchable candidate -- which reads as a retention or network problem rather than as a
    misconfiguration, so it is refused here instead.
    """


def resolve_repo(
    declared: str | None,
    *,
    env_var: str,
    root: str | None = None,
    origin: str | None = None,
) -> str:
    """`OWNER/NAME` for this fleet. Environment first, then the declaration, then the cross-check.

    THE TWO MECHANISMS ARE KEPT TOGETHER BECAUSE EACH CLOSES THE OTHER'S HOLE, and two independent
    fixes for the missing `repo` argument once landed side by side, one declaring it and one
    deriving it from the remote:

    * DERIVING cannot fail loudly. An origin URL is a property of a CHECKOUT, so a fork, a mirror or
      a probe clone silently serves whatever it was cloned from, and the derivation always produces
      something plausible, so nothing raises.
    * DECLARING can DRIFT, into the disagreement above.

    So the declaration is the source of truth -- it is what a human writes and a tool must consult --
    and the remote is used only to REFUSE. Neither lane was wrong; each was right about a different
    failure, and keeping only one would have kept only one of the answers.

    Args:
        declared: the value a consumer read out of its own policy file. None or empty raises.
        env_var: the override's name, supplied by the consumer so this package invents no spelling.
        root: where to ask git. None skips the cross-check entirely.
        origin: the remote URL, for a caller that already has it or is testing. When None and `root`
            is given, git is asked.

    Raises:
        RepoUndeclared: nothing names it.
        RepoDisagreesWithOrigin: it disagrees with the remote.
    """
    if from_env := os.environ.get(env_var):
        return from_env
    if not declared:
        msg = (
            f'no repository is declared, and there is no default on purpose -- a default is how a '
            f'fleet silently serves the wrong project. Declare it, or export {env_var}=OWNER/NAME.'
        )
        raise RepoUndeclared(msg)
    url = origin if origin is not None else (_origin_url(root) if root is not None else None)
    if url:
        _refuse_if_origin_disagrees(str(declared), url, env_var=env_var)
    return str(declared)


def repo_from(policy: Mapping, *, env_var: str, root: str | None = None, origin: str | None = None) -> str:
    """`resolve_repo` over a parsed policy document's `[forge] repo`. The shape a TOML file becomes."""
    return resolve_repo(policy.get('forge', {}).get('repo'), env_var=env_var, root=root, origin=origin)


def _origin_url(root: str) -> str | None:
    """`git remote get-url origin`, or None.

    UNREADABLE ORIGIN IS NOT AN ERROR. A lane worktree or an air-gapped box legitimately has none,
    and refusing to schedule because git could not be asked would be unknown read as wrong.
    """
    out = subprocess.run(  # noqa: S603 -- resolved executable, fixed argv
        [_GIT, '-C', root, 'remote', 'get-url', 'origin'],
        capture_output=True,
        text=True,
        check=False,
    )
    return out.stdout.strip() if out.returncode == 0 else None


def _refuse_if_origin_disagrees(declared: str, url: str, *, env_var: str) -> None:
    path = url.strip().rsplit('@', 1)[-1].split('://', 1)[-1]
    origin = '/'.join(path.split('/')[-2:]).removesuffix('.git')
    if origin and origin.lower() != declared.lower():
        msg = (
            f'policy declares repo = {declared!r} but origin points at {origin!r}. Work items would '
            f'be claimed in one repository and their objects fetched from another; fix the '
            f'declaration, the remote, or export {env_var} deliberately.'
        )
        raise RepoDisagreesWithOrigin(msg)
