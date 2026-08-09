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

LIVE_MARKER = 'live_forge'

#: Below this, the live tier has effectively vanished -- a renamed marker, a deleted file, an import
#: error swallowing a module. The number is deliberately a floor and not an exact count: it must not
#: need editing every time a test is added, or it will be edited to whatever it happens to be.
MINIMUM_LIVE_TESTS = 15

_deselected_live: list[str] = []


def pytest_deselected(items) -> None:
    _deselected_live.extend(item.nodeid for item in items if item.get_closest_marker(LIVE_MARKER))


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    """Say what was NOT run. Never silently."""
    selected_live = sum(1 for item in getattr(config, '_live_collected', []) if item)
    if _deselected_live:
        terminalreporter.write_sep('-', f'{LIVE_MARKER}: {len(_deselected_live)} NOT RUN', yellow=True)
        terminalreporter.write_line(
            f'  no forge was contacted. Run them with:  pytest -m {LIVE_MARKER}\n'
            f'  the contract itself IS covered offline (InMemoryStore and RecordingForge); what is '
            f'missing is the evidence that a real deployment still behaves as measured.'
        )
    elif selected_live:
        terminalreporter.write_sep('-', f'{LIVE_MARKER}: {selected_live} ran against a real forge', green=True)


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
    live = [item for item in items if item.get_closest_marker(LIVE_MARKER)]
    config._live_collected = live
    if _is_whole_suite(config) and len(live) < MINIMUM_LIVE_TESTS:
        raise pytest.UsageError(
            f'only {len(live)} tests carry @pytest.mark.{LIVE_MARKER}, expected at least '
            f'{MINIMUM_LIVE_TESTS}. The live tier has silently shrunk -- a renamed marker, or a '
            f'module that failed to import, would look exactly like this and a default run would '
            f'still be green.'
        )
