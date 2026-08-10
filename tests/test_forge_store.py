"""`ForgeStore`: the SAME contract as `test_store.py`, plus what only a real server can settle.

THE CONTRACT SUITE IS REUSED, NOT RESTATED. The `...SatisfiesTheContract` classes subclass the ones
in `test_store.py` and override a single fixture. Every assertion there -- including the
sixteen-thread race -- runs unchanged, twice: once against an in-memory forge and once against the
real server. Copying them here would have let the two drift, and the copy would inevitably have been
the weaker one.

WHAT EACH HALF CAN AND CANNOT PROVE, because this is exactly where a suite lies to itself:

* `RecordingForge` models the two properties the protocol REQUIRES of a deployment -- ids assigned
  by the server, monotonically, and a read that sees every earlier write. Racing the store against
  it proves the STORE's arbitration is right. **It can never prove the DEPLOYMENT has those
  properties**, because it was built to have them.
* The `live_forge` half runs the same arbitration against Gitea, where those properties are a measured
  fact rather than a construction. That is the only half that can say the protocol HOLDS.

Neither half is a mock of the other and nothing here monkeypatches the code under test. Deselect the
server half with `-m 'not live_forge'`.

FOUR ROUNDS, NOT ONE, for the race against the real server. One round electing one winner is what a
BROKEN protocol also does most of the time -- the failure being tested for is rare by construction,
so the evidence has to repeat.
"""

from __future__ import annotations

import ast
import io
import threading
import time
import tokenize
import uuid
from pathlib import Path

import pytest

from agent_swarm import forge_store as forge_store_module
from agent_swarm.forge import Comment, CommentGone, Forge, ForgeError, GiteaForge, WorkItem, default_forge
from agent_swarm.forge_store import (
    NOT_VISIBLE,
    VERDICT_LABELS,
    DuplicateWorkItems,
    ForgeStore,
    NotVisible,
    Role,
    decode_claim,
    encode_claim,
)
from agent_swarm.job import TEST_RUN, Job, JobKind
from agent_swarm.store import VERDICTS, Store

# Imported under private names ON PURPOSE: a name starting with `Test` is COLLECTED wherever it is
# bound, so importing them plainly would re-run the in-memory suite here -- against a `store`
# fixture this module does not define, i.e. fourteen errors that say nothing about the forge.
from test_store import TestClaimIsCompareAndSwap as _ClaimContract
from test_store import TestItIsATOMICUnderRealConcurrency as _AtomicityContract
from test_store import TestVerdictsAreRecorded as _VerdictContract

JOB = Job(id='j1', kind=TEST_RUN)


