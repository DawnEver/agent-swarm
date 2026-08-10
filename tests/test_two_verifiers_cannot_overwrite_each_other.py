"""Two verifiers, disagreeing, on one commit: the safe direction must win.

THE DEFECT. A commit status is keyed by (sha, context) and a second POST for the same pair REPLACES
the first -- that is written down in `StatusPublisher`'s own docstring, as a FEATURE (a retry is
safe). It is a feature for ONE writer and a correctness hole for two, and there are three: WS1, WS2
and G all hold the `swarm-verifier` credential today, so all three satisfy the publisher's
construction check and all three write the same key. A box that finished a stale tree a second later
overwrites a real FAIL with a PASS, and the merge gate -- which reads exactly that key -- lets the
change through. Gitea cannot delete a commit status, so the wrong answer is permanent.

WHY THE AVAILABILITY HALF AND THE CORRECTNESS HALF ARE NOT THE SAME FIX, and the whole design turns
on this. "Only a box holding the verifier credential can publish" makes a single box a single point
of failure; the deployment already answered that by giving three boxes the credential. THAT ANSWER
IS WHAT CREATED THE OVERWRITE. Redundancy and a single shared key are in direct tension: every step
towards availability adds a writer to one slot, and every writer added makes last-write-wins more
likely to be wrong. Neither half can be fixed by tightening the other.

WHAT RESOLVES BOTH AT ONCE is to stop sharing the key. A publisher writes
`<context>/<runner>` -- one key per writer, so the writer set may grow without bound and no write can
ever land on another's slot. Single-writer-per-fact, which is this architecture's core invariant,
applied to the one slot that had escaped it. Availability then comes free: any number of boxes may
publish, and a box being down removes its own context rather than the whole signal.

KEYED BY THE WRITER, NOT BY THE JOB, and that distinction is load-bearing. A job-keyed context
(`<context>/<testkey>`) would put two runners answering the same job back into one slot, which is
the original defect with extra steps -- and it is the likelier collision, since a runner whose lease
expired and a runner that took over are answering the SAME job by construction. The writer's
identity is the only key nothing else can claim.

THE MERGE DECISION THEN BECOMES AN AGGREGATE over every matching context, and `merge_decision` states
it here so it is testable off the server. Two properties it must have, both of them the reason this
file exists:

* a single non-success among the matching contexts BLOCKS, whatever the others say and whatever
  order they arrived in;
* NO matching context at all also BLOCKS. A merge must never proceed on no information, and "no
  status" is exactly no information -- it is what a fleet that is entirely down looks like.
"""

from __future__ import annotations

import pytest

from agent_swarm.forge import ROLE_ACCOUNTS, ForgeError
from agent_swarm.status import StatusPublisher, merge_decision
from agent_swarm.testing import RecordingForge
from conftest import TEST_CONTEXT as STATUS_CONTEXT

SHA = 'deadbeefcafe'


def _verifier() -> RecordingForge:
    return RecordingForge(username=ROLE_ACCOUNTS['verifier'])


def _states(forge: RecordingForge, sha: str) -> dict[str, str]:
    """(context -> state) for one sha, as a reader of the forge would see it."""
    return {ctx: state for (s, ctx), (state, _detail) in forge.statuses.items() if s == sha}


# ------------------------------------------------------- the discriminating case: they disagree


def test_a_pass_from_one_verifier_cannot_erase_a_fail_from_another():
    """THE DEFECT ITSELF, in the order that used to lose the FAIL: fail first, pass second.

    Before per-writer keys both POSTs landed on ('sha', <the bare context>) and the second one won,
    so this assertion read one entry holding 'success' -- a green merge gate over a red gate run.
    """
    forge = _verifier()
    StatusPublisher(forge, context=STATUS_CONTEXT, runner='ws1').publish(SHA, verdict='FAIL', detail='gate red')
    StatusPublisher(forge, context=STATUS_CONTEXT, runner='ws2').publish(SHA, verdict='PASS', detail='gate green')

    states = _states(forge, SHA)
    assert states == {f'{STATUS_CONTEXT}/ws1': 'failure', f'{STATUS_CONTEXT}/ws2': 'success'}
    assert not merge_decision(states, context=STATUS_CONTEXT).allowed


def test_the_other_order_blocks_too():
    """Order-independence is the property, not "the last one happens to be safe". A rule that only
    held when the FAIL arrived second would be a race dressed as a guarantee.
    """
    forge = _verifier()
    StatusPublisher(forge, context=STATUS_CONTEXT, runner='ws2').publish(SHA, verdict='PASS', detail='gate green')
    StatusPublisher(forge, context=STATUS_CONTEXT, runner='ws1').publish(SHA, verdict='FAIL', detail='gate red')
    assert not merge_decision(_states(forge, SHA), context=STATUS_CONTEXT).allowed


def test_an_inconclusive_from_one_verifier_blocks_a_pass_from_another():
    """INCONCLUSIVE is not a weaker FAIL for this purpose. Nobody found out, so the merge must not
    proceed -- and its `error` state is exactly as blocking as `failure`.
    """
    forge = _verifier()
    StatusPublisher(forge, context=STATUS_CONTEXT, runner='ws1').publish(
        SHA, verdict='INCONCLUSIVE', detail='node down'
    )
    StatusPublisher(forge, context=STATUS_CONTEXT, runner='ws2').publish(SHA, verdict='PASS', detail='gate green')
    decision = merge_decision(_states(forge, SHA), context=STATUS_CONTEXT)
    assert not decision.allowed
    assert decision.blocking == (f'{STATUS_CONTEXT}/ws1',)


