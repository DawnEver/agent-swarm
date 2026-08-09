"""The Gitea store: the SAME contract as `test_store.py`, plus what only a real backend can get wrong.

THE CONTRACT SUITE IS REUSED, NOT RESTATED. `TestGiteaSatisfiesTheContract*` subclass the classes in
`test_store.py` and override one fixture. Every assertion there -- including the sixteen-thread race
-- runs unchanged against the real server. Copying them here would have let the two drift, and the
copy would inevitably have been the weaker one.

WHY THE REF AND NOT THE ASSIGNEE. Measured against this deployment (Gitea 1.26.4) on 2026-08-09:

    PATCH /issues/{n} assignees   12 concurrent racers -> 12x 201.  Last-write-wins, no CAS.
    POST /labels (duplicate name) 12 concurrent racers -> 12x 201, 12 identical labels. No CAS.
    POST /git/refs                405. Not enabled here.
    git push <sha>:refs/<new>      8 concurrent racers -> exactly 1 rc=0, 7 rejected. A CAS.

Only the last one refuses a second writer, so only the last one can implement `try_claim`. The
Issue API is used for the half that needs no atomicity: the verdict.

THE OFFLINE HALF IS NOT A MOCK OF THE ONLINE HALF. It tests the pure decisions -- which ref name a
job contends for, what the lease payload says, which words are refused -- and those are exactly the
places where a wrong answer would make the online CAS silently stop being one. Deselect the rest
with `-m 'not network'`; nothing here monkeypatches the code under test.
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest

from agent_swarm.gitea import VERDICT_LABELS, GiteaStore, claim_ref, decode_claim, encode_claim
from agent_swarm.job import AGENT_TASK, TEST_RUN, Job
from agent_swarm.store import VERDICTS, Store

# Imported under private names ON PURPOSE: a name starting with `Test` is COLLECTED wherever it is
# bound, so importing them plainly would re-run the in-memory suite here -- against a `store`
# fixture this module does not define, i.e. fourteen errors that say nothing about Gitea.
from test_store import TestClaimIsCompareAndSwap as _ClaimContract
from test_store import TestItIsATOMICUnderRealConcurrency as _AtomicityContract
from test_store import TestVerdictsAreRecorded as _VerdictContract

JOB = Job(id='j1', kind=TEST_RUN)


class TestTheRefNameIsTheContentionTOKEN:
    """The owner must NOT appear in the ref name. Everything else is a detail; this is the design.

    A create-only push is a CAS over ONE NAME. Put the owner in the name and sixteen runners push
    sixteen different refs, every one of them succeeds, and `try_claim` returns True to all of them
    while still looking, in the log, exactly like a CAS. That is push-then-arbitrate wearing the
    new interface's clothes -- so the owner goes in the pushed COMMIT, which the winner alone gets
    to write.
    """

    def test_two_owners_contend_for_the_SAME_ref(self):
        assert claim_ref('ns', JOB) == claim_ref('ns', JOB)

    def test_the_owner_appears_nowhere_in_the_ref(self):
        assert 'runner-a' not in claim_ref('ns', JOB)

    def test_different_jobs_get_different_refs(self):
        assert claim_ref('ns', JOB) != claim_ref('ns', Job(id='j2', kind=TEST_RUN))

    def test_the_KIND_separates_two_jobs_of_one_id(self):
        """`claim_key` already says so; the ref must not flatten it back together."""
        assert claim_ref('ns', JOB) != claim_ref('ns', Job(id='j1', kind=AGENT_TASK))

    def test_namespaces_do_not_collide(self):
        assert claim_ref('ns-a', JOB) != claim_ref('ns-b', JOB)

    def test_it_is_under_refs_claims_and_touches_no_reserved_namespace(self):
        """`refs/heads`, `refs/candidates`, `refs/verdicts` and `refs/ci` belong to other systems."""
        ref = claim_ref('ns', JOB)
        assert ref.startswith('refs/claims/')

    def test_a_key_with_ref_hostile_characters_is_still_a_LEGAL_ref(self):
        """Git refuses `..`, a trailing `.lock`, spaces and `~^:?*[`. An id is free text, so an
        unsanitised one turns a claim into a push that always fails -- a job nothing can ever take.
        """
        ref = claim_ref('ns', Job(id='a b..c~d:e.lock', kind=TEST_RUN))
        assert '..' not in ref
        assert not any(c in ref for c in ' ~^:?*[\\')
        assert not ref.endswith('.lock')

    def test_sanitising_does_not_MERGE_two_distinct_ids(self):
        """The cheap sanitiser maps `a b` and `a-b` onto one name, and two different jobs sharing a
        claim is the deadlock the mechanism exists to prevent -- silently, since neither errors.
        """
        left = claim_ref('ns', Job(id='a b', kind=TEST_RUN))
        right = claim_ref('ns', Job(id='a-b', kind=TEST_RUN))
        assert left != right


class TestTheLeaseIsCARRIEDByTheClaim:
    """A create-only ref has no expiry, so a machine that dies holds the job forever.

    The payload is what fixes that, and it is why the owner and the timestamp must travel in the
    commit rather than the name: the name is spent on being identical for every racer.
    """

    def test_the_payload_round_trips(self):
        text = encode_claim(owner='runner-a', claimed_at=1000.0, lease_seconds=300.0)
        claim = decode_claim(text)
        assert claim.owner == 'runner-a'
        assert claim.claimed_at == pytest.approx(1000.0)
        assert claim.lease_seconds == pytest.approx(300.0)

    def test_a_fresh_claim_is_not_expired(self):
        claim = decode_claim(encode_claim(owner='a', claimed_at=time.time(), lease_seconds=300.0))
        assert claim.is_expired(now=time.time()) is False

    def test_a_claim_past_its_lease_IS_expired(self):
        claim = decode_claim(encode_claim(owner='a', claimed_at=1000.0, lease_seconds=300.0))
        assert claim.is_expired(now=1301.0) is True

    def test_the_boundary_is_not_yet_expired(self):
        claim = decode_claim(encode_claim(owner='a', claimed_at=1000.0, lease_seconds=300.0))
        assert claim.is_expired(now=1300.0) is False

    def test_an_UNREADABLE_payload_raises_rather_than_reading_as_unheld(self):
        """ "Corrupt" must not decode to "free". A claim nobody can parse is one a second runner
        would take while the first is still working -- the exact failure the CAS removes.
        """
        with pytest.raises(ValueError, match='claim'):
            decode_claim('not json at all')

    def test_a_payload_missing_the_OWNER_raises(self):
        with pytest.raises(ValueError, match='claim'):
            decode_claim('{"claimed_at": 1.0, "lease_seconds": 2.0}')

    def test_each_attempt_encodes_a_DISTINCT_nonce(self):
        """Two attempts by the same owner in the same second would otherwise build a byte-identical
        commit, hence the same sha -- and `git push` calls pushing a ref to the value it already has
        a SUCCESS. The CAS would answer True to a re-claim it is contractually required to refuse.
        """
        one = encode_claim(owner='a', claimed_at=1000.0, lease_seconds=300.0)
        two = encode_claim(owner='a', claimed_at=1000.0, lease_seconds=300.0)
        assert one != two


class TestTheVerdictVocabularyIsClosedBEFOREAnyNetworkCall:
    def test_every_verdict_has_exactly_one_label(self):
        assert set(VERDICT_LABELS) == VERDICTS
        assert len(set(VERDICT_LABELS.values())) == len(VERDICTS)

    def test_a_fourth_word_is_refused_without_touching_the_server(self):
        """Validation precedes I/O, so an unreachable server is not what makes this fail. A store
        that validated after the POST would leave a comment on the issue for a verdict it then
        rejected.
        """
        store = GiteaStore(namespace='probe-never-contacted', base_url='http://127.0.0.1:1')
        with pytest.raises(ValueError, match='verdict'):
            store.record_verdict(JOB, verdict='ERROR', detail='')


class TestItIsAStore:
    def test_the_protocol_is_satisfied(self):
        store = GiteaStore(namespace='probe-never-contacted', base_url='http://127.0.0.1:1')
        assert isinstance(store, Store)


# --------------------------------------------------------------------------------------------
# Everything below talks to the real server.
# --------------------------------------------------------------------------------------------


@pytest.fixture
def gitea_store():
    """A store in a namespace nobody else owns, purged afterwards.

    THE NAMESPACE IS PER-TEST because the server is shared and the contract suite hard-codes one
    job id. Two runs of this file at once would otherwise contend for `refs/claims/test-run/j1` and
    each would report the other's claim as a contract violation.
    """
    store = GiteaStore(namespace=f'probe-{uuid.uuid4().hex[:12]}')
    try:
        yield store
    finally:
        store.purge_namespace()


@pytest.mark.network
class TestGiteaSatisfiesTheContract(_ClaimContract):
    @pytest.fixture
    def store(self, gitea_store):
        return gitea_store


@pytest.mark.network
class TestGiteaIsAtomicOnTheRealServer(_AtomicityContract):
    @pytest.fixture
    def store(self, gitea_store):
        return gitea_store


@pytest.mark.network
class TestGiteaRecordsVerdicts(_VerdictContract):
    @pytest.fixture
    def store(self, gitea_store):
        return gitea_store


@pytest.mark.network
class TestWhatOnlyTheRealBackendCanGetWrong:
    def test_an_EXPIRED_lease_is_reclaimable_by_someone_else(self, gitea_store):
        """A create-only ref never expires on its own, so without this a crashed machine takes the
        job out of the fleet permanently -- and it looks healthy, because the claim is valid.
        """
        short = GiteaStore(namespace=gitea_store.namespace, lease_seconds=0.0)
        assert short.try_claim(JOB, owner='runner-dead') is True
        assert short.claim_owner(JOB) is None, 'an expired claim must read as unheld'
        assert short.try_claim(JOB, owner='runner-b') is True
        assert short.claim_owner(JOB) is None  # still zero-lease; the point is the takeover happened

    def test_a_LIVE_lease_is_not_stolen(self, gitea_store):
        assert gitea_store.try_claim(JOB, owner='runner-a') is True
        assert gitea_store.try_claim(JOB, owner='runner-b') is False
        assert gitea_store.claim_owner(JOB) == 'runner-a'

    def test_exactly_one_thread_wins_an_EXPIRED_claim_too(self, gitea_store):
        """The takeover path is a second write path to the same ref, and a second write path is
        where a CAS quietly stops being one. Racing it is the only way to say it still holds.
        """
        dead = GiteaStore(namespace=gitea_store.namespace, lease_seconds=0.0)
        dead.try_claim(JOB, owner='runner-dead')

        live = [GiteaStore(namespace=gitea_store.namespace) for _ in range(8)]
        winners: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def attempt(n: int) -> None:
            barrier.wait()
            if live[n].try_claim(JOB, owner=f'runner-{n}'):
                with lock:
                    winners.append(f'runner-{n}')

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(winners) == 1, f'{len(winners)} runners took over one expired claim: {winners}'
        assert gitea_store.claim_owner(JOB) == winners[0]

    def test_the_verdict_survives_a_FRESH_store_object(self, gitea_store):
        """The point of a backing store. An in-process cache would pass every verdict test in the
        contract suite while storing nothing on the server.
        """
        gitea_store.record_verdict(JOB, verdict='INCONCLUSIVE', detail='node down')
        fresh = GiteaStore(namespace=gitea_store.namespace)
        assert fresh.verdict(JOB) == 'INCONCLUSIVE'

    def test_the_detail_is_recorded_where_a_HUMAN_will_read_it(self, gitea_store):
        """gate.py's output is the evidence behind the verdict; a verdict without it is a claim."""
        gitea_store.record_verdict(JOB, verdict='FAIL', detail='3 failed, 10643 passed')
        assert '3 failed, 10643 passed' in gitea_store.verdict_detail(JOB)

    def test_recording_a_verdict_CLOSES_the_issue(self, gitea_store):
        """The board column is driven by state; an answered job left open is one the lead re-reads
        every tick.
        """
        gitea_store.record_verdict(JOB, verdict='PASS', detail='green')
        assert gitea_store.issue_state(JOB) == 'closed'

    def test_a_second_verdict_REPLACES_the_first(self, gitea_store):
        """A retry after INCONCLUSIVE must not leave the job carrying two verdict labels, since
        nothing downstream can act on a job that is both inconclusive and green.
        """
        gitea_store.record_verdict(JOB, verdict='INCONCLUSIVE', detail='node down')
        gitea_store.record_verdict(JOB, verdict='PASS', detail='green on retry')
        assert gitea_store.verdict(JOB) == 'PASS'