def _race_one_round(namespace: str, job: Job, *, racers: int) -> list[str]:
    """Register the job, then release `racers` RUNNERS at it; return everyone who believed they won.

    THE PRODUCTION SHAPE, and it is the shape because the alternative was measured not to work. The
    submitter creates the work item exactly once; runners may not create at all. Racing N separate
    SUBMITTERS -- which this used to do -- tests a configuration the design now forbids, and it
    would be asserting the convergence that was deleted for failing on GitHub.

    A FUNCTION AND NOT AN INLINE LOOP BODY: the closure captures the barrier, lock and winner list,
    and rebinding those per round inside a loop makes the thread bodies share whichever objects the
    LAST iteration created. It happens to be safe while every round joins before the next begins --
    which is exactly the kind of "safe for now" that a later edit turns into a race inside the test
    for races.
    """
    ForgeStore(namespace, default_forge(), role=Role.SUBMITTER).register(job)

    winners: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(racers)
    stores = [ForgeStore(namespace, default_forge(), role=Role.RUNNER) for _ in range(racers)]

    def attempt(n: int) -> None:
        barrier.wait()
        if stores[n].try_claim(job, owner=f'r{n:02d}'):
            with lock:
                winners.append(f'r{n:02d}')

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(racers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return winners


class RecordingForge:
    """An in-memory `Forge` that MODELS THE PRECONDITION, and says so.

    It assigns comment ids from a single counter under a lock -- server-assigned, monotonic, unique
    -- and every read sees every completed write. Those are precisely the two properties the claim
    protocol requires of a deployment, and they are here by CONSTRUCTION.

    So: a green run against this forge is evidence that `ForgeStore`'s arbitration is correct. It is
    NOT evidence that any real forge has these properties, and no amount of it ever will be. That is
    what the `live_forge` tests are for, and why they were measured before this was written.

    It is not a mock of the code under test -- the store's logic runs unmodified against it -- and
    it is a second genuine backend, which is what makes the vendor-neutrality claim checkable.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_id = 1
        self.items: dict[int, WorkItem] = {}
        self.bodies: dict[int, str] = {}
        self._comments: dict[int, list[Comment]] = {}
        # LABELS ARE (id, name) PAIRS, and the id is what a real removal targets.
        #
        # This double used to key labels by NAME, which made it gentler than reality on exactly the
        # axis reality has already been MEASURED to differ: `POST /labels` accepted twelve identical
        # names from twelve concurrent racers on this Gitea, so a name maps to a LIST of ids. A
        # name-keyed double cannot express a duplicate at all, so every test written through it
        # agreed that removal by name removes everything -- which is what `remove_label` does here
        # and is NOT what the vendor does.
        self.item_labels: dict[int, list[tuple[int, str]]] = {}
        #: Repo-level label definitions: name -> the ids sharing it. Non-unique BY MEASUREMENT.
        self.repo_labels: dict[str, list[int]] = {}
        self._next_label_id = 1000
        self.retired: list[int] = []

    def list_work_items(self) -> list[WorkItem]:
        """DETERMINISTICALLY SCRAMBLED. Neither first nor last correlates with the number.

        A real forge promises no ordering on a list endpoint -- Gitea and GitHub both default to
        recently-updated first, and either may change -- so a double that returns a MEANINGFUL order
        is better behaved than reality, and every ordering rule tested through it is really testing
        the double.

        This has now cost two holes, and the second is the more instructive:

        * **Insertion order** made "first match" and "LOWEST-numbered match" the same function, so
          `_lowest_numbered`'s tie-break was untestable.
        * Fixing that by REVERSING made "first match" and "HIGHEST-numbered match" the same
          function, so `newest_open`'s rule was untestable. **The fix relocated the hole rather than
          closing it**, which is this suite's own law about elimination fixes, firing on the repair
          for the previous instance of that law.

        The order here is a ROTATION by half the list, which for three or more items puts neither
        the lowest nor the highest number first -- so it is adverse to both `min` and `max` at once,
        by construction rather than by luck. A digest-based scramble was tried first and is worse:
        deterministic, but whether it happens to lead with the maximum is a property of the
        particular numbers, so it discriminated `min` and not `max`. **Fewer than three items cannot
        discriminate either way** -- with two, "first" IS one of min and max -- which is why the
        crashed-supersede case is also tested at six.

        Not `random.shuffle` and not `hash()`: an intermittent double fails intermittently, which
        reads as flakiness and gets retried away, and `hash()` is salted per process.
        """
        with self._lock:
            items = list(self.items.values())
        half = len(items) // 2
        return items[half:] + items[:half]

    def work_item(self, number: int) -> WorkItem | None:
        """BY NUMBER, and therefore fresh even in `StaleListForge` -- which is the measured shape."""
        with self._lock:
            return self.items.get(number)

    def create_work_item(self, *, title: str, body: str) -> int:
        with self._lock:
            number = len(self.items) + 1
            self.items[number] = WorkItem(number=number, title=title, state='open')
            self.bodies[number] = body
            self._comments[number] = []
            self.item_labels[number] = []
            return number

    def add_comment(self, number: int, body: str) -> int:
        with self._lock:
            # THE COUNTER IS THE POINT. A per-issue index, or a timestamp, would not be a
            # server-assigned monotonic key and the store's arbitration would be untested.
            comment_id = self._next_id
            self._next_id += 1
            self._comments[number].append(Comment(id=comment_id, body=body))
            return comment_id

    def comments(self, number: int) -> list[Comment]:
        with self._lock:
            return list(self._comments[number])

    def update_comment(self, number: int, comment_id: int, body: str) -> None:
        with self._lock:
            existing = self._comments[number]
            if not any(c.id == comment_id for c in existing):
                # THE DOUBLE MUST BE AS UNFORGIVING AS THE FORGE HERE. Gitea answers 404 for an edit
                # of a pruned comment, and a fake that silently no-op'd would let a runner believe
                # it had beaten -- the exact failure the distinction exists to prevent.
                msg = f'comment {comment_id} on item {number} no longer exists; re-create it'
                raise CommentGone(msg)
            self._comments[number] = [Comment(id=c.id, body=body) if c.id == comment_id else c for c in existing]

    def delete_comment(self, number: int, comment_id: int) -> None:
        with self._lock:
            self._comments[number] = [c for c in self._comments[number] if c.id != comment_id]

    def labels(self, number: int) -> list[str]:
        """NAMES, because that is what the `Forge` protocol promises. Ids stop at this boundary --
        and the fact that a name can appear TWICE in this list is the whole point of the change."""
        with self._lock:
            return [name for _id, name in self.item_labels[number]]

    def label_id(self, name: str) -> int:
        """The LOWEST id for `name`, created if absent -- `GiteaForge._label_id`'s rule.

        Mirrored deliberately rather than simplified: it is the rule that makes every runner
        converge on one id without coordinating, and a double that picked differently would make
        the store's convergence untestable.
        """
        with self._lock:
            ids = self.repo_labels.get(name)
            if not ids:
                self._next_label_id += 1
                self.repo_labels[name] = [self._next_label_id]
                return self._next_label_id
            return min(ids)

    def define_duplicate_label(self, name: str) -> int:
        """Create a SECOND repo label with an existing name and a higher id, and return it.

        NOT PART OF THE `Forge` PROTOCOL. It reproduces a state the vendor reaches and our code
        cannot: a losing racer's label, a human adding one in the web UI, an older client. Twelve
        of these were created on the real server in the CAS measurement, so this is reproduction,
        not invention.
        """
        with self._lock:
            self._next_label_id += 1
            self.repo_labels.setdefault(name, []).append(self._next_label_id)
            return self._next_label_id

    def attach_label_id(self, number: int, label_id: int, name: str) -> None:
        """Attach a SPECIFIC label id. The escape hatch for planting the duplicate above."""
        with self._lock:
            self.item_labels[number].append((label_id, name))

    def add_label(self, number: int, name: str) -> None:
        label_id = self.label_id(name)
        with self._lock:
            self.item_labels[number].append((label_id, name))

    def remove_label(self, number: int, name: str) -> None:
        """Detaches EVERY id sharing this name -- mirroring `GiteaForge.remove_label`.

        Until 2026-08-10 both this and the vendor wrapper removed only the ONE id `_label_id`
        converges on, so a same-named label attached under a higher id survived and left an item
        carrying two verdicts at once. The double could not express that at all while it keyed
        labels by name, which is why the bug lived behind a green suite.
        """
        with self._lock:
            ids = set(self.repo_labels.get(name, []))
            self.item_labels[number] = [(i, n) for i, n in self.item_labels[number] if i not in ids]

    def close_work_item(self, number: int) -> None:
        with self._lock:
            self.items[number] = WorkItem(number=number, title=self.items[number].title, state='closed')

    def reopen_work_item(self, number: int) -> None:
        """NOT part of the `Forge` protocol -- deliberately.

        Reopening is something a HUMAN does in the web UI, or a retry policy that lives above this
        layer; no code here needs to do it, and adding it to the protocol would oblige every
        backend to implement an operation nobody calls. This double grows it because a double's job
        is to reproduce states the real world can reach, not only the ones our code writes.
        """
        with self._lock:
            self.items[number] = WorkItem(number=number, title=self.items[number].title, state='open')

    def state(self, number: int) -> str:
        with self._lock:
            return self.items[number].state

    def retire_work_item(self, number: int) -> None:
        """Closes AND retitles, because a retired item must stop matching its title.

        The fake used only to close, which made it gentler than every real forge: retired items went
        on answering title lookups, so `reconcile_duplicates` would have retired the same ones
        forever and the test that counts survivors could never pass. Exactly the audit question from
        the double note -- is this double better-behaved than reality, and does the difference hide
        a failure class -- caught here by the sweep rather than in production.
        """
        self.retired.append(number)
        with self._lock:
            current = self.items[number]
            suffix = '' if current.title.endswith(' (retired)') else ' (retired)'
            self.items[number] = WorkItem(number=number, title=f'{current.title}{suffix}', state='closed')


@pytest.fixture
def recording_forge():
    return RecordingForge()


@pytest.fixture
def memory_store(recording_forge):
    return ForgeStore('ns', recording_forge, role=Role.SUBMITTER)


# --------------------------------------------------------------------------------------------
# The claim comment, pure.
# --------------------------------------------------------------------------------------------


class TestTheClaimCommentIsReadableBothWays:
    def test_it_round_trips(self):
        claim = decode_claim(encode_claim(owner='runner-a', expires_at=1000.0), comment_id=7)
        assert claim.owner == 'runner-a'
        assert claim.expires_at == pytest.approx(1000.0)
        assert claim.comment_id == 7

    def test_it_is_readable_by_a_HUMAN_scrolling_the_issue(self):
        """The forge is also the UI. An operator must be able to see who holds a job, and until
        when, without running a tool.
        """
        assert encode_claim(owner='runner-a', expires_at=1000.0).startswith('CLAIM ')
        assert 'runner-a' in encode_claim(owner='runner-a', expires_at=1000.0)

    def test_an_owner_containing_a_SPACE_survives_the_round_trip(self):
        """The expiry is encoded first so the owner can be the whole remainder. Truncating an owner
        at its first word would give two machines one identity -- and a release by either would
        then free the other's claim.
        """
        assert decode_claim(encode_claim(owner='box 7 runner a', expires_at=1.0)).owner == 'box 7 runner a'

    def test_a_NON_claim_comment_decodes_to_None(self):
        """Claims and verdicts share one comment stream, so 'this is not a claim' must be an
        ordinary answer rather than an error.
        """
        assert decode_claim('**PASS**\n\n```\n10646 passed\n```') is None

    def test_a_MALFORMED_claim_RAISES_instead_of_decoding_to_None(self):
        """THE DISTINCTION IS THE WHOLE POINT. If an unreadable claim returned `None` it would be
        skipped exactly like a verdict comment -- so a live claim in a format this version cannot
        read would be invisible, and a second runner would take a running job.
        """
        with pytest.raises(ValueError, match='claim'):
            decode_claim('CLAIM not-a-number runner-a')
        with pytest.raises(ValueError, match='claim'):
            decode_claim('CLAIM 12345')

    def test_a_fresh_claim_is_not_expired(self):
        assert decode_claim(encode_claim(owner='a', expires_at=time.time() + 300)).is_expired(now=time.time()) is False

    def test_a_claim_past_its_expiry_IS_expired(self):
        assert decode_claim(encode_claim(owner='a', expires_at=1000.0)).is_expired(now=1000.5) is True

    def test_the_boundary_instant_is_still_HELD(self):
        assert decode_claim(encode_claim(owner='a', expires_at=1000.0)).is_expired(now=1000.0) is False


class TestNoRefMechanismSURVIVED:
    """Refs are abandoned entirely (user directive 2026-08-09). A deleted mechanism that leaves a
    helper behind is a mechanism that comes back the first time someone needs one.
    """

    def test_the_store_module_runs_no_git_and_names_no_ref(self):
        source = Path(forge_store_module.__file__).read_text(encoding='utf-8')
        code = [
            token.string
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type not in (tokenize.STRING, tokenize.COMMENT)
        ]
        # EXACT identifiers, not substrings: `title_prefix` contains "ref" and would make this
        # assertion unpassable, which is how a check gets weakened until it means nothing.
        banned = {'ref', 'refs', 'claim_ref', 'git', 'push', '_push', '_git', 'subprocess', 'ls_remote', 'commit_tree'}
        offenders = sorted({t for t in code if t.lower() in banned})
        assert not offenders, f'ref machinery still in the store: {offenders}'

    def test_the_module_imports_no_SUBPROCESS(self):
        assert not hasattr(forge_store_module, 'subprocess')
        assert not hasattr(forge_store_module, 'claim_ref')


class TestNoVendorLEAKEDIntoTheLogic:
    """The user's constraint, as a check rather than an assurance.

    "把 Gitea/GitHub 降级为单纯的纯粹存储与 UI 界面" -- the forge is storage and a UI, and the
    scheduling logic is ours. A vendor conditional in the store would mean two behaviours where only
    one is ever tested, and cleanup is exactly where nobody would notice it was wrong.
    """

    def test_the_store_module_names_no_vendor_in_its_CODE(self):
        """Tokenised, not grepped. The module docstring cites the Gitea measurements on purpose --
        a plain grep would either flag that (and get suppressed) or be weakened to uselessness. So
        strings and comments are dropped and only executable tokens are examined.
        """
        source = Path(forge_store_module.__file__).read_text(encoding='utf-8')
        code = [
            token.string
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type not in (tokenize.STRING, tokenize.COMMENT)
        ]
        offenders = [t for t in code if 'gitea' in t.lower() or 'github' in t.lower()]
        assert not offenders, f'vendor names in the store logic: {offenders}'

    def test_the_store_imports_only_the_ABSTRACTION(self):
        assert not hasattr(forge_store_module, 'GiteaForge')
        assert not hasattr(forge_store_module, 'GitHubForge')

    def test_the_forge_is_REQUIRED_because_a_default_is_a_choice_of_vendor(self):
        with pytest.raises(TypeError):
            ForgeStore('ns')  # type: ignore[call-arg]

    def test_retirement_is_DELEGATED_never_spelled_out(self, memory_store, recording_forge):
        """Gitea cannot delete an issue here and closes-and-retitles instead; another forge may
        hard-delete. The store must ask, not decide, or cleanup grows a vendor conditional.
        """
        memory_store.record_verdict(JOB, verdict='PASS', detail='')
        memory_store.purge_namespace()
        assert recording_forge.retired == [1]

    def test_purging_cannot_reach_ANOTHER_namespace(self, recording_forge):
        ForgeStore('ns-a', recording_forge, role=Role.SUBMITTER).record_verdict(JOB, verdict='PASS', detail='')
        ForgeStore('ns-b', recording_forge, role=Role.SUBMITTER).purge_namespace()
        assert recording_forge.retired == []


class TestTheProtocolRefusesTheWayTheCONTRACTDemands:
    """Behaviour the contract suite does not reach, checked against the modelled forge."""

    def test_a_REFUSED_claimant_withdraws_its_comment(self, memory_store, recording_forge):
        """Not tidiness. See the next test for what an abandoned one does."""
        memory_store.try_claim(JOB, owner='runner-a')
        before = len(recording_forge.comments(1))
        assert memory_store.try_claim(JOB, owner='runner-b') is False
        assert len(recording_forge.comments(1)) == before, 'the loser left its claim comment behind'

    def test_a_refused_claim_does_not_ACTIVATE_when_the_holder_releases(self, memory_store):
        """THE FAILURE THAT MAKES WITHDRAWAL MANDATORY. A refused comment left in place becomes the
        lowest LIVE claim the moment the holder releases -- and the runner that was told False now
        reads itself as owning a job it never started. Nothing errors; it simply becomes true later.
        """
        memory_store.try_claim(JOB, owner='runner-a')
        assert memory_store.try_claim(JOB, owner='runner-b') is False
        memory_store.release(JOB, owner='runner-a')
        assert memory_store.claim_owner(JOB) is None

    def test_a_re_claim_by_the_HOLDER_leaves_the_original_claim_intact(self, memory_store):
        """The contract refuses it; this checks the refusal did not damage the live claim on its way
        out -- withdrawing the wrong comment would free a job that is being worked on.
        """
        memory_store.try_claim(JOB, owner='runner-a')
        assert memory_store.try_claim(JOB, owner='runner-a') is False
        assert memory_store.claim_owner(JOB) == 'runner-a'

    def test_an_EXPIRED_claim_is_taken_over_by_the_next_racer(self, recording_forge):
        """A machine that dies must not park a job forever. There is deliberately no separate
        takeover path: the dead claim stops counting and ordinary arbitration elects the next one.
        """
        dying = ForgeStore('ns', recording_forge, role=Role.SUBMITTER, lease_seconds=0.05)
        assert dying.try_claim(JOB, owner='runner-dead') is True
        time.sleep(0.15)
        assert dying.claim_owner(JOB) is None, 'an expired claim must read as unheld'
        assert ForgeStore('ns', recording_forge, role=Role.SUBMITTER).try_claim(JOB, owner='runner-b') is True

    def test_a_LIVE_claim_is_never_taken_over(self, memory_store):
        assert memory_store.try_claim(JOB, owner='runner-a') is True
        assert memory_store.try_claim(JOB, owner='runner-b') is False
        assert memory_store.claim_owner(JOB) == 'runner-a'

    def test_a_ZERO_lease_is_refused_at_CONSTRUCTION(self, recording_forge):
        """A zero lease expires the claim being made, so every runner refuses forever and the job
        silently never runs -- which reads as healthy contention rather than as a bug.
        """
        with pytest.raises(ValueError, match='lease_seconds'):
            ForgeStore('ns', recording_forge, role=Role.SUBMITTER, lease_seconds=0.0)

    def test_the_verdict_detail_is_not_a_CLAIM_comment(self, memory_store):
        """Claims and verdicts share one comment stream. A job claimed after its verdict would
        otherwise hand back `CLAIM ...` as gate.py's output -- plausible, wrong, unflagged.
        """
        memory_store.record_verdict(JOB, verdict='FAIL', detail='3 failed, 10643 passed')
        memory_store.try_claim(JOB, owner='runner-a')
        assert '3 failed, 10643 passed' in memory_store.verdict_detail(JOB)

    def test_the_sole_verdict_label_rule_lives_in_the_STORE(self, memory_store, recording_forge):
        """A retry after INCONCLUSIVE must leave ONE label on every backend, so the rule cannot be a
        vendor's. Checked against the backend that has no such rule of its own.
        """
        memory_store.record_verdict(JOB, verdict='INCONCLUSIVE', detail='node down')
        memory_store.record_verdict(JOB, verdict='PASS', detail='green on retry')
        assert [x for x in recording_forge.labels(1) if x.startswith('verdict:')] == ['verdict:pass']

    def test_concurrent_claimants_do_not_each_create_their_OWN_work_item(self, recording_forge):
        """The store-logic half of the creation race. It cannot prove the server's issue numbers are
        monotonic -- that is the network test's job -- but it does prove the store converges on the
        lowest one and retires its own duplicate instead of leaving it in the list.
        """
        job = Job(id='fresh-item', kind=TEST_RUN)
        stores = [ForgeStore('ns', recording_forge, role=Role.SUBMITTER) for _ in range(8)]
        winners: list[int] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def attempt(n: int) -> None:
            barrier.wait()
            if stores[n].try_claim(job, owner=f'r{n}'):
                with lock:
                    winners.append(n)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # CONVERGENCE is the vendor-neutral property, not "one item exists". Whether a retired
        # duplicate disappears from a title search is the FORGE's business -- this deployment
        # retitles, another may hard-delete -- and asserting it here would put a vendor behaviour
        # in a vendor-agnostic test. What must hold everywhere is that every racer resolved to the
        # same item, because that is what makes their comments contend at all.
        resolved = {store._item_number(job) for store in stores}
        assert len(resolved) == 1, f'racers used different work items: {resolved}'
        assert len(winners) == 1, f'{len(winners)} runners believed they won: {winners}'

    def test_two_jobs_do_not_share_a_work_item(self, memory_store):
        other = Job(id='j2', kind=TEST_RUN)
        assert memory_store.try_claim(JOB, owner='a') is True
        assert memory_store.try_claim(other, owner='b') is True


class TestTheVerdictVocabularyIsClosedBEFOREAnyIO:
    def test_every_verdict_has_exactly_one_label(self):
        assert set(VERDICT_LABELS) == VERDICTS
        assert len(set(VERDICT_LABELS.values())) == len(VERDICTS)

    def test_a_fourth_word_is_refused_without_touching_the_forge(self):
        """Validation precedes I/O, so an unreachable server is not what makes this fail. A store
        that validated after the comment was posted would leave one behind for a verdict it then
        rejected.
        """
        store = ForgeStore('probe-never-contacted', GiteaForge('http://127.0.0.1:1', 'o/r'), role=Role.SUBMITTER)
        with pytest.raises(ValueError, match='verdict'):
            store.record_verdict(JOB, verdict='ERROR', detail='')


class TestItIsAStore:
    def test_the_protocol_is_satisfied(self, memory_store):
        assert isinstance(memory_store, Store)

    def test_it_holds_a_forge_rather_than_BEING_one(self, memory_store):
        """Storage is a collaborator, not a base class. Inheriting the vendor client would put the
        decisions and the I/O in one object, and the vendor half is the half we cannot test twice.
        """
        assert not isinstance(memory_store, Forge)


# --------------------------------------------------------------------------------------------
# The contract, against the modelled forge. Proves the STORE; cannot prove a deployment.
# --------------------------------------------------------------------------------------------


class TestTheStoreLogicSatisfiesTheContract(_ClaimContract):
    @pytest.fixture
    def store(self, memory_store):
        return memory_store


class TestTheStoreLogicArbitratesUnderThreads(_AtomicityContract):
    @pytest.fixture
    def store(self, memory_store):
        return memory_store


class TestTheStoreLogicRecordsVerdicts(_VerdictContract):
    @pytest.fixture
    def store(self, memory_store):
        return memory_store


# --------------------------------------------------------------------------------------------
# Everything below talks to the real server, where the ordering properties are MEASURED FACTS
# rather than constructions.
# --------------------------------------------------------------------------------------------


@pytest.fixture
def server_store():
    """A store in a namespace nobody else owns, purged afterwards.

    THE NAMESPACE IS PER-TEST because the server is shared and the contract suite hard-codes one
    job id. Two runs of this file at once would otherwise contend for one work item, and each would
    report the other's claim as a contract violation.
    """
    store = ForgeStore(f'probe-p3-{uuid.uuid4().hex[:10]}', default_forge(), role=Role.SUBMITTER)
    try:
        yield store
    finally:
        store.purge_namespace()


@pytest.mark.live_forge
class TestTheRealServerSatisfiesTheContract(_ClaimContract):
    @pytest.fixture
    def store(self, server_store):
        return server_store


@pytest.mark.live_forge
class TestTheRealServerElectsOneWinner(_AtomicityContract):
    @pytest.fixture
    def store(self, server_store):
        return server_store


@pytest.mark.live_forge
class TestTheRealServerRecordsVerdicts(_VerdictContract):
    @pytest.fixture
    def store(self, server_store):
        return server_store


@pytest.mark.live_forge
class TestWhatOnlyTheRealServerCanSettle:
    def test_FOUR_rounds_each_elect_exactly_one_winner(self, server_store):
        """THE DISCRIMINATING TEST, and one round is not enough of it.

        The failure mode -- two runners both reading themselves lowest -- is rare by construction,
        so a single round electing a single winner is what a BROKEN protocol also does most of the
        time. Four independent rounds on four fresh work items, sixteen threads released together
        from a barrier, is the evidence the protocol was accepted on.
        """
        for round_number in range(4):
            job = Job(id=f'round{round_number}', kind=TEST_RUN)
            winners = _race_one_round(server_store.namespace, job, racers=16)
            assert len(winners) == 1, f'round {round_number}: {len(winners)} believed they won: {winners}'
            assert server_store.claim_owner(job) == winners[0]

    def test_runners_all_arbitrate_on_the_SUBMITTERS_item(self, server_store):
        """The creation race, in the shape that eliminates it rather than the shape that mitigated
        it. The submitter registers once; sixteen runners then discover and claim, and none of them
        can create even if the list cannot see the item -- the role refuses.

        This assertion is about the ITEM, not the winner: sixteen winners is ambiguous about the
        cause, sixteen items is not, and one item is the property that makes their claim comments
        contend at all.
        """
        job = Job(id='fresh-item', kind=TEST_RUN)
        submitted = ForgeStore(server_store.namespace, default_forge(), role=Role.SUBMITTER).register(job)

        stores = [ForgeStore(server_store.namespace, default_forge(), role=Role.RUNNER) for _ in range(16)]
        winners: list[int] = []
        lock = threading.Lock()
        barrier = threading.Barrier(16)

        def attempt(n: int) -> None:
            barrier.wait()
            if stores[n].try_claim(job, owner=f'r{n:02d}'):
                with lock:
                    winners.append(n)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        resolved = {store.work_item_number(job) for store in stores}
        assert resolved == {submitted}, f'runners left the submitter item: {resolved}'
        assert len(winners) == 1, f'{len(winners)} runners believed they won: {winners}'

    def test_comment_ids_come_back_MONOTONIC_under_concurrency(self, server_store):
        """The property the whole protocol rests on, asserted rather than assumed. If ids were not
        server-assigned and increasing, a later racer could carry a lower key -- which is precisely
        the `ci_tick` defect this replaces, reintroduced by the storage layer.
        """
        forge = default_forge()
        number = forge.create_work_item(title=f'[swarm] {server_store.namespace}/idprobe', body='probe')
        posted: list[int] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def post(n: int) -> None:
            barrier.wait()
            got = forge.add_comment(number, f'CLAIM 99999999999 r{n}')
            with lock:
                posted.append(got)

        threads = [threading.Thread(target=post, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        listed = [c.id for c in forge.comments(number)]
        assert len(set(posted)) == 8, f'ids were not unique: {sorted(posted)}'
        assert listed == sorted(listed), f'the list came back out of id order: {listed}'
        assert sorted(posted) == listed, 'a post was not visible in the list it belongs to'

    def test_a_LIVE_claim_is_not_stolen(self, server_store):
        assert server_store.try_claim(JOB, owner='runner-a') is True
        assert server_store.try_claim(JOB, owner='runner-b') is False
        assert server_store.claim_owner(JOB) == 'runner-a'

    def test_an_EXPIRED_claim_is_taken_over(self, server_store):
        dying = ForgeStore(server_store.namespace, default_forge(), role=Role.SUBMITTER, lease_seconds=0.5)
        assert dying.try_claim(JOB, owner='runner-dead') is True
        time.sleep(0.8)
        assert dying.claim_owner(JOB) is None
        assert (
            ForgeStore(server_store.namespace, default_forge(), role=Role.SUBMITTER).try_claim(JOB, owner='runner-b')
            is True
        )

    def test_the_verdict_survives_a_FRESH_store_object(self, server_store):
        """The point of a backing store. An in-process cache would pass every verdict test in the
        contract suite while storing nothing on the server.
        """
        server_store.record_verdict(JOB, verdict='INCONCLUSIVE', detail='node down')
        assert ForgeStore(server_store.namespace, default_forge(), role=Role.SUBMITTER).verdict(JOB) == 'INCONCLUSIVE'

    def test_the_detail_is_recorded_where_a_HUMAN_will_read_it(self, server_store):
        """gate.py's output is the evidence behind the verdict; a verdict without it is a claim."""
        server_store.record_verdict(JOB, verdict='FAIL', detail='3 failed, 10643 passed')
        assert '3 failed, 10643 passed' in server_store.verdict_detail(JOB)

    def test_recording_a_verdict_CLOSES_the_item(self, server_store):
        """The board column is driven by state; an answered job left open is one the lead re-reads
        every tick.
        """
        server_store.record_verdict(JOB, verdict='PASS', detail='green')
        assert server_store.item_state(JOB) == 'closed'

    def test_a_second_verdict_REPLACES_the_first(self, server_store):
        """A retry after INCONCLUSIVE must not leave the job carrying two verdict labels, since
        nothing downstream can act on a job that is both inconclusive and green.
        """
        server_store.record_verdict(JOB, verdict='INCONCLUSIVE', detail='node down')
        server_store.record_verdict(JOB, verdict='PASS', detail='green on retry')
        assert server_store.verdict(JOB) == 'PASS'


class StaleListForge:
    """A forge whose LIST lags, like GitHub's. THE HARNESS THIS BUG NEEDED.

    `RecordingForge` is read-after-write fresh by construction, so the whole offline suite passed
    for a design that produced 8 duplicate work items per round on a real GitHub repo. **Our test
    double was fresher than reality**, which is the most expensive kind to own: it does not merely
    fail to catch the bug, it certifies the bug as fixed.

    This one hides items younger than `staleness` from `list_work_items` and from nothing else --
    `create_work_item` still returns the number immediately, exactly as a 201 body does, and reads
    by number stay fresh. That is precisely the measured GitHub shape:

        plain list        22/22 stale, 0.42 s .. 6.36 s recovery
        GET /issues/{n}    0/22 stale

    It is a fake, and a fake proves nothing ABOUT GitHub. What it does is make a design that depends
    on a fresh list fail offline, deterministically, in the second it takes to run -- the difference
    between a bug we can hold still and one we can only observe.
    """

    def __init__(self, inner: RecordingForge, *, staleness: float) -> None:
        self.inner = inner
        self.staleness = staleness
        self.created_at: dict[int, float] = {}
        self.list_calls = 0

    def list_work_items(self) -> list[WorkItem]:
        self.list_calls += 1
        now = time.monotonic()
        return [
            item
            for item in self.inner.list_work_items()
            if now - self.created_at.get(item.number, 0.0) >= self.staleness
        ]

    def create_work_item(self, *, title: str, body: str) -> int:
        number = self.inner.create_work_item(title=title, body=body)
        self.created_at[number] = time.monotonic()
        return number

    def __getattr__(self, name):
        # Everything else -- comments, labels, state -- is a read BY NUMBER and is fresh on both
        # forges. Only the list lags.
        return getattr(self.inner, name)


class TestAListQueryCannotSayABSENT:
    """The bug this class exists for was invisible to the entire offline suite.

    `_item_number` concluded "no such work item" from a LIST query and then created one. On Gitea
    that is safe BY ACCIDENT -- its plain list is fresh. On GitHub the list was measured 22/22 stale
    with up to 6.36 s recovery, so "nothing found" meant "created 200 ms ago and not replicated
    yet", and the natural next line created a duplicate.
    """

    def test_the_answer_is_NOT_VISIBLE_never_None(self):
        """The re-typing that makes the bug unwritable. `if number is None: create()` cannot be
        written against a value that is never None -- and a comment saying so would have been prose
        the code never consults.
        """
        store = ForgeStore('ns', StaleListForge(RecordingForge(), staleness=60.0), role=Role.SUBMITTER)
        answer = store.work_item_number(JOB)
        assert answer is not None
        assert isinstance(answer, NotVisible)

    def test_a_hidden_item_reads_as_NOT_VISIBLE_rather_than_absent(self):
        forge = StaleListForge(RecordingForge(), staleness=60.0)
        ForgeStore('ns', forge, role=Role.SUBMITTER).register(JOB)
        assert isinstance(ForgeStore('ns', forge, role=Role.SUBMITTER).work_item_number(JOB), NotVisible)

    def test_NOT_VISIBLE_is_falsy_but_is_not_None(self):
        """A visible item number is never 0 -- forges number from 1 -- so the falsy case is exactly
        the unknown one, and `if not number:` is at least not silently wrong.
        """
        assert not NOT_VISIBLE
        assert NOT_VISIBLE is not None


class TestConcurrentCreationAgainstALAGGINGList:
    """The race, and the fact that it is now DELETED rather than narrowed.

    Convergence -- create, re-read, take the lowest -- used to live here. It is gone, because it was
    measured not to work: on GitHub the re-read did not return even the reader's own just-created
    issue, 24 of 24 times. A mitigation that reads from the same stale view that caused the problem
    cannot work, and one that works on Gitea alone is worse than none, because it makes the forge we
    test against unrepresentative of the forge we ship to.
    """

    @staticmethod
    def _race(store_factory, racers: int = 8) -> set[int]:
        job = Job(id='fresh', kind=TEST_RUN)
        stores = [store_factory() for _ in range(racers)]
        barrier = threading.Barrier(racers)

        def attempt(n: int) -> None:
            barrier.wait()
            stores[n].try_claim(job, owner=f'r{n}')

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(racers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return {store.work_item_number(job) for store in stores}

    def test_concurrent_SUBMITTERS_still_duplicate_and_that_is_the_named_residual(self):
        """THE HONEST RESIDUAL, held still rather than implied absent.

        Eight submitter-mode stores racing at one unsubmitted job produce eight work items, and
        nothing detects it. The contract is one submitter per job -- `register` is that call -- and
        it is enforced by ROLE for runners and by CONVENTION for submitters. This test exists so
        that the convention is visible as a convention, not mistaken for a guarantee.
        """
        forge = StaleListForge(RecordingForge(), staleness=30.0)
        resolved = self._race(lambda: ForgeStore('ns', forge, role=Role.SUBMITTER))
        assert len(resolved) == 8, f'expected every submitter on its own item, got {sorted(resolved)}'

    def test_REGISTER_deletes_the_race_without_any_window_at_all(self):
        """THE ACTUAL FIX. A single writer creates once; runners only discover and claim. No sleep,
        no window, no dependence on replication -- and it holds with the list lagging by thirty
        seconds, which no amount of convergence tuning could survive.
        """
        forge = StaleListForge(RecordingForge(), staleness=30.0)
        job = Job(id='fresh', kind=TEST_RUN)
        number = ForgeStore('ns', forge, role=Role.SUBMITTER).register(job)

        title = f'[swarm] ns/{job.claim_key()}'
        items = [i for i in forge.inner.list_work_items() if i.title == title]
        assert len(items) == 1
        assert items[0].number == number

    def test_register_does_not_RE_READ_to_find_what_it_just_created(self):
        """`POST /issues` returns the number in its 201 body -- authoritative, fresh, free. Creating
        and then listing to find your own creation is strictly worse on EVERY forge, and on GitHub
        it was measured to fail outright: the re-read missed the reader's own issue 24 of 24 times.
        """
        forge = StaleListForge(RecordingForge(), staleness=30.0)
        store = ForgeStore('ns', forge, role=Role.SUBMITTER)
        before = forge.list_calls
        store.register(Job(id='fresh', kind=TEST_RUN))
        assert forge.list_calls == before, 'register consulted the list it must not trust'


class TestEditingAComment:
    """`update_comment` exists for the heartbeat: one comment per runner, edited in place."""

    def test_an_edit_replaces_rather_than_appends(self, recording_forge):
        number = recording_forge.create_work_item(title='t', body='b')
        first = recording_forge.add_comment(number, 'BEAT 1')
        recording_forge.update_comment(number, first, 'BEAT 2')
        assert [c.body for c in recording_forge.comments(number)] == ['BEAT 2']

    def test_the_comment_ID_is_stable_across_an_edit(self, recording_forge):
        """A beat that changed its own id would be a new comment wearing the old one's name, and the
        fleet's view of "who is alive" is keyed on that id.
        """
        number = recording_forge.create_work_item(title='t', body='b')
        first = recording_forge.add_comment(number, 'BEAT 1')
        recording_forge.update_comment(number, first, 'BEAT 2')
        assert [c.id for c in recording_forge.comments(number)] == [first]

    def test_editing_a_PRUNED_comment_is_distinguishable_from_success(self, recording_forge):
        """THE ONE THE HEARTBEAT TURNS ON. A runner whose comment was pruned must learn it has to
        RE-CREATE. An edit that failed silently would leave it believing it had beaten while the
        fleet counted it dead -- and nothing anywhere would error.
        """
        number = recording_forge.create_work_item(title='t', body='b')
        first = recording_forge.add_comment(number, 'BEAT 1')
        recording_forge.delete_comment(number, first)
        with pytest.raises(CommentGone, match='re-create'):
            recording_forge.update_comment(number, first, 'BEAT 2')

    def test_CommentGone_is_a_forge_error_not_a_new_error_family(self):
        """A caller that already handles ForgeError keeps working; one that wants the distinction
        asks for it. A parallel hierarchy would make every existing handler subtly incomplete.
        """
        assert issubclass(CommentGone, ForgeError)


@pytest.mark.live_forge
class TestEditingAgainstTheRealForge:
    def test_an_edit_is_in_place_and_a_pruned_comment_404s(self, server_store):
        """MEASURED HERE, not assumed: 200 with the comment count unchanged, and 404
        `comment does not exist` once it is gone. Both halves, because a heartbeat that could not
        tell them apart would report liveness for a runner nobody can see.
        """
        forge = default_forge()
        number = forge.create_work_item(title=f'[swarm] {server_store.namespace}/beat', body='probe')
        beat = forge.add_comment(number, 'BEAT 1')
        forge.update_comment(number, beat, 'BEAT 2')
        bodies = [c.body for c in forge.comments(number)]
        assert bodies == ['BEAT 2'], f'the edit was not in place: {bodies}'

        forge.delete_comment(number, beat)
        with pytest.raises(CommentGone):
            forge.update_comment(number, beat, 'BEAT 3')


class TestTheDuplicateSubmitterReconciler:
    """The cross-process half of the creation race, caught after the fact rather than prevented.

    Deleting `_converge` made creation correct WITHIN a process and unguarded ACROSS them. Two
    submitter processes for one key produce two work items and nothing detects it. Convergence is
    not coming back -- it re-read the stale view whose removal was the point -- so the duplicate is
    reconciled afterwards, loudly, by a sweep that is never on the create path.
    """

    @staticmethod
    def _duplicate(forge: RecordingForge, count: int) -> tuple[ForgeStore, str]:
        """Two submitter PROCESSES, modelled as two stores with no shared lock -- which is exactly
        what the in-process lock cannot help with."""
        job = Job(id='raced', kind=TEST_RUN)
        title = f'[swarm] ns/{job.claim_key()}'
        for _ in range(count):
            ForgeStore('ns', forge, role=Role.SUBMITTER).register(job)
        return ForgeStore('ns', forge, role=Role.SUBMITTER), title

    def test_ONE_ITEM_survives_and_that_is_the_assertion(self, recording_forge):
        """COUNT THE ITEMS, NOT THE WINNERS. Sixteen winners is ambiguous about the cause -- it is
        equally consistent with broken arbitration and with duplicated items -- while one item is
        not. That distinction is what hole 1 cost us the first time.
        """
        store, title = self._duplicate(recording_forge, 8)
        with pytest.raises(DuplicateWorkItems):
            store.reconcile_duplicates()
        alive = [i for i in recording_forge.list_work_items() if i.title == title]
        assert len(alive) == 1, f'{len(alive)} items survived the reconciler'

    def test_the_LOWEST_number_is_the_survivor(self, recording_forge):
        """Server-assigned and monotonic on both forges, so every observer converges on the same
        survivor without coordinating -- and the earlier item is the one runners are likelier to
        have already found.
        """
        store, title = self._duplicate(recording_forge, 5)
        numbers = sorted(i.number for i in recording_forge.list_work_items() if i.title == title)
        with pytest.raises(DuplicateWorkItems):
            store.reconcile_duplicates()
        alive = [i for i in recording_forge.list_work_items() if i.title == title]
        assert [i.number for i in alive] == [numbers[0]]

    def test_a_LOOKUP_converges_on_the_lowest_BEFORE_the_reconciler_ever_runs(self, recording_forge):
        """THE TIE-BREAK ON THE READ PATH, which every other test here was covering for.

        Found by scoring a prediction rather than by review. `_lowest_numbered`'s `min` was changed
        to `max` and **all 412 offline tests stayed green** -- because single-writer registration
        means our own code never creates two items with one title, so every lookup sees exactly one
        match and `min` equals `max`. Elimination alibied the tie-break. The reconciler's own
        `min` IS tested, but only AFTER `reconcile_duplicates` has already retired the losers.

        The reconciler is a manual sweep, so there is always a window where duplicates are live and
        lookups are happening. In that window the tie-break is the ONLY thing making two observers
        with different visibility name the same item -- and disagreeing there means two runners
        claim on two issues and both win, which is hole 1 from the claim-protocol measurement,
        reached by a different road.

        The duplicates are planted directly on the double: our submitter cannot produce this state,
        so no test derived from our write path could ever have separated the two `min`s.
        """
        job = Job(id='dup', kind=TEST_RUN)
        title = f'[swarm] ns/{job.claim_key()}'
        for _ in range(4):
            recording_forge.create_work_item(title=title, body='planted')
        numbers = sorted(i.number for i in recording_forge.list_work_items() if i.title == title)
        assert len(numbers) == 4, 'the plant did not take -- the rest of this test would be vacuous'

        # Two independent observers, both cold: neither has the creation response in hand.
        first = ForgeStore('ns', recording_forge, role=Role.RUNNER).work_item_number(job)
        second = ForgeStore('ns', recording_forge, role=Role.SUBMITTER).work_item_number(job)

        assert first == second == numbers[0], (
            f'observers named {first} and {second} out of {numbers} -- a lookup that does not '
            'take the lowest lets two runners claim two issues and both win'
        )

    def test_it_is_LOUD_about_every_retirement(self, recording_forge):
        """Silent dedup hides a submitter racing itself forever, and an hourly tidy-up would make
        the source impossible to find while the fleet looked healthy.
        """
        store, _ = self._duplicate(recording_forge, 4)
        with pytest.raises(DuplicateWorkItems) as caught:
            store.reconcile_duplicates()
        assert len(caught.value.findings) == 3
        assert 'submitter raced itself' in str(caught.value)
        assert all(f.kept < f.retired for f in caught.value.findings)

    def test_it_RETIRES_rather_than_deleting_or_merging(self, recording_forge):
        """Detection and retirement only -- the same ruling as the tamper case. Merging would invent
        a history that never happened, and HOW an item is retired is the forge's business.
        """
        store, title = self._duplicate(recording_forge, 3)
        survivor = min(i.number for i in recording_forge.list_work_items() if i.title == title)
        recording_forge.add_comment(survivor, 'a comment the reconciler must not touch')
        before = recording_forge.comments(survivor)
        with pytest.raises(DuplicateWorkItems):
            store.reconcile_duplicates()
        assert recording_forge.comments(survivor) == before, 'the reconciler edited the survivor'
        assert len(recording_forge.retired) == 2

    def test_reconciling_TWICE_is_silent_the_second_time(self, recording_forge):
        """What the retirement contract buys. An implementation that merely CLOSED a duplicate would
        leave it matching its title, and this sweep would then alarm about the same items forever --
        which is how a real alarm gets ignored.
        """
        store, _ = self._duplicate(recording_forge, 4)
        with pytest.raises(DuplicateWorkItems):
            store.reconcile_duplicates()
        assert ForgeStore('ns', recording_forge, role=Role.SUBMITTER).reconcile_duplicates() == []

    def test_a_clean_namespace_is_SILENT(self, recording_forge):
        """An alarm that fires on the normal case is not an alarm."""
        ForgeStore('ns', recording_forge, role=Role.SUBMITTER).register(JOB)
        assert ForgeStore('ns', recording_forge, role=Role.SUBMITTER).reconcile_duplicates() == []

    def test_it_cannot_reach_ANOTHER_namespace(self, recording_forge):
        """Scoped by the same title prefix as `purge_namespace`, so another swarm's duplicates are
        not this sweep's to retire -- and its items are not this sweep's to count.
        """
        self._duplicate(recording_forge, 3)
        assert ForgeStore('other', recording_forge, role=Role.SUBMITTER).reconcile_duplicates() == []
        assert recording_forge.retired == []

    def test_a_RUNNER_may_not_reconcile(self, recording_forge):
        """Retiring is a write to the work-item lifecycle, which is the submitter's, and default-deny
        is the rule that stopped runners creating in the first place.
        """
        self._duplicate(recording_forge, 2)
        with pytest.raises(PermissionError):
            ForgeStore('ns', recording_forge, role=Role.RUNNER).reconcile_duplicates()

    def test_it_is_NOT_on_the_hot_path(self):
        """A background sweep may be arbitrarily late; the create path may not be slow. A create
        that waited for a list would be the deleted convergence mitigation wearing a new name, so
        this checks the SOURCE rather than trusting the docstring.
        """
        source = Path(forge_store_module.__file__).read_text(encoding='utf-8')
        tree = ast.parse(source)
        hot = {'try_claim', 'register', '_item_number', '_from_index', 'record_verdict'}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in hot:
                called = {
                    n.func.attr for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                }
                assert 'reconcile_duplicates' not in called, f'{node.name} calls the sweep inline'

    def test_the_cache_does_not_keep_pointing_at_a_RETIRED_item(self, recording_forge):
        """A store that had resolved a loser must not go on confirming it. Left in place, its index
        entry would resolve to a retired item whose title no longer matches -- reported as index
        corruption, which is loud but blames the wrong thing.
        """
        job = Job(id='raced', kind=TEST_RUN)
        loser_store = ForgeStore('ns', recording_forge, role=Role.SUBMITTER)
        ForgeStore('ns', recording_forge, role=Role.SUBMITTER).register(job)
        loser_store.register(job)

        with pytest.raises(DuplicateWorkItems):
            loser_store.reconcile_duplicates()
        assert loser_store.work_item_number(job) == 1


# --------------------------------------------------------------------------------------------
# Discovery: the question a runner has and nobody else could answer.
# --------------------------------------------------------------------------------------------


class TestClaimableFindsWorkARunnerCouldTake:
    """`claimable` is the only method here that does not take a Job the caller already holds.

    A runner's whole problem is that it has none yet. Before this, the only way to ask was
    `Forge.list_work_items()` from the consumer -- which would put the identity grammar and the
    answered-ness rule in a second place, free to drift from the ones that write them.
    """

    def _register(self, store, *ids, kind=JobKind.TEST_RUN):
        return [store.register(Job(id=i, kind=kind)) for i in ids]

    def test_it_returns_registered_open_work(self, memory_store):
        self._register(memory_store, 'a', 'b')

        jobs = memory_store.claimable(JobKind.TEST_RUN).jobs

        assert sorted(j.id for j in jobs) == ['a', 'b']
        assert all(j.kind is JobKind.TEST_RUN for j in jobs)

    def test_an_ANSWERED_item_is_not_work(self, memory_store):
        """The control. Without it, `claimable` could return everything and still look right."""
        self._register(memory_store, 'a', 'b')
        memory_store.record_verdict(Job(id='a', kind=JobKind.TEST_RUN), verdict='PASS', detail='done')

        assert [j.id for j in memory_store.claimable(JobKind.TEST_RUN).jobs] == ['b']

    def test_INCONCLUSIVE_is_ANSWERED_too(self, memory_store):
        """ "I cannot tell" is a conclusion about this attempt, so the item is not still waiting.

        Re-offering it here would make an unreachable box spin on the same job forever. Retrying an
        inconclusive run is a POLICY -- it belongs to whoever owns the retry budget, and it acts by
        clearing the label, not by the store quietly forgetting the label exists.
        """
        (number,) = self._register(memory_store, 'a')
        memory_store.record_verdict(Job(id='a', kind=JobKind.TEST_RUN), verdict='INCONCLUSIVE', detail='node down')

        assert memory_store.claimable(JobKind.TEST_RUN).jobs == ()
        assert 'verdict:inconclusive' in memory_store.forge.labels(number)

    def test_a_REOPENED_item_carrying_a_verdict_label_is_not_work(self, memory_store, recording_forge):
        """THE ONLY TEST THAT DISCRIMINATES THE LABEL CHECK. Verified by deleting the check.

        Every other test here passes with `claimable`'s label filter removed, because
        `record_verdict` CLOSES the item on every verdict word -- so the state check alone was
        answering them. I had written the opposite in the docstring (that INCONCLUSIVE stays open
        and the label check carried it); deleting the check and watching nothing redden is what
        corrected it.

        The case where state and label disagree is a REOPEN -- a human reopening an answered issue,
        a retry policy reopening before it clears the label. State says claimable, label says
        answered. Answered wins: the cheap outcome is one job nobody picks up, the expensive one is
        re-running a job whose conclusion is already published and racing its author.
        """
        (number,) = self._register(memory_store, 'a')
        memory_store.record_verdict(Job(id='a', kind=JobKind.TEST_RUN), verdict='PASS', detail='green')
        recording_forge.reopen_work_item(number)

        assert memory_store.item_state(Job(id='a', kind=JobKind.TEST_RUN)) == 'open'
        assert memory_store.claimable(JobKind.TEST_RUN).jobs == (), (
            'a reopened item still carrying verdict:pass was offered as work'
        )

    def test_a_CANCELLED_item_closed_without_a_verdict_is_not_work(self, memory_store, recording_forge):
        """THE ONLY TEST THAT DISCRIMINATES THE STATE CHECK, and the exact mirror of the one above.

        The two filters cover for each other on everything our own code writes: `record_verdict`
        always labels AND closes, so each mutant survived the other's tests. Only where they
        DISAGREE does either earn its place, and both disagreements are things a human does --
        reopen an answered item (label without open), close an unwanted one (closed without label).

        Cancelled is the cheaper-looking direction and is not free: withdrawn work handed to a
        runner burns a whole gate and publishes a verdict on a question nobody is asking any more.
        """
        (number,) = self._register(memory_store, 'a')
        recording_forge.close_work_item(number)

        assert memory_store.verdict(Job(id='a', kind=JobKind.TEST_RUN)) is None, 'not a verdict -- a withdrawal'
        assert memory_store.claimable(JobKind.TEST_RUN).jobs == ()

    def test_another_KIND_is_not_returned(self, memory_store):
        self._register(memory_store, 'a')
        self._register(memory_store, 'z', kind=JobKind.AGENT_TASK)

        assert [j.id for j in memory_store.claimable(JobKind.TEST_RUN).jobs] == ['a']
        assert [j.id for j in memory_store.claimable(JobKind.AGENT_TASK).jobs] == ['z']

    def test_another_NAMESPACE_is_not_returned(self, recording_forge):
        """Namespaces are what keep two fleets off each other's work."""
        mine = ForgeStore('mine', recording_forge, role=Role.SUBMITTER)
        theirs = ForgeStore('theirs', recording_forge, role=Role.SUBMITTER)
        mine.register(Job(id='a', kind=JobKind.TEST_RUN))
        theirs.register(Job(id='b', kind=JobKind.TEST_RUN))

        assert [j.id for j in mine.claimable(JobKind.TEST_RUN).jobs] == ['a']

    def test_a_SHARDED_job_round_trips_its_width(self, memory_store):
        """The width is part of the identity: a 2-way shard 1 and a 4-way shard 1 cover different
        tests, so losing it would hand a runner a slice of the wrong partition."""
        memory_store.register(Job(id='k', kind=JobKind.TEST_RUN, shard=2, n_shards=4))

        (job,) = memory_store.claimable(JobKind.TEST_RUN).jobs

        assert (job.id, job.shard, job.n_shards) == ('k', 2, 4)

    def test_an_UNREACHABLE_store_RAISES(self, memory_store, monkeypatch):
        """Matching `live_runners`, not the `fleet_capabilities` asymmetry.

        Returning an empty result on a network failure would make an offline runner report "no
        work" forever -- indistinguishable from a genuinely idle queue, and a regression the CI
        scheduler has already been through once.
        """

        def _boom():
            raise ForgeError('the control plane is unreachable')

        monkeypatch.setattr(memory_store.forge, 'list_work_items', _boom)

        with pytest.raises(ForgeError):
            memory_store.claimable(JobKind.TEST_RUN)


class TestAbsenceIsUNWRITABLE:
    """The structural half, and the reason this returns a wrapper rather than a list.

    An empty result means NO WORK VISIBLE, never no work exists -- every forge list read can be
    stale. Seeing less than exists costs one idle tick; concluding nothing exists re-runs a
    25-minute job or creates a duplicate item.

    A docstring saying so would not be enough, and this project has the receipts: the `tmp_path`
    scope trap was described in a comment in the very file where two people then fell into it. So
    the ban is enforced rather than documented.
    """

    def test_truthiness_RAISES(self, memory_store):
        claimable = memory_store.claimable(JobKind.TEST_RUN)

        with pytest.raises(TypeError, match='no truth value'):
            bool(claimable)

    def test_the_tempting_expression_raises(self, memory_store):
        """`if not claimable:` is the exact line this exists to make impossible."""
        claimable = memory_store.claimable(JobKind.TEST_RUN)

        with pytest.raises(TypeError):
            if not claimable:
                pass

    def test_the_error_says_what_to_write_instead(self, memory_store):
        """A refusal that does not name the alternative gets worked around, not obeyed."""
        try:
            bool(memory_store.claimable(JobKind.TEST_RUN))
        except TypeError as exc:
            assert '.jobs' in str(exc)
        else:
            pytest.fail('truthiness did not raise')

    def test_iteration_and_len_still_WORK(self, memory_store):
        """The ban is narrow on purpose. Only the ambiguous expression is refused; a caller that
        wants to count or loop is asking an unambiguous question."""
        memory_store.register(Job(id='a', kind=JobKind.TEST_RUN))
        claimable = memory_store.claimable(JobKind.TEST_RUN)

        assert len(claimable) == 1
        assert [j.id for j in claimable] == ['a']


class TestTheREADDecidesWhichCandidateIsCurrent:
    """Latest-wins was a property of the REF TRANSPORT, not of the design.

    `refs/candidates/<branch>` got it from force-push: the tip was the only candidate that existed,
    so no stale SHA could be picked up. Nothing in the Issue transport provides that, and the
    supersede that replaces it is TWO writes -- create the new item, close the old. A submitter that
    dies between them leaves two open items for one branch.

    Hence the rule under test: the READ decides, the close is cleanup. Every test here plants that
    crashed-submitter state directly, because once the close works our own writer can never produce
    it again -- the same reason `claimable`'s two filters needed a reopened item and a cancelled one.
    """

    def _submit(self, store, branch, sha):
        return store.register(Job(id=f'{branch}/{sha}', kind=JobKind.TEST_RUN))

    def test_the_only_candidate_is_the_current_one(self, memory_store):
        self._submit(memory_store, 'main', 'aaa')

        job = memory_store.newest_open(JobKind.TEST_RUN, group='main')

        assert job is not None and job.id == 'main/aaa'

    def test_a_CRASHED_SUPERSEDE_still_yields_the_NEW_sha(self, memory_store):
        """THE WHOLE POINT. Two open items for one branch, and the older must never be picked.

        This is the state a submitter leaves behind when it dies after creating the replacement and
        before closing the predecessor. Refs made it unrepresentable; Issues do not, so it is the
        read that has to be correct.
        """
        self._submit(memory_store, 'main', 'old')
        self._submit(memory_store, 'main', 'new')  # the close never happened

        job = memory_store.newest_open(JobKind.TEST_RUN, group='main')

        assert job is not None and job.id == 'main/new', (
            'the reader picked the stale SHA -- exactly what force-push made impossible'
        )

    def test_a_LONG_series_of_crashed_supersedes_still_yields_the_newest(self, memory_store, recording_forge):
        """SIX items, not two, and the count is load-bearing.

        With two, "take the newest" and "take whichever the forge listed first" agree half the time,
        and under a deterministic double they agree ALWAYS or never -- so the two-item test above
        passed with the rule mutated to `first seen`. It is kept because it names the scenario; this
        one is what discriminates.

        Six crashed supersedes in a row is not far-fetched: it is one submitter crashing repeatedly
        against the same branch, which is exactly what a broken submitter does.
        """
        shas = [f's{i}' for i in range(6)]
        for sha in shas:
            self._submit(memory_store, 'main', sha)

        listed = [i.title for i in recording_forge.list_work_items()]
        assert not listed[0].endswith(f'main/{shas[-1]}'), (
            'the forge happened to list the newest first, so this test cannot tell "newest" from '
            '"first" -- change the double order, do not weaken the assertion'
        )

        assert memory_store.newest_open(JobKind.TEST_RUN, group='main').id == f'main/{shas[-1]}'

    def test_every_observer_converges_without_coordinating(self, memory_store, recording_forge):
        """Two cold readers, one answer. The ordering key is server-assigned, so this holds without
        either reader knowing the other exists -- and it must, or two runners gate two SHAs and both
        report on `main`."""
        self._submit(memory_store, 'main', 'old')
        self._submit(memory_store, 'main', 'new')

        answers = {
            ForgeStore('ns', recording_forge, role=Role.RUNNER).newest_open(JobKind.TEST_RUN, group='main').id
            for _ in range(2)
        }

        assert answers == {'main/new'}

    def test_a_CLOSED_predecessor_is_not_a_candidate(self, memory_store, recording_forge):
        """The cleanup direction: when the close DOES land, the old item drops out. If closing had
        no effect the namespace would never shrink and retention would be unimplementable."""
        first = self._submit(memory_store, 'main', 'old')
        self._submit(memory_store, 'main', 'new')
        recording_forge.close_work_item(first)

        assert memory_store.newest_open(JobKind.TEST_RUN, group='main').id == 'main/new'

    def test_the_LAST_candidate_being_closed_means_NOTHING_VISIBLE(self, memory_store, recording_forge):
        number = self._submit(memory_store, 'main', 'only')
        recording_forge.close_work_item(number)

        assert memory_store.newest_open(JobKind.TEST_RUN, group='main') is None

    def test_ANOTHER_BRANCH_is_never_returned(self, memory_store):
        """Branches are independent series. Leaking across them would gate a feature branch's SHA
        and publish the verdict against `main`."""
        self._submit(memory_store, 'main', 'aaa')
        self._submit(memory_store, 'feature', 'zzz')

        assert memory_store.newest_open(JobKind.TEST_RUN, group='main').id == 'main/aaa'
        assert memory_store.newest_open(JobKind.TEST_RUN, group='feature').id == 'feature/zzz'

    def test_a_branch_is_not_a_PREFIX_of_another_branch(self, memory_store):
        """`main` must not match `main-experiment`. The separator is part of the test, not decoration
        -- a bare `startswith(group)` passes every other test in this class."""
        self._submit(memory_store, 'main', 'aaa')
        # NEWER than the branch under test, so a bare `startswith(group)` returns THIS one. Ordered
        # deliberately: with the newer item on `main`, the assertion holds either way and the test
        # is vacuous -- which is how it first shipped, and the mutant survived it.
        self._submit(memory_store, 'main-experiment', 'zzz')

        assert memory_store.newest_open(JobKind.TEST_RUN, group='main').id == 'main/aaa'

    def test_an_UNREACHABLE_store_RAISES(self, memory_store, monkeypatch):
        """Same asymmetry as `claimable` and `live_runners`. `None` here would read as "no candidate
        on this branch" and the scheduler would idle through an outage reporting healthy."""

        def _boom():
            raise ForgeError('the control plane is unreachable')

        monkeypatch.setattr(memory_store.forge, 'list_work_items', _boom)

        with pytest.raises(ForgeError):
            memory_store.newest_open(JobKind.TEST_RUN, group='main')


class TestTheSubmitterDECLARESWhatToRun:
    """The request is DATA on the item, not a rule the scheduler re-derives.

    Under the ref transport there was nowhere to put it, so `ci.py candidate` printed a payload that
    was published nowhere and `ci_tick` computed its own from the branch name. One rule, two
    spellings -- and the duplicated derivation is the defect, not the copy that drifted. It had
    already produced a visible one: `--heavy` on any branch but `main` reported work nobody would do.
    """

    def _job(self, sha='aaa'):
        return Job(id=f'main/{sha}', kind=JobKind.TEST_RUN)

    def test_what_was_declared_is_what_is_read_back(self, memory_store):
        job = self._job()
        memory_store.register(job, requests=['fast', 'heavy'])

        assert memory_store.requested_runs(job) == frozenset({'fast', 'heavy'})

    def test_a_request_is_NOT_recomputed_from_the_id(self, memory_store):
        """THE DISCRIMINATING TEST. A branch called `main` asking for `fast` ONLY.

        Under the deleted rule `main` implied `['fast', 'heavy']`, so any implementation that still
        consults the id returns `heavy` here. Chosen deliberately: on any other branch name the two
        rules agree and the test would be vacuous.
        """
        job = self._job()
        memory_store.register(job, requests=['fast'])

        assert memory_store.requested_runs(job) == frozenset({'fast'}), (
            'the scheduler-side rule is back: `main` was expanded to fast+heavy by something other than the submitter'
        )

    def test_a_NON_main_branch_can_ask_for_heavy(self, memory_store):
        """The hole the ref transport could not close, now closed. `--heavy` off `main` was a flag
        that reported work nobody would do, because the request had no transport."""
        job = Job(id='feature/zzz', kind=JobKind.TEST_RUN)
        memory_store.register(job, requests=['fast', 'heavy'])

        assert 'heavy' in memory_store.requested_runs(job)

    def test_declaring_NOTHING_reads_back_as_nothing(self, memory_store):
        """Not as a default. Choosing what to do about an empty declaration is the scheduler's, and
        it is the only layer that knows whether a default is safe."""
        job = self._job()
        memory_store.register(job)

        assert memory_store.requested_runs(job) == frozenset()

    def test_an_EMPTY_request_is_REFUSED_at_the_write(self, memory_store):
        """A bare prefix would read back as an unnamed run -- something nothing can schedule and
        nobody can see on the board. Refused where it is written, not filtered where it is read."""
        with pytest.raises(ValueError, match='must be named'):
            memory_store.register(self._job(), requests=['fast', ''])

    def test_a_VERDICT_label_is_not_mistaken_for_a_request(self, memory_store):
        """Both vocabularies live in the same label namespace, so the prefixes must not collide --
        and a verdict landing must not retroactively change what was asked for."""
        job = self._job()
        memory_store.register(job, requests=['fast'])
        memory_store.record_verdict(job, verdict='PASS', detail='green')

        assert memory_store.requested_runs(job) == frozenset({'fast'})

    def test_requests_do_not_LEAK_between_items(self, memory_store):
        memory_store.register(self._job('aaa'), requests=['fast'])
        memory_store.register(self._job('bbb'), requests=['heavy'])

        assert memory_store.requested_runs(self._job('aaa')) == frozenset({'fast'})
        assert memory_store.requested_runs(self._job('bbb')) == frozenset({'heavy'})


class TestADuplicateLabelCannotProduceTwoVerdictsAtOnce:
    """A work item carrying both `verdict:inconclusive` and `verdict:pass`, which nothing can act on.

    `record_verdict` already tries to prevent this -- it removes every existing verdict label before
    adding the new one, and its comment says why: "a retry after INCONCLUSIVE that merely ADDED
    `pass` would leave the job both inconclusive and green". The removal is by NAME, and
    `GiteaForge.remove_label` resolves a name to ONE id, the lowest. **A same-named label attached
    under a higher id survives the loop**, so the guard's stated scope is wider than its real one --
    the scope-lie variant, which makes a reader route around a working check rather than distrust it.

    `_label_id`'s own docstring is where the claim lives: "everything above this line addresses
    labels by name anyway". False for removal.

    THE DISCRIMINATING STATE IS ONE OUR CODE CANNOT CREATE. Our writer always resolves through
    `_label_id` and therefore always attaches the lowest id. A higher-id attachment comes from a
    human in the web UI, an older client, or the racer that lost -- and duplicate label DEFINITIONS
    are measured, not hypothetical: twelve identical names from twelve racers on this Gitea.
    """

    def _job(self):
        return Job(id='dup-label', kind=JobKind.TEST_RUN)

    def test_the_DOUBLE_reports_a_duplicated_name_TWICE(self, recording_forge):
        """PINS THE DOUBLE'S OWN MODEL, and it needs pinning: collapsing `labels()` back to a set
        made every test in this class pass again while the modelled reality was gone.

        A double that cannot represent the failure is not a neutral simplification -- it is an
        assertion that the failure is impossible, and this one was refuted by measurement before it
        was written: twelve identical label names from twelve racers.
        """
        number = recording_forge.create_work_item(title='t', body='b')
        recording_forge.add_label(number, 'verdict:pass')
        stray = recording_forge.define_duplicate_label('verdict:pass')
        recording_forge.attach_label_id(number, stray, 'verdict:pass')

        assert recording_forge.labels(number) == ['verdict:pass', 'verdict:pass']

    def test_a_duplicate_DEFINITION_gets_a_higher_id(self, recording_forge):
        """The ordering is what makes the vendor's `min()` convergence meaningful; a double that
        handed out a lower id would make the store agree with itself for the wrong reason."""
        first = recording_forge.label_id('run:fast')
        second = recording_forge.define_duplicate_label('run:fast')

        assert second > first
        assert recording_forge.label_id('run:fast') == first, 'attachment must still converge on the lowest'

    def test_a_HIGHER_ID_verdict_label_does_not_survive_the_next_verdict(self, memory_store, recording_forge):
        job = self._job()
        number = memory_store.register(job)
        memory_store.record_verdict(job, verdict='INCONCLUSIVE', detail='node down')
        # The state our writer cannot reach: a second `verdict:inconclusive`, different id.
        stray = recording_forge.define_duplicate_label('verdict:inconclusive')
        recording_forge.attach_label_id(number, stray, 'verdict:inconclusive')

        memory_store.record_verdict(job, verdict='PASS', detail='green on the retry')

        assert recording_forge.labels(number).count('verdict:inconclusive') == 0, (
            'the item carries both INCONCLUSIVE and PASS -- exactly the state record_verdict says '
            'it prevents, and nothing downstream can act on it'
        )

    def test_the_reported_verdict_is_not_DECIDED_BY_LIST_ORDER(self, memory_store, recording_forge):
        """The consequence, stated where it hurts. `verdict()` returns the FIRST label it maps, so
        with two present the answer depends on the order the forge happens to return -- and a green
        read off an item that also says INCONCLUSIVE is the worst failure this system can have.
        """
        job = self._job()
        number = memory_store.register(job)
        memory_store.record_verdict(job, verdict='INCONCLUSIVE', detail='node down')
        stray = recording_forge.define_duplicate_label('verdict:inconclusive')
        recording_forge.attach_label_id(number, stray, 'verdict:inconclusive')

        memory_store.record_verdict(job, verdict='PASS', detail='green on the retry')

        assert memory_store.verdict(job) == 'PASS'
