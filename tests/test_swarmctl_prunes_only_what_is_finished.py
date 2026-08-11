"""`prune-issues` deletes CLOSED work items past a cutoff, and refuses everything else.

WHY DELETING IS SAFE, stated once here because it is the whole justification. A work item carries
WORK STATE -- claimed by whom, answered how -- which is the one fact that must be CONTENDED, which
is why it lives on a server at all. It is not the record: a verdict's record is
`refs/verdicts/<testkey>/<kind>/<envkey>`, immutable, and the narrative's record is `.claude/memory/`. A
closed item's fact has already expired, so removing it destroys nothing. That is the architecture's
own "a projection never writes back; the record lives elsewhere" paying for itself.

MEASURED 2026-08-10 on the live host: 1934 items, of which exactly ONE was open. The queue was never
the problem -- the ARCHIVE was, and an archive is precisely what those two homes already are.

WHAT THESE TESTS PIN is the SELECTION, not the HTTP. Deletion is irreversible on a host serving
every lane at once, so the interesting question is never "does DELETE work" but "would this have
selected something it should not". Each test below is one way to answer that wrongly.
"""

from __future__ import annotations

import argparse
import datetime
import time

import pytest

from agent_swarm import swarmctl as _swarmctl

pytestmark = pytest.mark.unit


@pytest.fixture(scope='module')
def swarmctl():
    """The module under test, as an ORDINARY IMPORT.

    It used to be loaded by path with `importlib.util.spec_from_file_location`, because it lived in
    another project as a bare script that nothing could import. Here it is a module of this package,
    so the loader preamble is gone -- and with it a whole class of mistake, since a hand-rolled load
    can silently execute a DIFFERENT file from the one an import would resolve.
    """
    return _swarmctl


def _stamp(days_ago: float) -> str:
    when = datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days_ago)
    return when.isoformat().replace('+00:00', 'Z')


def _item(number: int, *, days_ago: float | None = 30.0, labels: tuple[str, ...] = ()) -> dict:
    return {
        'number': number,
        'title': f'[swarm] test-run/j{number}',
        'closed_at': None if days_ago is None else _stamp(days_ago),
        'labels': [{'name': n} for n in labels],
    }


@pytest.fixture
def run(swarmctl, monkeypatch, tmp_path):
    """Drive `cmd_prune_issues` over a canned CLOSED listing; record what it would DELETE."""

    def go(items, **flags):
        deleted: list[int] = []
        provider = swarmctl.GiteaProvider('http://host:9000', 'Org', None, 'admin')
        pages = [items, []]
        monkeypatch.setattr(provider, 'api_list', lambda _m, _p: pages.pop(0) if pages else [])
        monkeypatch.setattr(provider, 'api', lambda _m, path, _b=None: deleted.append(int(path.rsplit('/', 1)[-1])))
        args = argparse.Namespace(
            repo='Org/Repo',
            older_than_days=flags.get('older_than_days', 7.0),
            keep_label=flags.get('keep_label'),
            manifest=str(tmp_path / 'pruned.jsonl'),
            yes=flags.get('yes', True),
        )
        swarmctl.cmd_prune_issues(provider, args)
        return deleted

    return go


# --------------------------------------------------------------------------- what it removes


def test_an_old_closed_item_is_deleted(run):
    """The feature. 1933 of 1934 items on the live host are exactly this."""
    assert run([_item(1, days_ago=30)]) == [1]


def test_a_RECENTLY_closed_item_is_kept(run):
    """The cutoff must bind. Someone reading yesterday's failure is the reason there is a grace
    period at all, and an off-by-one here is irreversible.
    """
    assert run([_item(1, days_ago=1)]) == []


def test_an_item_with_NO_closed_at_is_kept(run):
    """UNKNOWN IS NOT OLD. An unparseable or missing timestamp defaulting to "long ago" is unknown
    becoming old enough to delete -- the failure in the irreversible direction.
    """
    assert run([_item(1, days_ago=None)]) == []


def test_an_UNPARSEABLE_closed_at_is_kept(run):
    """Same rule, reached by a different road: a server that changes its timestamp format must cost
    a no-op, never a sweep.
    """
    bad = _item(1)
    bad['closed_at'] = 'the day before yesterday'
    assert run([bad]) == []


# --------------------------------------------------------------------------- what it refuses


