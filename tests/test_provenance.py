"""The install must be a COPY, not a live view of this working tree.

THE HAZARD, in one sentence: `agent-swarm` is a dependency of motronics' gate, and no motronics
verdict records which version of it decided the result. An editable install would make that worse in
the sharpest possible way -- every uncommitted edit in this repo would be live inside every gate on
that box, and a verdict would be decided partly by code that exists in no commit.

**THIS IS A CROSS-REPO ASSERTION AND IT MUST BE READ AS ONE.** It reaches out of this repository and
inspects a DIFFERENT project's interpreter. It is not "the venv is safe"; it is exactly one claim
about exactly one directory, and the reasons that matters are below.

**IT DOES NOT COVER THE FAILURE WE ACTUALLY HIT.** On 2026-08-10 an install landed in that
interpreter while a gate was starting. It was a NON-editable install from a committed sha -- the
correct kind -- and it was still enough to change the interpreter under a running verdict. Refusing
editable installs closes one door in a room with two. The other door is TIMING, and nothing here
closes it: that needs an install-time interlock, or the provenance line in the gate log that
lane-transport is adding. Without this paragraph the test reads as a guarantee of something it does
not check, which would make it worse than absent.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_swarm.provenance import Provenance, read_provenance, running_provenance

#: The interpreter every gate on this box runs under. A LITERAL PATH, not discovery: the whole point
#: is to assert about one specific environment, and a check that searched for "some venv" would pass
#: by finding a different one.
MOTRONICS_SITE_PACKAGES = Path('C:/Users/linxu/Documents/PEMC/motronics-studio/.venv/Lib/site-packages')


def _write_dist_info(root: Path, *, editable: bool | None, version: str = '0.1.0') -> Path:
    dist_info = root / f'agent_swarm-{version}.dist-info'
    dist_info.mkdir(parents=True)
    if editable is not None:
        payload = {'dir_info': {'editable': editable}, 'url': 'file:///somewhere/agent-swarm'}
        (dist_info / 'direct_url.json').write_text(json.dumps(payload), encoding='utf-8')
    return dist_info


class TestReadingAnInstallsOrigin:
    """Unit-tested against fabricated dist-info, so the checker itself is not taken on trust."""

    def test_an_editable_install_is_reported_as_editable(self, tmp_path):
        _write_dist_info(tmp_path, editable=True)
        found = read_provenance(tmp_path)
        assert found is not None
        assert found.editable is True

    def test_a_copy_install_is_reported_as_a_copy(self, tmp_path):
        _write_dist_info(tmp_path, editable=False)
        assert read_provenance(tmp_path).editable is False

    def test_NOT_INSTALLED_is_None_and_must_not_read_as_fine(self, tmp_path):
        """A source checkout running with `PYTHONPATH=src` has no dist-info at all. `None` is
        "absent", and the caller decides whether absent is acceptable where it stands -- this
        function must not decide that for it.
        """
        assert read_provenance(tmp_path) is None

    def test_an_install_with_NO_direct_url_records_nothing(self, tmp_path):
        """A wheel from an index has no `direct_url.json`. That is not editable, and it is also not
        provenance -- `recorded` is what tells the two apart.
        """
        _write_dist_info(tmp_path, editable=None)
        found = read_provenance(tmp_path)
        assert found.editable is False
        assert found.recorded is False

    def test_a_CORRUPT_direct_url_is_treated_as_EDITABLE(self, tmp_path):
        """The one answer this must never give is "corrupt provenance, therefore clean install".
        Unreadable is not evidence of safety, so it fails toward the refusal.
        """
        dist_info = _write_dist_info(tmp_path, editable=False)
        (dist_info / 'direct_url.json').write_text('{"dir_info": ', encoding='utf-8')
        assert read_provenance(tmp_path).editable is True

    def test_the_RAW_text_is_kept_for_a_gate_log(self, tmp_path):
        """`__version__` is `0.1.0` on every commit so far and distinguishes nothing, so a gate log
        must print what pip wrote. Verbatim, because a summary is a place for a bug to hide and the
        raw line is what another engineer can compare against their own box.
        """
        _write_dist_info(tmp_path, editable=False)
        text = read_provenance(tmp_path).direct_url_text
        assert text is not None
        assert json.loads(text)['url'].startswith('file://')


class TestTheGateInterpreterHoldsACopy:
    """The cross-repo assertion. One claim, one directory -- see this module's docstring for what it
    deliberately does NOT cover."""

    @pytest.mark.skipif(
        not MOTRONICS_SITE_PACKAGES.is_dir(),
        reason='the motronics venv is not on this box; the claim is about that interpreter and no other',
    )
    def test_agent_swarm_is_not_installed_EDITABLE_there(self):
        """An editable install would put every uncommitted edit in this repo inside every gate on
        that box, and a verdict would then be decided partly by code that exists in no commit and
        can be reproduced by nobody.

        NOT INSTALLED IS A PASS, and deliberately so: this asserts "not editable", not "present".
        Whether it should be installed at all is the other project's decision, and a test that
        demanded presence would fail on every box that does not run gates.
        """
        found = read_provenance(MOTRONICS_SITE_PACKAGES)
        if found is None:
            pytest.skip('agent_swarm is not installed in that interpreter; nothing to be editable')
        assert isinstance(found, Provenance)
        assert not found.editable, (
            f'agent_swarm is installed EDITABLE at {found.dist_info}, so uncommitted edits in this '
            f'working tree are live inside every gate on this box. Reinstall from a committed sha: '
            f'pip install --no-deps <copy of `git archive <sha>`>. direct_url.json says: '
            f'{found.direct_url_text}'
        )


class TestATimingCannotBeQuotedWithoutItsProvenance:
    """`running_provenance()` exists so a measured number cannot be printed without saying where its
    code came from. The rule it replaces -- "always quote the two together" -- is one a person has
    to remember, and this suite has already produced one figure that travelled without it."""

    def test_it_names_the_source_tree_and_its_sha_when_run_from_a_checkout(self):
        line = running_provenance()
        assert 'agent_swarm' in line
        assert 'SOURCE' in line or 'INSTALLED' in line

    def test_it_says_DIRTY_when_the_tree_has_uncommitted_changes(self):
        """The state in which a figure is unreproducible by anyone else -- and therefore the one it
        must never be quoted from silently.
        """
        line = running_provenance()
        if 'SOURCE' not in line:
            pytest.skip('this interpreter runs an installed copy, so there is no working tree to be dirty')
        dirty = subprocess.run(
            ['git', '-C', str(Path(__file__).resolve().parent.parent), 'status', '--porcelain'],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        assert ('DIRTY' in line) is bool(dirty)

    def test_the_timing_test_actually_CALLS_it(self):
        """Otherwise this module is a capability nobody uses, and the discipline is back to being a
        thing someone remembers.
        """
        source = (Path(__file__).resolve().parent / 'test_end_to_end.py').read_text(encoding='utf-8')
        assert 'running_provenance()' in source
