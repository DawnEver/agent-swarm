"""The live tier must be VISIBLY absent from a default run, not silently absent.

Deselecting the forge tests fixes the wall of red on a box with no server. On its own it introduces
something worse and quieter: the tests vanish from the report, and "all green" becomes
indistinguishable from "no forge was ever contacted". That is the unearned green this package exists
to prevent, occurring inside its own suite.

These tests run pytest in a SUBPROCESS, because the property is about what the terminal reporter
prints and the only honest way to check a report is to read one. They use `--collect-only`, or a
single file, wherever that is enough: a child that ran the whole suite would put ~5 s on the parent
for evidence that costs 0.5 s, and the default tier has a time budget it is the point of this file
to respect.
"""

from __future__ import annotations

import functools
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import conftest
from conftest import LIVE_MARKER, LIVE_MARKERS, MINIMUM_LIVE_TESTS

REPO = Path(__file__).resolve().parent.parent

#: The child pytest may collect this file too. Without a guard a test could spawn a run that spawns
#: a run: the suite would not fail, it would simply never finish, and the first symptom is a timeout
#: with no failing assertion to point at. (It happened.)
_CHILD_FLAG = 'AGENT_SWARM_TIERING_CHILD'

pytestmark = pytest.mark.skipif(
    os.environ.get(_CHILD_FLAG) == '1',
    reason='inner pytest run: spawning another would recurse without bound',
)

#: Every live test lives here, so this one file is enough to observe the deselection report.
LIVE_TEST_FILE = 'tests/test_forge_store.py'


@functools.cache
def _pytest(*args: str) -> subprocess.CompletedProcess[str]:
    """Cached: the same child command is asked for by several tests, and pytest startup is the
    whole cost. Every call below also passes `--collect-only` where it can -- the summary hook that
    prints the deselection line runs at collection, so nothing has to be EXECUTED to observe it."""
    child = dict(os.environ)
    child[_CHILD_FLAG] = '1'
    child['PYTHONPATH'] = str(REPO / 'src')
    return subprocess.run(
        [sys.executable, '-m', 'pytest', *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        env=child,
    )


class TestTheDefaultRunIsOfflineAndSaysSo:
    def test_it_NAMES_the_tier_it_did_not_run(self):
        """The line that stops "all green" from meaning two different things."""
        result = _pytest(LIVE_TEST_FILE, '--collect-only', '-q')
        assert result.returncode == 0, result.stdout[-2000:]
        assert f'{LIVE_MARKER}: ' in result.stdout, 'the default run said nothing about the live tier'
        assert 'NOT RUN' in result.stdout

    def test_it_carries_the_COMMAND_that_runs_them(self):
        """A report that names a gap without naming the fix is a report that gets read once."""
        result = _pytest(LIVE_TEST_FILE, '--collect-only', '-q')
        assert f'pytest -m "{" or ".join(LIVE_MARKERS)}"' in result.stdout

    def test_it_contacts_no_forge_at_all(self):
        """Asserted on the COLLECTION, which cannot reach a network: every live test is deselected
        before a single one is executed, so no default run can depend on a reachable server.
        """
        result = _pytest('tests/', '--collect-only', '-q')
        assert result.returncode == 0
        assert 'deselected' in result.stdout


class TestTheLiveTierStillExists:
    def test_enough_tests_carry_the_marker(self):
        """The floor guard's own premise. If the marker were renamed, or a module failed to import,
        the default run would still be green and this is the only thing that would notice.
        """
        result = _pytest('tests/', '-m', ' or '.join(LIVE_MARKERS), '--collect-only', '-q')
        # pytest prints e.g. "23/234 tests collected (211 deselected)"; take the first number.
        found = re.search(r'(\d+)/\d+ tests collected', result.stdout)
        assert found, f'could not read the collection summary from: {result.stdout[-500:]}'
        assert int(found.group(1)) >= MINIMUM_LIVE_TESTS

    def test_the_floor_is_a_FLOOR_not_an_exact_count(self):
        """An exact count gets edited to whatever it happens to be every time a test is added, until
        it asserts nothing. A floor only ever moves when the tier genuinely shrinks.
        """
        assert MINIMUM_LIVE_TESTS < 27


class TestAnOverriddenMarkExpressionCannotSilentlySelectLive:
    """The accident: `-m` REPLACES `addopts`' deselection rather than intersecting with it.

    `pytest -m "not live"` reads as a guard, names neither marker, and therefore excludes nothing --
    it widens an offline run into real forge writes and real LLM sessions. It happened, and the run
    was killed at 3.5 minutes having already created work items on the real forge.

    The end-of-run summary cannot help, because it prints after the writes. The banner fires at
    COLLECTION, while the run can still be stopped.
    """

    @pytest.mark.parametrize('markexpr', ['not live', 'not slow', 'unit', 'not network'])
    def test_an_expression_that_never_NAMES_a_live_marker_is_the_accident(self, markexpr):
        assert conftest.live_tier_selected_by_accident(markexpr)

    @pytest.mark.parametrize(
        'markexpr', ['live_forge or live_fabric', 'live_forge', 'not live_forge and not live_fabric']
    )
    def test_asking_for_the_live_tier_BY_NAME_is_not_an_accident(self, markexpr):
        """The discriminating direction. A banner on a deliberate live run is noise, and noise is
        how a warning stops being read.
        """
        assert not conftest.live_tier_selected_by_accident(markexpr)

    def test_the_DEFAULT_run_is_silent(self):
        """No `-m` on the command line means `addopts` is in force and nothing was overridden.

        This is the case the first version got wrong: the hook runs before pytest's own mark
        deselection, so the live items are still in `items` on an ordinary offline run.
        """
        assert not conftest.live_tier_selected_by_accident('')