def test_a_kept_label_survives_at_any_age(run):
    """The escape hatch, and it has a ceiling: it keeps items, it can never cause a deletion."""
    assert run([_item(1, days_ago=999, labels=('keep',))], keep_label=['keep']) == []


def test_a_DRY_RUN_deletes_nothing(run):
    """THE DEFAULT. `--yes` is the entire confirmation, and it is honoured only after the count and
    the oldest/newest number are printed -- so what an operator confirms is a measurement.
    """
    assert run([_item(1, days_ago=30)], yes=False) == []


def test_the_manifest_records_what_was_deleted(run, tmp_path):
    """ "Nothing of value is lost" is a CLAIM. For the cost of one file it is checkable, so a later
    question -- was that one really worthless? -- has an answer that is not someone's memory.
    """
    run([_item(7, days_ago=30)])
    written = (tmp_path / 'pruned.jsonl').read_text(encoding='utf-8')
    assert '"number": 7' in written and 'test-run/j7' in written


def test_the_manifest_is_written_BEFORE_the_first_delete(swarmctl, monkeypatch, tmp_path):
    """Ordering, for the same reason the verdict is written before its commit status: a crash
    mid-sweep must leave a record of what was selected, not a repository missing items nobody can
    enumerate.
    """
    seen: list[str] = []
    provider = swarmctl.GiteaProvider('http://host:9000', 'Org', None, 'admin')
    pages = [[_item(1), _item(2)], []]
    monkeypatch.setattr(provider, 'api_list', lambda _m, _p: pages.pop(0) if pages else [])
    manifest = tmp_path / 'm.jsonl'

    def _api(_method, _path, _body=None):
        seen.append('delete' if manifest.exists() else 'delete-before-manifest')

    monkeypatch.setattr(provider, 'api', _api)
    swarmctl.cmd_prune_issues(
        provider,
        argparse.Namespace(repo='O/R', older_than_days=7.0, keep_label=None, manifest=str(manifest), yes=True),
    )
    assert seen == ['delete', 'delete'], seen


def test_a_REFUSED_deletion_fails_the_run(swarmctl, monkeypatch, tmp_path):
    """Deletion needs OWNER or ADMIN -- measured: all four role credentials get 403. A sweep that
    printed a count while deleting nothing is the shape this repo hunts, so the refusal RAISES.
    """
    provider = swarmctl.GiteaProvider('http://host:9000', 'Org', None, 'admin')
    pages = [[_item(1)], []]
    monkeypatch.setattr(provider, 'api_list', lambda _m, _p: pages.pop(0) if pages else [])

    refusal = '403: token does not have at least one of required scope(s)'

    def _refuse(_m, _p, _b=None):
        raise swarmctl.Fail(refusal)

    monkeypatch.setattr(provider, 'api', _refuse)
    with pytest.raises(swarmctl.Fail, match='refused'):
        swarmctl.cmd_prune_issues(
            provider,
            argparse.Namespace(
                repo='O/R', older_than_days=7.0, keep_label=None, manifest=str(tmp_path / 'm.jsonl'), yes=True
            ),
        )


def test_it_never_asks_for_OPEN_items(swarmctl, monkeypatch):
    """THE SCOPE CLAIM, and it is checked at the REQUEST rather than by filtering afterwards. An
    open item is either live work or a leaked claim; the first is someone's job in flight and the
    second is a defect this would hide. Asking only for `state=closed` makes the guarantee
    structural instead of a filter someone can reorder.
    """
    asked: list[str] = []
    provider = swarmctl.GiteaProvider('http://host:9000', 'Org', None, 'admin')

    def _list(_method, path):
        asked.append(path)
        return []

    monkeypatch.setattr(provider, 'api_list', _list)
    swarmctl.cmd_prune_issues(
        provider,
        argparse.Namespace(repo='O/R', older_than_days=7.0, keep_label=None, manifest=None, yes=False),
    )
    assert asked and all('state=closed' in path for path in asked), asked


def test_the_cutoff_is_measured_from_NOW(swarmctl):
    """Guards the arithmetic itself: a cutoff computed the wrong way round would delete everything
    NEWER than N days, which is the exact inverse and reads identically in a log.
    """
    assert swarmctl._parse_iso8601(_stamp(30)) < time.time() - 7 * 86400 < swarmctl._parse_iso8601(_stamp(1))
