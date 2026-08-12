#!/usr/bin/env python
"""Run this repository's own `agent_swarm.staged_imports` check against this repository.

The mechanism lives in `src/agent_swarm/staged_imports.py` and is written for any repository;
`main()` therefore takes `importable_roots` as a REQUIRED keyword, because which directories hold
importable code is a layout, not a default. This file supplies agent-swarm's answer -- `src` -- and
nothing else. It exists so the check has a RUNNER: the module's tests build a synthetic repo under
`tmp_path`, so they prove the logic and say nothing about whether anything ever invokes it, and a
guard nobody invokes is a guard that does not exist.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from agent_swarm.staged_imports import main  # noqa: E402  (the path insert above must precede it)

if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:], importable_roots=['src']))
