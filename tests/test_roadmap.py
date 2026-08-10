"""What a roadmap must produce, stated before the producer exists.

The properties that matter: ids are stable across a re-read, a MATERIAL change moves them and a
scheduling-hint change does not, an item with no acceptance criterion is refused rather than queued,
and a dependency cycle raises instead of deadlocking silently.
"""

from __future__ import annotations

import pytest

from agent_swarm.allocator import choose
from agent_swarm.job import AGENT_TASK
from agent_swarm.loop import Box
from agent_swarm.roadmap import RoadmapError, load, loads

IDLE = Box(available_gib=64.0)

ROADMAP = """
version = 1

[[item]]
key = 'spool-retention'
title = 'Retention for refs/ci/shards and refs/candidates'
acceptance = 'pytest tests/test_spool.py -q'
rem = 'human'
priority = 5

[[item]]
key = 'testkey-index'
title = 'CLI-owned testkey -> number index'
acceptance = 'pytest tests/test_forge_store.py -q'
rem = 'human'
priority = 7
needs = ['spool-retention']
"""


def _item(**kw) -> dict:
    base = {'key': 'k', 'title': 't', 'acceptance': 'pytest -q', 'rem': 'human'}
    base.update(kw)
    return {'version': 1, 'item': [base]}


class TestSchema:
    def test_a_roadmap_becomes_agent_task_jobs(self) -> None:
        roadmap = loads(ROADMAP)
        assert [i.key for i in roadmap.items] == ['spool-retention', 'testkey-index']
        assert all(i.job.kind is AGENT_TASK for i in roadmap.items)

    def test_scheduling_hints_reach_the_job(self) -> None:
        roadmap = load(_item(exclusivity='cheap', ram_gib=0.5))
        job = roadmap.items[0].job
        assert job.exclusivity == 'cheap'
        assert job.ram_gib == 0.5

    def test_an_unpriced_item_is_legal(self) -> None:
        # The common case. `ram_gib is None` means unmeasured, never free.
        assert load(_item()).items[0].job.ram_gib is None

    def test_an_unknown_field_is_refused(self) -> None:
        # A manifest is DATA. An unrecognised key is either a typo whose value is silently ignored,
        # or narrative arriving in a file that must not carry it -- both refused at load.
        with pytest.raises(RoadmapError, match='rationale'):
            load(_item(rationale='because it seemed like a good idea'))

    def test_a_duplicate_key_is_refused(self) -> None:
        data = {'version': 1, 'item': [{'key': 'k', 'title': 'a', 'acceptance': 'x', 'rem': 'human'}] * 2}
        with pytest.raises(RoadmapError, match='duplicate'):
            load(data)

    def test_an_unknown_version_is_refused(self) -> None:
        with pytest.raises(RoadmapError, match='version'):
            load({'version': 99, 'item': []})


class TestAcceptance:
    """An item whose done-ness cannot be checked is not a work item. It RAISES."""

    @pytest.mark.parametrize('bad', [None, '', '   '])
    def test_an_item_without_an_acceptance_criterion_raises(self, bad) -> None:
        # DEFENCE: the verdict vocabulary is PASS/FAIL/INCONCLUSIVE for both kinds, and there is no
        # fourth word for "unanswerable". Labelling such an item would put work in the queue that no
        # executor can ever answer -- it would be claimed, run, and then need a verdict nobody can
        # give, which is exactly how INCONCLUSIVE stops meaning "infrastructure broke". The roadmap
        # is human-authored and read at load time, so raising is loud at the moment the human is
        # looking, and costs them one line.
        data = _item()
        if bad is None:
            del data['item'][0]['acceptance']
        else:
            data['item'][0]['acceptance'] = bad
        with pytest.raises(RoadmapError, match='acceptance'):
            load(data)

    def test_the_criterion_travels_with_the_item(self) -> None:
        assert load(_item(acceptance='pytest -q -k spool')).items[0].acceptance == 'pytest -q -k spool'


