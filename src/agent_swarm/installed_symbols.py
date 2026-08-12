"""Refuse code that imports a symbol the INSTALLED copy of a package does not have.

EXTRACTED FROM motronics' `scripts/repo/check_installed_symbols.py`, 2026-08-12. That file checked
ONE package by name; the mechanism never needed to know which, so here the package, its distribution
name and the reinstall command are all REQUIRED ARGUMENTS with no defaults.

THE MEASURED DEFECT, 2026-08-12. A 408-line test file was committed that had never once been able to
import: the installed copy of the sibling package sat at one commit while the symbol it named landed
in a later one. Nothing on that box could have said otherwise, because the file was written against
the SOURCE of a sibling repository and judged against the INSTALL of it.

WHY IT IS STRUCTURAL AND NOT A SLIP. A non-editable git dependency does not see the sibling working
tree at all, so publication is three acts in a fixed order -- push, reinstall, and only then can the
consumer import it. Every cross-repo change pays that, forever, which is why the check is the
deliverable and the one fix was not. A step that must be remembered on every change is a step that
will be forgotten on some change, and the failure it produces -- an ImportError at collection -- is
the one that takes a test worker down and turns a whole run INCONCLUSIVE.

WHY IT PINS NOTHING. The remedy is always "update the install", never "freeze the tree away from the
sibling". This reads the installed revision only to PRINT it, so a reader learns which copy
answered; it never compares it to a declared one.

SHAPE: pure functions over a list of files, plus a `main` that gets the list from git. The purity is
what lets a test PLANT a file importing a symbol that does not exist and drive the real resolver,
rather than re-deriving it.
"""

from __future__ import annotations

import argparse
import ast
import json
import shutil
import subprocess
import sys
import warnings
from collections.abc import Iterable
from importlib import import_module, metadata
from importlib.util import find_spec
from pathlib import Path

#: The operator's git via PATH -- resolved, never a guessed absolute path.
_GIT = shutil.which('git') or 'git'


def installed_revision(distribution: str) -> str:
    """The commit the installed copy came from (PEP 610), or a phrase saying it cannot be read.

    FOR THE MESSAGE ONLY. Nothing here compares it to anything: a reader of a refusal needs to know
    WHICH copy answered, and that is a different question from whether it is the right one.

    The DISTRIBUTION name is asked for rather than the module name, because the two are spelled
    differently often enough that reading provenance off the module would raise
    `PackageNotFoundError` and be reported as "no provenance".
    """
    try:
        dist = metadata.distribution(distribution)
    except metadata.PackageNotFoundError:
        return 'NOT INSTALLED'
    text = dist.read_text('direct_url.json')
    if not text:
        return 'no PEP 610 provenance (installed from an index or a path)'
    try:
        return str(json.loads(text).get('vcs_info', {}).get('commit_id', 'unknown'))
    except (ValueError, AttributeError):
        return 'unreadable direct_url.json'


def requested(source: str, *, package: str) -> list[tuple[str, str | None]]:
    """`(module, name-or-None)` for every import in `source` rooted at `package`.

    NESTED IMPORTS COUNT. A function-body import fails just as hard, one call later, and a check that
    only read module level would pass on the lazy-import spelling that heavy optional dependencies
    are legitimately written with.
    """
    # `ast.parse` raises the compiler's own SyntaxWarnings (a stray `\w` in a non-raw string, say)
    # against files this check has no opinion about, and they arrive labelled `<unknown>:5`, which
    # names neither the file nor a defect anyone reading THIS output can act on. Suppressed here
    # rather than fixed here: that warning belongs to whoever owns the file, and a linter has it.
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', SyntaxWarning)
        tree = ast.parse(source)
    wanted: list[tuple[str, str | None]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            wanted.extend((alias.name, None) for alias in node.names if alias.name.split('.')[0] == package)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ''
            if node.level == 0 and module.split('.')[0] == package:
                wanted.extend((module, alias.name) for alias in node.names)
    return wanted


def _resolves(module: str, name: str | None) -> bool:
    """Can this exact import statement succeed against the INSTALLED copy?

    A `from X import Y` is answered by IMPORTING X, because `Y` may be an attribute rather than a
    submodule and no static read of the installed tree can tell the two apart. That is the point:
    the question is whether the import works, and the only instrument that answers it is the import.
    """
    try:
        if find_spec(module) is None:
            return False
    except (ImportError, ValueError):
        return False
    if name is None:
        return True
    try:
        return hasattr(import_module(module), name) or find_spec(f'{module}.{name}') is not None
    except (ImportError, ValueError, AttributeError):
        return False


def unresolvable_imports(files: Iterable[Path], *, package: str) -> dict[str, list[str]]:
    """Map file -> the import statements the installed copy cannot satisfy. Empty means safe.

    A file that cannot be parsed contributes NOTHING and is not an error here: a syntax error is a
    different defect with a louder guard, and reporting it twice would make this check noisy about
    something it does not own.
    """
    broken: dict[str, list[str]] = {}
    for path in files:
        try:
            source = Path(path).read_text(encoding='utf-8', errors='replace')
            wanted = requested(source, package=package)
        except (OSError, SyntaxError, ValueError):
            continue
        missing = [
            f'from {module} import {name}' if name else f'import {module}'
            for module, name in wanted
            if not _resolves(module, name)
        ]
        if missing:
            broken[str(path)] = sorted(set(missing))
    return broken


def _git_files(args: list[str], repo: Path) -> list[Path]:
    out = subprocess.run([_GIT, *args], cwd=repo, capture_output=True, text=True, check=True, timeout=120).stdout
    return [repo / line for line in out.splitlines() if line.endswith('.py') and (repo / line).exists()]


def main(argv: list[str], *, package: str, distribution: str, reinstall_command: str) -> int:
    """Check the index (or, with `--all`, every tracked file) and refuse with exit 1 on a miss.

    `reinstall_command` is the consumer's own install line and is REQUIRED: a refusal whose remedy
    this package invented would send the reader to a command their project does not use.
    """
    parser = argparse.ArgumentParser(description=(__doc__ or '').splitlines()[0])
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument(
        '--all',
        action='store_true',
        help='every TRACKED .py rather than the index -- what a suite asks, since a file that was '
        'committed before this check existed is still broken today',
    )
    args = parser.parse_args(argv)

    repo = args.repo.resolve()
    files = _git_files(['ls-files'] if args.all else ['diff', '--cached', '--name-only'], repo)
    broken = unresolvable_imports(files, package=package)
    if not broken:
        sys.stdout.write(f'[installed-symbols] OK -- every {package} import resolves against ')
        sys.stdout.write(f'{installed_revision(distribution)}.\n')
        return 0

    sys.stdout.write(f'[installed-symbols] REFUSED -- the INSTALLED {package} is ')
    sys.stdout.write(f'{installed_revision(distribution)} and lacks:\n')
    for path, imports in sorted(broken.items()):
        sys.stdout.write(f'  {path}\n')
        for statement in imports:
            sys.stdout.write(f'      {statement}\n')
    sys.stdout.write(f'\nIf the symbol exists in the {distribution} working tree, it is not PUBLISHED here yet:\n')
    sys.stdout.write(f'  1. git push                       # in {distribution}\n')
    sys.stdout.write(f'  2. {reinstall_command}\n')
    sys.stdout.write('Do NOT pin a revision to make this pass -- the fix is always to update the install.\n')
    return 1