def test_agreement_still_admits_a_merge():
    """The discriminating half. A rule that blocked everything would be trivially safe and would
    freeze `main` -- the exact failure `status.py` was written to prevent.
    """
    forge = _verifier()
    StatusPublisher(forge, context=STATUS_CONTEXT, runner='ws1').publish(SHA, verdict='PASS', detail='green')
    StatusPublisher(forge, context=STATUS_CONTEXT, runner='ws2').publish(SHA, verdict='PASS', detail='green')
    assert merge_decision(_states(forge, SHA), context=STATUS_CONTEXT).allowed


# ------------------------------------------------------- one writer may still change its mind


def test_a_runner_republishing_replaces_its_OWN_answer():
    """Idempotence is preserved WITHIN a writer, which is what made the shared key attractive. A
    retry after a lost response, or a re-run that changes its mind, must not accumulate contexts.
    """
    forge = _verifier()
    publisher = StatusPublisher(forge, context=STATUS_CONTEXT, runner='ws1')
    publisher.publish(SHA, verdict='PASS', detail='first')
    publisher.publish(SHA, verdict='FAIL', detail='on reflection')
    assert _states(forge, SHA) == {f'{STATUS_CONTEXT}/ws1': 'failure'}


def test_two_publishers_with_the_same_runner_name_share_a_key():
    """Stated so nobody reads per-writer keying as per-PROCESS keying. The name is the identity, so
    two processes claiming one name are one writer as far as this mechanism is concerned -- which is
    why the runner name must be the box's, not a per-run token.
    """
    forge = _verifier()
    StatusPublisher(forge, context=STATUS_CONTEXT, runner='ws1').publish(SHA, verdict='FAIL', detail='a')
    StatusPublisher(forge, context=STATUS_CONTEXT, runner='ws1').publish(SHA, verdict='PASS', detail='b')
    assert _states(forge, SHA) == {f'{STATUS_CONTEXT}/ws1': 'success'}


# ------------------------------------------------------- no information is not a pass


def test_no_status_at_all_blocks():
    """A merge must never proceed on no information. An empty mapping is what a fleet that is
    entirely down looks like, and it is the reading a naive `all(...)` would call green.
    """
    decision = merge_decision({}, context=STATUS_CONTEXT)
    assert not decision.allowed
    assert 'no' in decision.reason


def test_a_status_on_an_UNRELATED_context_does_not_count_as_information():
    """Some other producer's green must not satisfy this gate. It is not evidence about our gate,
    and treating it as such would let any repo integration unblock a merge.
    """
    forge = _verifier()
    forge.set_status(SHA, state='success', context='ci/lint', description='fine')
    assert not merge_decision(_states(forge, SHA), context=STATUS_CONTEXT).allowed


def test_a_context_that_merely_starts_with_the_same_letters_does_not_count():
    """`motronics/gateway/x` is not `motronics/gate/<runner>`. A prefix test written with a bare
    `startswith` matches it, and the glob a branch rule uses (`<context>/*`) does not -- so the two
    would disagree about which statuses the gate is made of, which is a scope-lie with a merge on
    the end of it.
    """
    states = {f'{STATUS_CONTEXT}way/x': 'success'}
    assert not merge_decision(states, context=STATUS_CONTEXT).allowed


def test_the_bare_context_is_not_counted_either():
    """A status written to the UNSUFFIXED context is ignored, and that is deliberate: the branch
    rule's glob does not match it either, so counting it here would make this function claim
    authority the server does not grant. Nothing in this package can write it -- `runner` is
    required and may not be empty -- so the only source is a legacy or foreign writer.
    """
    assert not merge_decision({STATUS_CONTEXT: 'success'}, context=STATUS_CONTEXT).allowed


def test_a_pending_status_blocks():
    """`pending` is a run in flight. Admitting it would merge on a gate that has not answered."""
    states = {f'{STATUS_CONTEXT}/ws1': 'success', f'{STATUS_CONTEXT}/ws2': 'pending'}
    assert not merge_decision(states, context=STATUS_CONTEXT).allowed


# ------------------------------------------------------- the runner name is validated before I/O


@pytest.mark.parametrize('runner', ['', ' ', 'ws 1', 'ws/1', 'ws\t1', '*'])
def test_a_runner_name_that_would_break_the_key_is_refused_at_construction(runner: str):
    """Refused where the publisher is BUILT, for the same reason the role is: a commit status cannot
    be deleted, so a name that produced a colliding or unmatchable context would be permanent.

    An empty name would rebuild the shared key. A `/` would forge a context under someone else's
    namespace, and a `*` is a glob metacharacter in the branch rule that reads this.
    """
    with pytest.raises(ForgeError, match='runner'):
        StatusPublisher(_verifier(), context=STATUS_CONTEXT, runner=runner)


def test_the_runner_name_is_required():
    """No default. A default would be one shared spelling across the fleet, which is the shared key
    again -- arrived at by convenience rather than by design, which is the harder version to see.
    """
    with pytest.raises(TypeError):
        StatusPublisher(_verifier(), context=STATUS_CONTEXT)  # type: ignore[call-arg]


def test_a_non_verifier_is_still_refused():
    """The role check is not replaced by this change. Per-writer keys stop one verifier erasing
    another; they say nothing about who may write at all.
    """
    with pytest.raises(ForgeError, match='verifier'):
        StatusPublisher(RecordingForge(username=ROLE_ACCOUNTS['agent']), context=STATUS_CONTEXT, runner='ws1')


def test_the_published_context_is_reported_by_the_publisher():
    """The operator configuring branch protection needs the exact spelling, and re-deriving it by
    hand is how the two copies of `STATUS_CONTEXT` drifted in the first place.
    """
    assert StatusPublisher(_verifier(), context=STATUS_CONTEXT, runner='ws1').context == f'{STATUS_CONTEXT}/ws1'
