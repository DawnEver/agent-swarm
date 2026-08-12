"""Every CALL of a named function, with its argument shape -- statically, via the parser.

EXTRACTED FROM motronics' `scripts/repo/call_sweep.py`, 2026-08-12, unchanged in substance: it named
no project even before the move, which is why it moved whole rather than splitting.

Answers the one question a text sweep cannot: which CALL does this keyword belong to? A grep finds
the token; it cannot attribute it, so a kwarg sitting four lines below its own call, in a file where
a DIFFERENT function legitimately still takes that kwarg, reads as a hit for the wrong function.
That is how a stale kwarg survived a sweep on 2026-08-05 and was reported as verified.

It also catches what "change the parameter type so misses raise" cannot: that check only reaches
sites that EXECUTE, so a stale kwarg in a skipped, xfailed or unselected test never raises. This is
static, so selection does not matter.

THE ROOTS ARE A REQUIRED ARGUMENT. `src` and `tests` are one layout among several, and a default
would make a caller with a different one sweep the wrong tree and report zero -- the silence that
reads as "clean".
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path


def call_sites(func_name: str, roots: list[str]) -> list[tuple[str, int, int, list[str | None]]]:
    """``(path, lineno, n_positional, kwarg_names)`` for every call of ``func_name``.

    A ``None`` in the kwarg list is a ``**kwargs`` unpacking -- the dict-forwarded case, which is
    reported rather than resolved, since its keys are not knowable statically.
    """
    out = []
    for root in roots:
        for path in sorted(Path(root).rglob('*.py')):
            try:
                tree = ast.parse(path.read_text(encoding='utf-8'))
            except (SyntaxError, UnicodeDecodeError):
                continue  # not our business; the linters own syntax
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else getattr(fn, 'attr', None)
                if name == func_name:
                    out.append((str(path), node.lineno, len(node.args), [k.arg for k in node.keywords]))
    return out


def main(argv: list[str], *, default_roots: list[str]) -> int:
    """Report every call site, flagging the forbidden shapes. Exit 1 if any site was flagged."""
    parser = argparse.ArgumentParser(description=(__doc__ or '').splitlines()[0])
    parser.add_argument('func')
    parser.add_argument('roots', nargs='*', help=f'trees to sweep (default: {" ".join(default_roots)})')
    parser.add_argument('--forbid', action='append', default=[], help='kwarg that must not appear')
    parser.add_argument('--max-positional', type=int, default=None, help='refuse more positional args than this')
    args = parser.parse_args(argv)

    roots = args.roots or list(default_roots)
    sites = call_sites(args.func, roots)
    bad = 0
    for path, lineno, n_pos, kws in sites:
        flags = []
        if args.max_positional is not None and n_pos > args.max_positional:
            flags.append(f'POSITIONAL past {args.max_positional}')
        flags += [f'FORBIDDEN kwarg {k!r}' for k in args.forbid if k in kws]
        if None in kws:
            flags.append('**kwargs unpacking -- keys not statically knowable, CHECK BY HAND')
        bad += bool(flags)
        shown = [k for k in kws if k is not None]
        note = f'  <-- {"; ".join(flags)}' if flags else ''
        sys.stdout.write(f'{path}:{lineno}  positional={n_pos} kwargs={shown}{note}\n')
    sys.stdout.write(f'\n{len(sites)} call site(s) of {args.func!r} under {roots}; {bad} flagged\n')
    return 1 if bad else 0
