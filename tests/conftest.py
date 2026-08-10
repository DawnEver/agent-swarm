"""Make the live-forge tier VISIBLE when it does not run.

THE DEFECT THIS CLOSES. Deselecting the live tests by default fixes the wall of red on a box with no
forge, but it introduces a worse thing quietly: the tests are then *absent from the report*, and
"all green" and "nothing was ever tested against a real forge" become indistinguishable. That is the
same shape as a verdict read as PASS because nobody asked -- the unearned green this package exists
to prevent, in its own suite.

So a default run states, in its summary, exactly how many live tests it did not run and the command
that runs them. And a run where NO live tests exist at all is louder still: a marker that quietly
stopped matching anything is how a whole tier disappears without a single line of red.
"""

from __future__ import annotations

from pathlib import Path

import pytest

#: Every tier that reaches off this box. Two, now: a forge over HTTP and a session fleet over
#: fabric's own transport. They fail for different reasons and are deselected together.
LIVE_MARKERS = ('live_forge', 'live_fabric')
LIVE_MARKER = LIVE_MARKERS[0]

#: Below this, the live tier has effectively vanished -- a renamed marker, a deleted file, an import
#: error swallowing a module. The number is deliberately a floor and not an exact count: it must not
#: need editing every time a test is added, or it will be edited to whatever it happens to be.
MINIMUM_LIVE_TESTS = 15

_deselected_live: list[str] = []


def pytest_deselected(items) -> None:
    _deselected_live.extend(item.nodeid for item in items if any(item.get_closest_marker(m) for m in LIVE_MARKERS))


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    """Say what was NOT run. Never silently."""
    selected_live = sum(1 for item in getattr(config, '_live_collected', []) if item)
    selector = ' or '.join(LIVE_MARKERS)
    if _deselected_live:
        terminalreporter.write_sep('-', f'{LIVE_MARKER}: {len(_deselected_live)} NOT RUN', yellow=True)
        terminalreporter.write_line(
            f'  nothing off this box was contacted. Run them with:  pytest -m "{selector}"\n'
            f'  the contracts themselves ARE covered offline (InMemoryStore, RecordingForge and the\n'
            f'  fake session); what is missing is the evidence that a real deployment and a real\n'
            f'  session fleet still behave as measured.'
        )
    elif selected_live:
        terminalreporter.write_sep('-', f'{LIVE_MARKER}: {selected_live} ran off this box', green=True)


def live_tier_selected_by_accident(markexpr: str) -> bool:
    """Did a command-line `-m` widen this run into the live tier without meaning to?

    A FUNCTION, so the predicate can be tested without spawning pytest inside pytest. Its first
    version lived inline and fired on every default run, because the hook that holds it runs BEFORE
    pytest's own mark deselection -- a warning that cries wolf on every offline run is one that gets
    deleted, so the predicate is the part that has to be right.

    The accident's signature is an expression that never NAMES a live marker: `-m "not live"` reads
    as a guard and matches neither `live_forge` nor `live_fabric`, so it excludes nothing while
    replacing the default that did.
    """
    return bool(markexpr) and not any(marker in markexpr for marker in LIVE_MARKERS)


def _is_whole_suite(config) -> bool:
    """Is this a full run, as opposed to someone iterating on one file?

    THE FLOOR BELOW MUST NOT FIRE ON `pytest tests/test_spool.py`, which legitimately collects no
    live tests at all. A guard that cries wolf during ordinary iteration is a guard that gets
    deleted, so it applies only when the whole tree was asked for: directory arguments, no `-k`.
    """
    if config.option.keyword:
        return False
    return all(Path(str(arg).split('::')[0]).is_dir() for arg in config.args)


def pytest_collection_modifyitems(config, items) -> None:
    """Refuse a whole-suite run in which the live tier has silently ceased to exist.

    A COLLECTION-TIME ERROR, not a summary note: a note about a vanished tier is a note nobody reads
    on a green run. It counts what was COLLECTED before deselection, so it fires whether or not the
    tier was selected -- which is the point, since the default run is the one that would hide it.
    """
    live = [item for item in items if any(item.get_closest_marker(m) for m in LIVE_MARKERS)]
    config._live_collected = live

    # SAY IT BEFORE THE FIRST NETWORK CALL, not in the summary.
    #
    # A command-line `-m` REPLACES the `addopts` expression rather than intersecting with it, so
    # `pytest -m "not live"` -- reaching for `-m` to narrow a run -- silently WIDENS it into the live
    # tier: real forge writes, real LLM sessions. `live` matches neither marker name, so the guard
    # reads as satisfied while doing nothing. That is not hypothetical; it happened, and the run was
    # killed at 3.5 minutes having already created work items on the real forge.
    #
    # The end-of-run summary could not have helped: it prints after the writes. A banner at
    # collection is the only version that arrives while the run can still be stopped, and it names
    # the flag because "you are about to hit the network" is useless without "here is what did it".
    # The signature is the EXPRESSION, not the selection. This hook runs before pytest's own mark
    # deselection, so `items` still holds the live tests on an ordinary default run -- the first
    # version of this banner fired on every offline run, which is how a warning becomes wallpaper.
    # What distinguishes the accident is that `-m` was overridden by an expression that never
    # NAMES a live marker: the author was thinking about the live tier and failed to exclude it.
    markexpr = config.option.markexpr
    if live and live_tier_selected_by_accident(markexpr):
        writer = config.get_terminal_writer()
        writer.line('')
        writer.line(f'LIVE TIER SELECTED: {len(live)} tests will contact a REAL forge / spawn REAL sessions.', red=True)
        writer.line(f'  -m was given as {markexpr!r}, which names neither {" nor ".join(LIVE_MARKERS)}.', red=True)
        writer.line('  a command-line -m REPLACES the default deselection; it does not add to it.', red=True)
        writer.line('  to NARROW a default run use -k or a path. Ctrl-C now if this is not what you meant.', red=True)
        writer.line('')

    if _is_whole_suite(config) and len(live) < MINIMUM_LIVE_TESTS:
        raise pytest.UsageError(
            f'only {len(live)} tests carry @pytest.mark.{LIVE_MARKER}, expected at least '
            f'{MINIMUM_LIVE_TESTS}. The live tier has silently shrunk -- a renamed marker, or a '
            f'module that failed to import, would look exactly like this and a default run would '
            f'still be green.'
        )