class TestIdStability:
    def test_the_same_roadmap_gives_the_same_ids(self) -> None:
        assert [i.job.id for i in loads(ROADMAP).items] == [i.job.id for i in loads(ROADMAP).items]

    def test_reordering_the_items_does_not_move_an_id(self) -> None:
        first = load(_item(key='a'))
        data = {
            'version': 1,
            'item': [{'key': 'z', 'title': 't', 'acceptance': 'pytest -q', 'rem': 'human'}, _item(key='a')['item'][0]],
        }
        assert load(data).by_key['a'].job.id == first.by_key['a'].job.id

    @pytest.mark.parametrize('hint', [{'priority': 9}, {'ram_gib': 4.0}, {'exclusivity': 'cheap'}])
    def test_a_scheduling_hint_change_keeps_the_id(self, hint) -> None:
        # MATERIAL means: the work to be done, or the standard it is judged by -- `title` and
        # `acceptance`. A hint changes WHEN and WHERE a job runs, never WHAT it is, so moving the id
        # would abandon the verdict history of work that did not change.
        assert load(_item(**hint)).items[0].job.id == load(_item()).items[0].job.id

    @pytest.mark.parametrize('material', [{'title': 'a different job'}, {'acceptance': 'pytest -q -k other', 'rem': 'human'}])
    def test_a_material_change_moves_the_id(self, material) -> None:
        # A verdict is a statement about a specific brief judged by a specific criterion. Keeping
        # the id would let an old PASS answer the new item -- the unearned green, by re-editing.
        assert load(_item(**material)).items[0].job.id != load(_item()).items[0].job.id

    def test_the_key_is_visible_in_the_id(self) -> None:
        # A bare hash in an issue title is unreadable by the human who owns the roadmap.
        assert load(_item(key='spool-retention')).items[0].job.id.startswith('spool-retention@')


class TestDependencies:
    def test_a_cycle_raises(self) -> None:
        data = {
            'version': 1,
            'item': [
                {'key': 'a', 'title': 't', 'acceptance': 'x', 'rem': 'human', 'needs': ['b']},
                {'key': 'b', 'title': 't', 'acceptance': 'x', 'rem': 'human', 'needs': ['a']},
            ],
        }
        with pytest.raises(RoadmapError, match='cycle'):
            load(data)

    def test_a_self_dependency_is_a_cycle(self) -> None:
        with pytest.raises(RoadmapError, match='cycle'):
            load(_item(needs=['k']))

    def test_a_longer_cycle_is_named_not_merely_reported(self) -> None:
        # 'there is a cycle somewhere' sends the human through the whole file.
        data = {
            'version': 1,
            'item': [
                {'key': 'a', 'title': 't', 'acceptance': 'x', 'rem': 'human', 'needs': ['b']},
                {'key': 'b', 'title': 't', 'acceptance': 'x', 'rem': 'human', 'needs': ['c']},
                {'key': 'c', 'title': 't', 'acceptance': 'x', 'rem': 'human', 'needs': ['a']},
            ],
        }
        with pytest.raises(RoadmapError, match=r'a -> b -> c -> a'):
            load(data)

    def test_a_diamond_is_not_a_cycle(self) -> None:
        # A shared dependency reached by two paths is an ordinary DAG. A cycle check that raised
        # here would refuse a legal roadmap -- and the human's only recourse would be to delete a
        # real dependency, converting a false alarm into an actual ordering bug.
        data = {
            'version': 1,
            'item': [
                {'key': 'base', 'title': 't', 'acceptance': 'x', 'rem': 'human'},
                {'key': 'left', 'title': 't', 'acceptance': 'x', 'rem': 'human', 'needs': ['base']},
                {'key': 'right', 'title': 't', 'acceptance': 'x', 'rem': 'human', 'needs': ['base']},
                {'key': 'top', 'title': 't', 'acceptance': 'x', 'rem': 'human', 'needs': ['left', 'right']},
            ],
        }
        assert len(load(data).items) == 4

    def test_an_unknown_dependency_raises(self) -> None:
        with pytest.raises(RoadmapError, match='nonesuch'):
            load(_item(needs=['nonesuch']))

    def test_an_item_with_unmet_needs_is_not_offered(self) -> None:
        roadmap = loads(ROADMAP)
        offered = [c.job.id for c in roadmap.candidates(done=frozenset())]
        assert offered == [roadmap.by_key['spool-retention'].job.id]

    def test_satisfying_the_dependency_offers_the_dependent(self) -> None:
        roadmap = loads(ROADMAP)
        offered = [c.job.id for c in roadmap.candidates(done=frozenset({'spool-retention'}))]
        assert roadmap.by_key['testkey-index'].job.id in offered


class TestItFeedsTheAllocator:
    def test_candidates_are_allocatable(self) -> None:
        roadmap = loads(ROADMAP)
        cands = roadmap.candidates(done=frozenset({'spool-retention'}))
        # priority 7 beats priority 5 at equal age.
        assert choose(cands, IDLE, now=0.0) == roadmap.by_key['testkey-index'].job

    def test_an_unrecorded_wait_ages_rather_than_starves(self) -> None:
        # `ready_at` is a fact only the store holds. A missing one defaults to "waiting since the
        # epoch" -- maximally aged. That errs toward PICKING an item, which costs one out-of-order
        # run; the opposite default (`now`) would reset the age on every tick and starve the item
        # forever, silently, which is the failure this whole design is against.
        roadmap = loads(ROADMAP)
        assert all(c.ready_at == 0.0 for c in roadmap.candidates(done=frozenset()))
        assert roadmap.candidates(done=frozenset(), ready_at={'spool-retention': 42.0})[0].ready_at == 42.0
