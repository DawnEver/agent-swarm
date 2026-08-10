"""Every roadmap item states its PROVENANCE, and the rem bridge checks the two directions.

THE PROBLEM THIS SOLVES. The backlog had three homes -- `roadmap.toml`, rem's `manual.md` /
`sharp-review.md` findings, and prose in `.claude/memory/` -- and no relation between them. Three
homes for one fact class is the session's dominant defect at the level of a workflow: a rem finding
could be worked, closed and forgotten while its roadmap twin still queued; a roadmap item could
carry no trace of the observation that justified it, so nobody could tell an admitted intention from
a leftover.

THE RULE IS ONE WRITER PER FACT, and the fields say which:

    rem  ("was this NOTICED, and is it still open")  -> owned by rem's scan of memory/**
    roadmap ("was it ADMITTED as intent")            -> owned by `roadmap.toml`'s `rem =` field
    forge issue ("what is its WORK STATE")           -> owned by Gitea, because it must be contended

So this bridge NEVER writes into rem. It reads both sides and reports drift, and admission stays a
human action -- which is the whole reason `rem` is a required field rather than something inferred.

`rem = 'human'` IS A REAL ANSWER, not an escape hatch. Plenty of intent is born of the human's
vision and was never a finding. What is refused is SILENCE: an item with no `rem` at all leaves the
reader to supply "presumably someone wanted this", and that assumption is exactly what let leftovers
survive. Saying `human` puts the claim on record where it can be disputed.

WHY `rem` IS NOT MATERIAL. `_MATERIAL_FIELDS` decides the job id, and a recorded verdict is a
statement about a brief under a criterion. Where the idea came from changes neither, so correcting a
provenance typo must not invalidate a PASS.
"""

from __future__ import annotations

import pytest

from agent_swarm.rem_bridge import RemTask, check
from agent_swarm.roadmap import RoadmapError, loads

BASE = """
version = 1

[[item]]
key = "spool-retention"
title = "retention for refs/ci"
acceptance = "pytest tests/test_spool.py -q"
rem = "MANUAL-20260614-001"
"""


# --------------------------------------------------------------------------- the field


def test_an_item_carries_its_provenance():
    assert loads(BASE).by_key['spool-retention'].rem == 'MANUAL-20260614-001'


def test_an_item_with_no_provenance_is_REFUSED():
    """THE GAP ITSELF. Silence reads as "presumably someone wanted this", and that assumption is
    what let a leftover survive beside admitted intent, indistinguishable from it.
    """
    with pytest.raises(RoadmapError, match='rem'):
        loads(BASE.replace('rem = "MANUAL-20260614-001"\n', ''))


def test_human_is_a_legitimate_provenance():
    """The discriminating half: a rule that only accepted finding ids would make the roadmap unable
    to express intent born of the human's own vision -- most of it, in fact.
    """
    assert loads(BASE.replace('"MANUAL-20260614-001"', '"human"')).by_key['spool-retention'].rem == 'human'


def test_a_blank_provenance_is_REFUSED():
    """`rem = ""` satisfies "the key is present" while saying nothing. That is the disguise: a field
    added to pass a check rather than to carry a fact.
    """
    with pytest.raises(RoadmapError, match='rem'):
        loads(BASE.replace('"MANUAL-20260614-001"', '""'))


def test_provenance_does_NOT_change_the_job_id():
    """A recorded verdict is a statement about a BRIEF under a CRITERION. Correcting a provenance
    typo must not throw away a PASS -- and if `rem` were material, every such edit would.
    """
    one = loads(BASE).by_key['spool-retention'].job.id
    two = loads(BASE.replace('"MANUAL-20260614-001"', '"SR-023"')).by_key['spool-retention'].job.id
    assert one == two


def test_the_title_still_DOES_change_the_job_id():
    """The other half of the same claim: if nothing changed the id, the test above would pass for a
    reason that has nothing to do with provenance.
    """
    one = loads(BASE).by_key['spool-retention'].job.id
    two = loads(BASE.replace('retention for refs/ci', 'something else entirely')).by_key['spool-retention'].job.id
    assert one != two


# --------------------------------------------------------------------------- the two directions


def _open(task_id: str, summary: str = 's') -> RemTask:
    return RemTask(id=task_id, summary=summary, checked=False, severity='MEDIUM')


def test_a_roadmap_item_naming_a_finding_NOBODY_RECORDED_is_a_problem():
    """The dangling direction. A provenance field that names nothing is the declaration-that-lies
    shape: it looks like evidence and cannot be followed to any.
    """
    problems = check(loads(BASE), [_open('MANUAL-99999999-001')])
    assert any('MANUAL-20260614-001' in p for p in problems), problems


def test_a_matching_finding_is_NOT_a_problem():
    """The discriminating half -- a checker that flagged everything would be ignored within a week."""
    assert check(loads(BASE), [_open('MANUAL-20260614-001')]) == []


def test_human_provenance_needs_no_finding():
    assert check(loads(BASE.replace('"MANUAL-20260614-001"', '"human"')), []) == []


def test_a_CLOSED_finding_still_backing_an_open_roadmap_item_is_a_problem():
    """The direction that actually bites on a 7x24 fleet: rem says the observation is resolved while
    the roadmap still queues work for it, so the swarm keeps spending on a settled question.
    """
    closed = RemTask(id='MANUAL-20260614-001', summary='s', checked=True, severity='MEDIUM')
    problems = check(loads(BASE), [closed])
    assert any('closed' in p for p in problems), problems


def test_an_open_finding_NOT_on_the_roadmap_is_reported_but_is_not_an_ERROR():
    """A noticed-but-unadmitted finding is the NORMAL state -- admission is a human act and most
    findings never earn it. It is reported so the backlog is visible, and separated from the two
    real inconsistencies so the report does not drown them.
    """
    from agent_swarm.rem_bridge import unpromoted

    assert unpromoted(loads(BASE), [_open('MANUAL-20260614-001'), _open('SR-099')]) == ['SR-099']
