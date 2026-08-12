"""A pin must go stale on CONTENT, and only on content -- otherwise it is a note with extra steps.

MOVED HERE WITH THE CODE, 2026-08-12. The mechanism is the invalidation key: a fact recorded beside
the content hashes of its sources can answer FRESH/STALE in one call, which neither a re-read nor a
scratchpad can do. The whole argument rests on hashing rather than mtime, so the load-bearing test
here is the one that rewrites a file with IDENTICAL bytes and demands FRESH.

These tests build a real git repository and let `git hash-object` do the hashing, because the claim
is about what git reports; a stubbed hasher would be the test checking its own copy of the rule.

WHAT THESE CANNOT SEE: the `--refresh` re-run path is exercised only through a shell command, so a
box whose shell rejects the probe command would report differently -- the command used here is the
most portable one available (`python -c`), not a guarantee about every shell.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_swarm.pins import blob_hash, drift, main, sources_state


def _git(repo: Path, *args: str) -> None:
    # timeout: a git call that hangs would park the suite at 0% CPU rather than failing.
    subprocess.run([shutil.which('git') or 'git', *args], cwd=repo, check=True, capture_output=True, timeout=60)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _git(tmp_path, 'init', '-q')
    _git(tmp_path, 'config', 'user.email', 't@t')
    _git(tmp_path, 'config', 'user.name', 't')
    (tmp_path / 'note.txt').write_text('alpha\n', encoding='utf-8')
    _git(tmp_path, 'add', '-A')
    _git(tmp_path, 'commit', '-qm', 'base')
    # The pin records sources by the spelling the caller used, so relative paths need a CWD.
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_a_touched_but_unchanged_source_is_NOT_stale(repo: Path, capsys) -> None:
    """THE POINT OF HASHING RATHER THAN MTIME, and the assertion the design argument rests on.

    An mtime key answers "something happened to this file", which is a different question: a
    formatter that rewrote every file, a checkout, or a revert would all report STALE on a value
    that is still exactly right. False STALEs are what make a freshness signal get ignored.
    """
    assert main(['pin', 'n', '--file', 'note.txt'], store=repo / 'store') == 0
    capsys.readouterr()

    (repo / 'note.txt').write_text('alpha\n', encoding='utf-8')  # same bytes, new mtime

    assert main(['get', 'n'], store=repo / 'store') == 0
    captured = capsys.readouterr()
    assert 'FRESH' in captured.err
    assert captured.out == 'alpha\n'


def test_a_changed_source_makes_get_exit_2_and_say_STALE(repo: Path, capsys) -> None:
    assert main(['pin', 'n', '--file', 'note.txt'], store=repo / 'store') == 0
    capsys.readouterr()

    (repo / 'note.txt').write_text('beta\n', encoding='utf-8')

    assert main(['get', 'n'], store=repo / 'store') == 2, 'a stale pin that exits 0 is a lying declaration'
    captured = capsys.readouterr()
    assert 'STALE' in captured.err and 'note.txt' in captured.err
    assert captured.out == 'alpha\n', 'the pinned value is still shown -- it is labelled, not withheld'


def test_a_cmd_pin_without_from_REFUSES(repo: Path, capsys) -> None:
    """A command's dependencies cannot be inferred, so a pin with guessed sources would answer FRESH
    about a stale value -- a lying declaration inside the tool written to catch lying declarations.
    """
    code = main(['pin', 'n', '--cmd', 'echo hi'], store=repo / 'store')
    assert code == 1
    assert '--from' in capsys.readouterr().err
    assert not (repo / 'store' / 'n.json').exists(), 'it refused and wrote the pin anyway'


def test_a_cmd_pin_with_from_records_the_named_sources(repo: Path, capsys) -> None:
    """The control. A refusal-only test passes against a tool that refuses everything."""
    command = f'{Path(sys.executable).as_posix()} -c "print(7)"'
    assert main(['pin', 'n', '--cmd', command, '--from', 'note.txt'], store=repo / 'store') == 0
    capsys.readouterr()
    assert main(['get', 'n'], store=repo / 'store') == 0
    assert capsys.readouterr().out.strip() == '7'


def test_the_store_directory_really_decides_where_pins_live(repo: Path, capsys) -> None:
    """THE DISCRIMINATING TEST FOR THE EXTRACTION, and why `store` has no default.

    Two stores must not see each other's pins. If the argument were ignored -- or silently defaulted
    to one layout -- every consumer would share one cache directory, and a pin recorded by one tree
    would answer, confidently and wrongly, about another tree's files.
    """
    assert main(['pin', 'n', '--file', 'note.txt'], store=repo / 'store-a') == 0
    capsys.readouterr()

    assert main(['get', 'n'], store=repo / 'store-b') == 1
    assert 'no pin named' in capsys.readouterr().err
    assert main(['get', 'n'], store=repo / 'store-a') == 0


def test_list_reports_each_store_separately(repo: Path, capsys) -> None:
    assert main(['pin', 'first', '--file', 'note.txt'], store=repo / 'store-a') == 0
    assert main(['pin', 'second', '--file', 'note.txt'], store=repo / 'store-b') == 0
    capsys.readouterr()

    assert main(['list'], store=repo / 'store-a') == 0
    listing = capsys.readouterr().out
    assert 'first' in listing and 'second' not in listing and 'FRESH' in listing


def test_a_slice_pins_only_the_named_lines(repo: Path, capsys) -> None:
    (repo / 'many.txt').write_text('one\ntwo\nthree\n', encoding='utf-8')
    assert main(['pin', 'n', '--file', 'many.txt', '--lines', '2,3'], store=repo / 'store') == 0
    capsys.readouterr()
    assert main(['get', 'n'], store=repo / 'store') == 0
    assert capsys.readouterr().out == 'two\nthree'


def test_a_missing_file_is_refused_rather_than_pinned_empty(repo: Path, capsys) -> None:
    assert main(['pin', 'n', '--file', 'nope.txt'], store=repo / 'store') == 1
    assert 'does not exist' in capsys.readouterr().err


def test_the_hash_is_of_content_and_a_missing_file_hashes_to_None(repo: Path) -> None:
    """The primitive under everything above, asserted directly so a failure names the right layer."""
    first = blob_hash(repo / 'note.txt')
    (repo / 'copy.txt').write_text('alpha\n', encoding='utf-8')
    assert first is not None
    assert blob_hash(repo / 'copy.txt') == first, 'identical bytes must hash identically'
    assert blob_hash(repo / 'absent.txt') is None

    state = sources_state(['note.txt', 'absent.txt'])
    assert state == {'note.txt': first, 'absent.txt': None}
    assert drift({'sources': state}) == []
    (repo / 'note.txt').write_text('gamma\n', encoding='utf-8')
    assert drift({'sources': state}) == ['note.txt']
