"""The vacuity sieve's own controls, plus the boundary that keeps it vendor-neutral.

WHY THE CONTROLS ARE THE TEST. `vacuity.self_test` plants five positive and negative shapes and runs
the REAL layers over them -- an empty guard, an arity mismatch, an accumulator that must NOT be
flagged, a correctly-shaped small guard that must NOT be flagged, and the shape layers A and B are
both blind to that only C's intersection can see. That is not a convenience wrapper: the FIRST run
of this sieve reported 0/0 on a real tree while silently skipping the 29 modules that carried the
matrices, and a false zero looks exactly like a clean result. Controls are what turn a null result
into evidence.

So this file's job is to run them and to assert the two properties `self_test` itself cannot: that
it can FAIL (a control list that always said PASS would be the same false zero one level up), and
that the replay denylist is genuinely required.
"""

from __future__ import annotations

import pytest

from agent_swarm import vacuity


@pytest.fixture(scope='module')
def controls(tmp_path_factory):
    """The full control suite, run once -- it imports and replays, so it is not free."""
    return vacuity.self_test(tmp_path_factory.mktemp('sieve'))


def test_every_control_passes(controls):
    """Parametrization is deliberately NOT used: a failure must name every broken control at once,
    because they are diagnostic of DIFFERENT layers and fixing them one red at a time would mean one
    import-and-replay cycle each.
    """
    failed = [label for label, ok in controls if not ok]
    assert not failed, 'the sieve does not behave as its own controls describe:\n  ' + '\n  '.join(failed)


def test_the_control_list_is_not_empty(controls):
    """The instrument first. An empty list satisfies the assertion above vacuously -- which is
    precisely the defect class this whole module exists to find, so it must not be its own instance.
    """
    assert len(controls) >= 9


def test_the_controls_can_actually_FAIL(tmp_path, monkeypatch):
    """THE DISCRIMINATING TEST. If `self_test` cannot go red, a green self-test is worth nothing --
    and the tool's whole claim is that it fires on the two known shapes. Sabotage layer B into
    finding nothing and at least one control must notice.
    """
    monkeypatch.setattr(vacuity, 'layer_b', lambda _paths, _root: [])
    results = vacuity.self_test(tmp_path)
    assert any(not ok for _label, ok in results), 'a sieve with layer B disabled still reported every control PASS'


def test_layer_c_REQUIRES_a_denylist(tmp_path):
    """A REPLAY IS REAL EXECUTION. Which symbols make a body unsafe to run is a fact about the
    project being scanned; a default shipped here would run a stranger's destructive test body while
    looking like static analysis. A `TypeError` at the call is the whole protection.
    """
    with pytest.raises(TypeError):
        vacuity.layer_c([], tmp_path, 1.0)  # type: ignore[call-arg]


def test_a_denylisted_name_makes_a_site_UNKNOWN_rather_than_a_number(tmp_path):
    """The denylist's actual effect, and the direction that matters: a disqualified site must become
    UNKNOWN -- "I did not measure this" -- never a count bought by executing the body.
    """
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests' / 'test_planted.py').write_text(
        'import pytest\n'
        "_POPULATION = ('a', 'b')\n"
        "_GUARD = ('a',)\n\n"
        "@pytest.mark.parametrize('item', _POPULATION)\n"
        'def test_it(item):\n'
        '    dangerous_call()\n'
        '    assert item in _GUARD or True\n',
        encoding='utf-8',
    )
    paths = vacuity.scan_paths(tmp_path, ('tests',))

    measured, unknown = vacuity.layer_c(paths, tmp_path, 60.0, denylist=frozenset({'dangerous_call'}))
    assert measured == [], 'a denylisted body must not be replayed for a number'
    assert any('denylisted: dangerous_call' in site for site in unknown)

    # THE CONTROL: without the denylist entry the same site IS measured, so the assertion above is
    # about the denylist and not about the file being unreplayable for some other reason.
    measured_without, _unknown = vacuity.layer_c(paths, tmp_path, 60.0, denylist=frozenset())
    assert any('_GUARD' in line for line in measured_without)


def test_scan_paths_takes_its_roots_and_skips_caches(tmp_path):
    """Which directories are worth scanning is a tree's LAYOUT. A package guessing would silently
    scan nothing in a repo shaped differently -- reporting zero findings, which reads as a clean
    tree.
    """
    (tmp_path / 'here').mkdir()
    (tmp_path / 'here' / 'a.py').write_text('X = 1\n', encoding='utf-8')
    (tmp_path / 'here' / '__pycache__').mkdir()
    (tmp_path / 'here' / '__pycache__' / 'a.py').write_text('X = 1\n', encoding='utf-8')
    (tmp_path / 'elsewhere').mkdir()
    (tmp_path / 'elsewhere' / 'b.py').write_text('Y = 1\n', encoding='utf-8')

    found = vacuity.scan_paths(tmp_path, ('here',))
    assert [p.name for p in found] == ['a.py'], 'a cache is not source, and an unnamed root is not scanned'


def test_the_report_says_UNKNOWN_even_when_the_measured_numbers_are_clean(tmp_path):
    """UNKNOWN IS NOT A CLEAN RESULT. A report that printed only the measured half would be the same
    instrument-that-lies shape the module exists to find -- silent about what it could not see.
    """
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests' / 'test_planted.py').write_text(
        'import pytest\n'
        "_GUARD = ('a',)\n\n"
        "@pytest.mark.parametrize('item', ('a', 'b'))\n"
        'def test_it(item, tmp_path):\n'  # a fixture -> unreplayable -> UNKNOWN
        '    assert item in _GUARD or tmp_path\n',
        encoding='utf-8',
    )
    lines = vacuity.render_report(
        vacuity.scan_paths(tmp_path, ('tests',)),
        tmp_path,
        layers='c',
        min_size=3,
        budget_s=60.0,
        denylist=frozenset(),
    )
    text = '\n'.join(lines)
    assert 'MEASURED population: 0' in text
    assert 'UNKNOWN (not a clean result): 1' in text
    assert 'takes fixture(s)' in text
