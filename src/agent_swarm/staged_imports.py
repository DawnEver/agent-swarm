"""Refuse a commit whose staged files import a module git does not have.

EXTRACTED FROM motronics' `scripts/repo/check_untracked_imports.py`, 2026-08-12. The measurement is
that project's; the mechanism is any repository's, and this is it.

Measured 2026-07-31, twice in one fan-out session: a lane created a new module, its importers were
already staged by someone else, and the module itself sat untracked. Committing the index as it
stood would have shipped an ``ImportError`` on any clean checkout while every local test stayed
green -- the failure is invisible on the machine that has the file.

Why a tool rather than a rule: ``git status`` shows a coordinator a wall of ``??`` with no signal
about which of those files something else already imports. The LANE knows which of its files are
new; the coordinator, who owns the index, does not. That asymmetry is not fixable by care, so it is
checked instead.

THE IMPORTABLE ROOTS ARE A REQUIRED ARGUMENT. Which directories hold importable code is a LAYOUT,
and a default of `src/` would silently report clean on a repository that keeps its package
elsewhere -- the exact "a guard that looked nowhere" failure this package refuses to ship.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

#: The operator's git via PATH -- resolved, never a guessed absolute path.
_GIT = shutil.which('git') or 'git'


def _git(args: list[str], repo: Path) -> list[str]:
    out = subprocess.run([_GIT, *args], cwd=repo, capture_output=True, text=True, check=True, timeout=120).stdout
    return [line for line in out.splitlines() if line]


def untracked_modules(repo: Path, *, importable_roots: Sequence[str]) -> dict[str, str]:
    """Map dotted module path -> repo-relative path, for untracked importable modules.

    A new TEST file is not imported by anything, so its absence is a lost safety net rather than a
    broken checkout: a DIFFERENT defect with a different remedy, which is why `importable_roots`
    names only the trees whose new modules something can import. Widening it to every new file makes
    the output noisy enough to be ignored, which is how a guard stops being consulted at all.
    """
    roots = tuple(importable_roots)
    modules: dict[str, str] = {}
    for path in _git(['ls-files', '--others', '--exclude-standard'], repo):
        if not path.endswith('.py') or not path.startswith(roots):
            continue
        parts = Path(path).with_suffix('').parts[1:]  # drop the root anchor
        if parts[-1] == '__init__':
            parts = parts[:-1]
        modules['.'.join(parts)] = path
    return modules


def _importers(module: str, candidates: list[str], repo: Path) -> list[str]:
    """Files among ``candidates`` that import ``module`` (as a module or a from-import)."""
    leaf = module.rsplit('.', 1)[-1]
    pattern = re.compile(
        rf'^\s*(?:from\s+{re.escape(module)}\b|import\s+{re.escape(module)}\b'
        rf'|from\s+{re.escape(module.rsplit(".", 1)[0])}\s+import\s+[^\n]*\b{re.escape(leaf)}\b)',
        re.MULTILINE,
    )
    hits = []
    for candidate in candidates:
        text = (repo / candidate).read_text(encoding='utf-8', errors='ignore')
        if pattern.search(text):
            hits.append(candidate)
    return hits


def dangling_imports(repo: Path, *, importable_roots: Sequence[str]) -> dict[str, list[str]]:
    """Map untracked module path -> staged files importing it. Empty means safe to commit."""
    modules = untracked_modules(repo, importable_roots=importable_roots)
    if not modules:
        return {}
    staged = [p for p in _git(['diff', '--cached', '--name-only'], repo) if p.endswith('.py')]
    staged = [p for p in staged if (repo / p).exists()]
    dangling = {}
    for module, path in modules.items():
        importers = _importers(module, staged, repo)
        if importers:
            dangling[path] = importers
    return dangling


def main(argv: list[str], *, importable_roots: Sequence[str]) -> int:
    """Report, and refuse with exit 1, every untracked module a staged file already imports."""
    parser = argparse.ArgumentParser(description=(__doc__ or '').splitlines()[0])
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    args = parser.parse_args(argv)

    dangling = dangling_imports(args.repo.resolve(), importable_roots=importable_roots)
    if not dangling:
        sys.stdout.write('[untracked-imports] OK -- no staged file imports an untracked module.\n')
        return 0
    sys.stdout.write('[untracked-imports] REFUSED -- these modules are UNTRACKED but already imported:\n')
    for path, importers in sorted(dangling.items()):
        sys.stdout.write(f'  {path}\n')
        for importer in importers:
            sys.stdout.write(f'      imported by {importer}\n')
    sys.stdout.write('\nCommitting now ships an ImportError on a clean checkout. Run:\n')
    sys.stdout.write('  git add ' + ' '.join(sorted(dangling)) + '\n')
    return 1
