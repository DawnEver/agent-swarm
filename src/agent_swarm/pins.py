"""Pin a fact together with its INVALIDATION KEY, so re-reading it costs one call and can say STALE.

EXTRACTED FROM motronics' `scripts/repo/recall.py`, 2026-08-12. The measurement that produced it is
that project's and stays there; what is here is the mechanism, which never named a motor.

WHY AN INVALIDATION KEY AND NOT A SCRATCHPAD. A written fact does not invalidate itself when its
source changes, so a note is cheap and silently wrong -- the "declaration that lies" shape. Git has
known the answer the whole time: the blob hash. A pin that carries its sources' hashes is the one
shape that beats both a re-read (expensive, no drift signal) and a note.

A `--cmd` pin REQUIRES `--from`. A command's dependencies cannot be inferred, and a pin whose
sources were guessed would report FRESH on a stale value -- a lying declaration used to fix lying
declarations. Refusing is the only honest option, so it refuses.

THE STORE DIRECTORY IS A REQUIRED ARGUMENT AND HAS NO DEFAULT. Where a consumer keeps a cache is the
consumer's layout, and a default here would make every caller that omits it write into one project's
tree. `main` takes it, and so does every function that touches disk.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

#: The operator's git via PATH -- resolved, never a guessed absolute path.
_GIT = shutil.which('git') or 'git'


def blob_hash(path: Path) -> str | None:
    """``git hash-object`` of ``path``, or None if it cannot be hashed.

    The file's CONTENT hash, not its mtime: a touched-but-unchanged file is not a change, and a
    reverted edit is not one either. mtime says "something happened to this file", which is a
    different question and the one that produces false STALEs.
    """
    if not path.is_file():
        return None
    out = subprocess.run([_GIT, 'hash-object', '--', str(path)], capture_output=True, text=True, check=False)
    return out.stdout.strip() or None


def sources_state(sources: list[str]) -> dict[str, str | None]:
    """The content hash of every named source, keyed by the spelling the caller used."""
    return {source: blob_hash(Path(source)) for source in sources}


def drift(pin: dict) -> list[str]:
    """Sources whose content hash no longer matches what was pinned."""
    return [source for source, was in pin['sources'].items() if blob_hash(Path(source)) != was]


def _pin_path(store: Path, name: str) -> Path:
    return store / f'{name}.json'


def _read_lines(path: Path, lines: str) -> str:
    start, _, end = lines.partition(',')
    body = path.read_text(encoding='utf-8', errors='replace').splitlines()
    return '\n'.join(body[int(start) - 1 : int(end)])


def _cmd_pin(args: argparse.Namespace, store: Path) -> int:
    if args.file:
        source = Path(args.file)
        if not source.is_file():
            sys.stderr.write(f'[pins] {args.file} does not exist\n')
            return 1
        value = _read_lines(source, args.lines) if args.lines else source.read_text(encoding='utf-8')
        sources = [args.file]
    else:
        if not args.from_:
            # The refusal this tool would be dishonest without. See the module docstring.
            sys.stderr.write(
                "[pins] --cmd needs --from <paths>: a command's sources cannot be inferred, and a\n"
                '[pins] pin with guessed sources would answer FRESH about a value that is stale.\n'
            )
            return 1
        # `shell=True` IS THE FEATURE, not an oversight: `--cmd` is a shell command the operator
        # typed, pipes and all, and pinning its stdout is the whole point. It is never composed from
        # anything but that argument. No `noqa` rides along -- this repo does not select the `S`
        # rules, and a suppression for a rule that is off is a finding of its own (RUF100).
        out = subprocess.run(args.cmd, shell=True, capture_output=True, text=True, check=False)
        if out.returncode != 0:
            sys.stderr.write(f'[pins] the command failed (exit {out.returncode}); nothing pinned:\n{out.stderr}')
            return 1
        value = out.stdout
        sources = list(args.from_)

    store.mkdir(parents=True, exist_ok=True)
    _pin_path(store, args.name).write_text(
        json.dumps(
            {
                'name': args.name,
                'value': value,
                'sources': sources_state(sources),
                'cmd': args.cmd,
                'pinned_at': time.time(),
            },
            indent=2,
        ),
        encoding='utf-8',
    )
    sys.stdout.write(f'[pins] pinned {args.name!r} ({len(value)} chars, {len(sources)} source(s))\n')
    return 0


def _cmd_get(args: argparse.Namespace, store: Path) -> int:
    path = _pin_path(store, args.name)
    if not path.is_file():
        sys.stderr.write(f'[pins] no pin named {args.name!r} -- the `list` subcommand shows what there is\n')
        return 1
    pin = json.loads(path.read_text(encoding='utf-8'))
    changed = drift(pin)
    if changed:
        sys.stderr.write(f'[pins] STALE: {", ".join(changed)} changed since this was pinned.\n')
        if pin.get('cmd') and args.refresh:
            args.file, args.lines, args.from_ = None, None, list(pin['sources'])
            return _cmd_pin(args, store) or _cmd_get(args, store)
        sys.stderr.write('[pins] the value below is what was pinned; re-pin (or pass --refresh) before trusting it.\n')
    else:
        sys.stderr.write('[pins] FRESH: every source is byte-identical to when this was pinned.\n')
    sys.stdout.write(pin['value'])
    return 2 if changed and not args.refresh else 0


def _cmd_list(_args: argparse.Namespace, store: Path) -> int:
    if not store.is_dir():
        sys.stdout.write('[pins] nothing pinned\n')
        return 0
    for path in sorted(store.glob('*.json')):
        pin = json.loads(path.read_text(encoding='utf-8'))
        changed = drift(pin)
        age = (time.time() - pin.get('pinned_at', 0)) / 60.0
        state = f'STALE ({len(changed)} source(s) moved)' if changed else 'FRESH'
        sys.stdout.write(f'{pin["name"]:24s} {state:28s} {age:6.0f} min  {len(pin["value"])} chars\n')
    return 0


def main(argv: list[str], *, store: Path) -> int:
    """The `pin` / `get` / `list` command surface. `store` is REQUIRED and has no default."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='action', required=True)

    p_pin = sub.add_parser('pin', help='record a value together with its sources content hashes')
    p_pin.add_argument('name')
    p_pin.add_argument('--cmd', help='shell command whose stdout is the value (requires --from)')
    p_pin.add_argument('--file', help='file to read instead of running a command')
    p_pin.add_argument('--lines', help='START,END (1-indexed, inclusive) to slice --file')
    p_pin.add_argument('--from', dest='from_', nargs='+', help='the paths this value depends on')
    p_pin.set_defaults(func=_cmd_pin)

    p_get = sub.add_parser('get', help='print a pinned value and whether it is still FRESH')
    p_get.add_argument('name')
    p_get.add_argument('--refresh', action='store_true', help='re-run a stale --cmd pin instead of warning')
    p_get.set_defaults(func=_cmd_get)

    sub.add_parser('list', help='one line per pin: name, FRESH/STALE, age').set_defaults(func=_cmd_list)

    args = parser.parse_args(argv)
    return args.func(args, store)
