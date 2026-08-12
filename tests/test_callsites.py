"""A sweep that cannot ATTRIBUTE a keyword to its call is a grep with a better reputation.

MOVED HERE WITH THE CODE, 2026-08-12. The defect it exists for was measured on 2026-08-05: a stale
keyword argument survived a text sweep and was reported as verified, because the token sat in a file
where a DIFFERENT function legitimately still took that keyword. A line-oriented search finds the
token; only a parse can say which call it belongs to.

WHAT THESE CANNOT SEE: the sweep is static and name-based, so a call reached through an alias, a
variable holding the function, or `getattr` is invisible to it. That is a stated limit of the
mechanism, not something these tests could close.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_swarm.callsites import call_sites, main


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / 'src').mkdir()
    return tmp_path


def test_a_kwarg_belonging_to_a_DIFFERENT_call_is_not_reported(tree: Path) -> None:
    """THE DEFECT THIS MODULE EXISTS FOR. Both calls live in one file and both mention `mode`; only
    one of them is a call of the function under sweep. A text sweep reports two hits here and a
    reader concludes the wrong site is stale.
    """
    (tree / 'src' / 'a.py').write_text(
        'target(1, mode="fast")\nother(2, mode="slow")\n',
        encoding='utf-8',
    )
    sites = call_sites('target', [str(tree / 'src')])
    assert [(site[1], site[2], site[3]) for site in sites] == [(1, 1, ['mode'])]


def test_kwargs_unpacking_is_reported_as_unresolvable(tree: Path, capsys) -> None:
    """A `**kwargs` forward cannot be resolved statically. Reporting it as CLEAN would be the silent
    version of the same defect: the keys are exactly the ones nobody can see.
    """
    (tree / 'src' / 'a.py').write_text('target(**payload)\n', encoding='utf-8')

    assert call_sites('target', [str(tree / 'src')])[0][3] == [None]

    assert main(['target', str(tree / 'src')], default_roots=['unused']) == 1
    out = capsys.readouterr().out
    assert 'CHECK BY HAND' in out and 'not statically knowable' in out


def test_a_forbidden_kwarg_is_flagged_and_the_run_fails(tree: Path, capsys) -> None:
    (tree / 'src' / 'a.py').write_text('target(mode="fast")\ntarget(other=1)\n', encoding='utf-8')

    assert main(['target', str(tree / 'src'), '--forbid', 'mode'], default_roots=['unused']) == 1
    out = capsys.readouterr().out
    assert "FORBIDDEN kwarg 'mode'" in out
    assert '2 call site(s)' in out and '1 flagged' in out


def test_no_forbidden_kwarg_is_a_clean_zero(tree: Path, capsys) -> None:
    """The control: a checker that flagged everything would satisfy the test above."""
    (tree / 'src' / 'a.py').write_text('target(mode="fast")\n', encoding='utf-8')

    assert main(['target', str(tree / 'src'), '--forbid', 'other'], default_roots=['unused']) == 0
    assert '0 flagged' in capsys.readouterr().out


def test_max_positional_flags_only_the_call_that_exceeds_it(tree: Path, capsys) -> None:
    (tree / 'src' / 'a.py').write_text('target(1)\ntarget(1, 2, 3)\n', encoding='utf-8')

    assert main(['target', str(tree / 'src'), '--max-positional', '2'], default_roots=['unused']) == 1
    out = capsys.readouterr().out
    assert 'POSITIONAL past 2' in out
    assert out.count('POSITIONAL past 2') == 1, 'the compliant call was flagged too'


def test_a_method_call_is_found_by_its_attribute_name(tree: Path) -> None:
    """`obj.target(...)` and `target(...)` break identically when the signature moves; a sweep that
    knew only the bare-name spelling would miss most real call sites.
    """
    (tree / 'src' / 'a.py').write_text('obj.target(1)\n', encoding='utf-8')
    assert [site[2] for site in call_sites('target', [str(tree / 'src')])] == [1]


def test_a_file_that_does_not_parse_does_not_stop_the_sweep(tree: Path) -> None:
    """Syntax is the linters' business. A sweep that aborted on one broken file would report a
    partial count as if it were the whole tree -- silence that reads as clean.
    """
    (tree / 'src' / 'broken.py').write_text('def (:\n', encoding='utf-8')
    (tree / 'src' / 'good.py').write_text('target(1)\n', encoding='utf-8')
    assert len(call_sites('target', [str(tree / 'src')])) == 1


def test_the_roots_really_decide_what_is_swept(tree: Path) -> None:
    """THE DISCRIMINATING TEST FOR THE EXTRACTION, and why `default_roots` has no default.

    The same call site is a hit under one layout and invisible under another. A consumer whose code
    does not live in `src/` would otherwise get a confident zero from a sweep that looked nowhere --
    and zero hits is indistinguishable from clean unless the roots are known to have been consulted.
    """
    (tree / 'lib').mkdir()
    (tree / 'lib' / 'a.py').write_text('target(1)\n', encoding='utf-8')

    assert call_sites('target', [str(tree / 'src')]) == []
    assert len(call_sites('target', [str(tree / 'lib')])) == 1


def test_main_falls_back_to_the_default_roots_it_was_GIVEN(tree: Path, capsys) -> None:
    """The same fact through the CLI surface: with no positional roots, the caller's list is used --
    the caller's, not one this module chose.
    """
    (tree / 'lib').mkdir()
    (tree / 'lib' / 'a.py').write_text('target(1)\n', encoding='utf-8')

    assert main(['target'], default_roots=[str(tree / 'src')]) == 0
    assert '0 call site(s)' in capsys.readouterr().out

    assert main(['target'], default_roots=[str(tree / 'lib')]) == 0
    assert '1 call site(s)' in capsys.readouterr().out
