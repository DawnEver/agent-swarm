"""Admission is a door; this is the ROOM. A box that acquires other work mid-run must narrow.

THE GAP, STATED AS THE DEFECT IT IS. `loop.Box.blockers` is asked ONCE, before the claim, and
`Outcome.REFUSED` is the only answer the layer can give about capacity. So the fleet's entire
response to "this workstation just got busy" is to decline the NEXT item, while the job already
running keeps the whole machine. The terminal model covers the ends -- opening it enrols the box,
closing it withdraws it -- and nothing covered the middle.

WHY THE OBVIOUS TEST IS VACUOUS. "The worker count comes from a capacity reading" passes against a
width computed once at startup, which is exactly the code that already exists. The discriminating
question is whether a reading taken LATER can still change the width of a run in flight, and whether
the CALLER can tell that it did. Both are asserted here against a capacity source that CHANGES
between readings; a fixed source cannot distinguish the two implementations at all.

AND THE SHRINK MUST NOT BE A SILENT SUCCESS. An executor that is told to narrow and does not is the
forbidden shape wearing the regulator as a disguise: every log line says "shrinking", the box runs
at full width, and the return value is a clean PASS. So the honour handshake is mandatory and its
absence RAISES -- the tests below plant an executor that ignores a grant and an executor that never
answers one, and both must reach the caller as a crash rather than as a verdict.
"""

from __future__ import annotations

import pytest

from agent_swarm.job import TEST_RUN, Job
from agent_swarm.loop import Box, Outcome, RegulatedRun, run_one, run_regulated
from agent_swarm.scaling import (
    CapacityUnreadable,
    Regulation,
    Regulator,
    WidthNotHonoured,
    workers_for,
)
from agent_swarm.store import InMemoryStore

pytestmark = pytest.mark.unit

CHEAP_JOB = Job(id='j1', kind=TEST_RUN, exclusivity='cheap', ram_gib=0.1)

#: An idle box with room, passed explicitly at every call site for the reason `Box` gives: a
#: defaulted capacity would mean "unlimited" wherever somebody forgot to measure.
IDLE = Box(available_gib=32.0)

#: The regulator's pricing, kept in one place so a test that changes a READING is not also silently
#: changing the arithmetic. 2.0 GiB per worker, 2.0 GiB reserve, 8 workers asked for.
PRICING = {'per_worker_gib': 2.0, 'reserve_gib': 2.0, 'max_workers': 8}


class Readings:
    """A capacity source that CHANGES. The whole experiment depends on this, not on a constant.

    The last value repeats, so a test declares only the transitions it cares about and an executor
    may poll as often as it likes without the source running out.
    """

    def __init__(self, *values: float | None) -> None:
        self.values = list(values)
        self.taken = 0

    def __call__(self) -> float | None:
        value = self.values[min(self.taken, len(self.values) - 1)]
        self.taken += 1
        return value


class Cooperative:
    """An executor that asks before each unit of work and does what it is told.

    COOPERATIVE AND NOT SUPERVISED, deliberately: a thread watching a running job would be the
    daemon this design refuses, and it could only shrink a worker pool it owns. The executor is the
    only party that knows where its safe points are, so it polls at them.
    """

    def __init__(self, units: int = 3, verdict: str = 'PASS') -> None:
        self.units = units
        self.verdict = verdict
        self.widths: list[int] = []

    def execute(self, job: Job, regulator: Regulator) -> tuple[str, str]:
        for _ in range(self.units):
            grant = regulator.reading()
            self.widths.append(grant.workers)
            if grant.workers == 0:
                regulator.honour(0)
                return 'INCONCLUSIVE', 'yielded the box; the capacity that admitted this run is gone'
            regulator.honour(grant.workers)
        return self.verdict, 'done'


class ReadsOnceAtStartup:
    """THE CODE THAT ALREADY EXISTS, as a test double. It must NOT be able to pass the shrink test."""

    def __init__(self, units: int = 3) -> None:
        self.units = units
        self.widths: list[int] = []

    def execute(self, job: Job, regulator: Regulator) -> tuple[str, str]:
        grant = regulator.reading()
        regulator.honour(grant.workers)
        self.widths.extend([grant.workers] * self.units)
        return 'PASS', 'done'


class IgnoresTheGrant:
    """Told to narrow, runs wide anyway -- and reports the width it really used."""

    def execute(self, job: Job, regulator: Regulator) -> tuple[str, str]:
        regulator.reading()
        regulator.honour(8)
        regulator.reading()
        regulator.honour(8)
        return 'PASS', 'done'


class NeverAnswers:
    """Reads the grant and never says what it adopted. The silent shape, exactly."""

    def execute(self, job: Job, regulator: Regulator) -> tuple[str, str]:
        regulator.reading()
        regulator.reading()
        return 'PASS', 'done'


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


def _regulator(*values: float | None) -> Regulator:
    return Regulator(Readings(*values), **PRICING)


# ------------------------------------------------------------------ the width arithmetic


class TestWorkersFor:
    def test_the_reserve_is_not_available_to_workers(self):
        """32 GiB free, 2 reserved, 2 per worker -> 15, before the caller's own ceiling applies."""
        assert workers_for(32.0, per_worker_gib=2.0, reserve_gib=2.0, max_workers=99) == 15

    def test_the_callers_ceiling_is_never_exceeded(self):
        """The ceiling is what the caller measured its own work against; capacity may only LOWER it."""
        assert workers_for(32.0, **PRICING) == 8

    def test_a_box_with_only_the_reserve_left_supports_NOBODY(self):
        assert workers_for(2.5, **PRICING) == 0

    def test_it_never_returns_a_NEGATIVE_width(self):
        """A box already past its reserve is zero workers, not minus three."""
        assert workers_for(0.0, **PRICING) == 0

    def test_an_UNREADABLE_capacity_is_not_a_number(self):
        """`capacity_blocker` refuses on `None` at the door for the same reason: unknown must never
        become unlimited by arithmetic."""
        with pytest.raises(CapacityUnreadable):
            workers_for(None, **PRICING)

    def test_an_UNPRICED_worker_is_refused_rather_than_assumed(self):
        """Zero GiB per worker divides to infinity, which is the safe-looking answer that is wrong."""
        with pytest.raises(ValueError, match='per_worker_gib'):
            workers_for(32.0, per_worker_gib=0.0, reserve_gib=2.0, max_workers=8)


# ------------------------------------------------------------------ the discriminating cases


class TestCapacityDroppingMidRun:
    def test_the_RUNNING_jobs_width_drops(self, store):
        """THE ACCEPTANCE CRITERION. 32 GiB at the door, 8 GiB two units in: 8 workers -> 3."""
        ex = Cooperative(units=3)
        run_regulated(
            CHEAP_JOB,
            executor=ex,
            store=store,
            owner='box-1',
            box=IDLE,
            regulator=_regulator(32.0, 32.0, 8.0),
        )
        assert ex.widths == [8, 8, 3], f'the width never followed the capacity: {ex.widths}'

    def test_the_CALLER_can_tell_it_shrank(self, store):
        """A log is not a signal. The reduction is in the RETURN or it did not happen."""
        run = run_regulated(
            CHEAP_JOB,
            executor=Cooperative(units=3),
            store=store,
            owner='box-1',
            box=IDLE,
            regulator=_regulator(32.0, 32.0, 8.0),
        )
        assert isinstance(run, RegulatedRun)
        assert run.outcome is Outcome.ANSWERED
        assert Regulation.SHRANK in [a.regulation for a in run.adjustments]
        assert run.final_workers == 3

    def test_a_STARTUP_ONLY_read_cannot_pass_the_test_above(self, store):
        """THE CONTROL, and the reason the two tests before it are not vacuous. Reading capacity
        once and holding the width is the behaviour that already exists; against the SAME dropping
        source it must produce no shrink at all. Without this, a test asserting "capacity is read"
        would be green on the unfixed code."""
        ex = ReadsOnceAtStartup(units=3)
        run = run_regulated(
            CHEAP_JOB,
            executor=ex,
            store=store,
            owner='box-1',
            box=IDLE,
            regulator=_regulator(32.0, 32.0, 8.0),
        )
        assert ex.widths == [8, 8, 8]
        assert Regulation.SHRANK not in [a.regulation for a in run.adjustments]


class TestCapacityGone:
    def test_the_job_YIELDS_rather_than_running_on_at_full_width(self, store):
        ex = Cooperative(units=5)
        run = run_regulated(
            CHEAP_JOB,
            executor=ex,
            store=store,
            owner='box-1',
            box=IDLE,
            regulator=_regulator(32.0, 2.5),
        )
        assert ex.widths == [8, 0], 'the executor kept working after the box was taken away'
        assert run.yielded
        assert run.final_workers == 0

    def test_a_YIELD_is_an_INCONCLUSIVE_and_not_a_FAIL(self, store):
        """A box that gave the machine back has said nothing about the code. Recording FAIL would
        convert somebody else's workload into evidence against a diff."""
        run_regulated(
            CHEAP_JOB,
            executor=Cooperative(units=5),
            store=store,
            owner='box-1',
            box=IDLE,
            regulator=_regulator(32.0, 2.5),
        )
        assert store.verdict(CHEAP_JOB) == 'INCONCLUSIVE'

    def test_the_claim_is_released_after_a_yield(self, store):
        """A yielded job nothing will retry is worse than one that never started."""
        run_regulated(
            CHEAP_JOB,
            executor=Cooperative(units=5),
            store=store,
            owner='box-1',
            box=IDLE,
            regulator=_regulator(32.0, 2.5),
        )
        assert store.claim_owner(CHEAP_JOB) is None


class TestCapacityRecovering:
    def test_the_width_GROWS_BACK(self, store):
        """A DECISION, TESTED RATHER THAN ASSUMED: regulation, not a ratchet. A width that could
        only fall would leave a box permanently narrow after one transient dip -- the neighbour's
        build finishes and this machine never uses the memory again until the run ends."""
        ex = Cooperative(units=3)
        run = run_regulated(
            CHEAP_JOB,
            executor=ex,
            store=store,
            owner='box-1',
            box=IDLE,
            regulator=_regulator(32.0, 6.0, 32.0),
        )
        assert ex.widths == [8, 2, 8]
        assert Regulation.GREW in [a.regulation for a in run.adjustments]

    def test_growth_stops_at_the_width_the_caller_asked_for(self, store):
        """Capacity may only lower the ceiling. A box with 512 GiB free does not get to invent 200
        workers for work the caller priced at 8."""
        ex = Cooperative(units=2)
        run_regulated(
            CHEAP_JOB,
            executor=ex,
            store=store,
            owner='box-1',
            box=IDLE,
            regulator=_regulator(6.0, 512.0),
        )
        assert ex.widths == [2, 8]


class TestAReductionThatCannotBeHonoured:
    def test_an_executor_that_IGNORES_a_grant_crashes_the_run(self, store):
        """THE FORBIDDEN SHAPE, planted. Running wider than granted must not be reachable as a
        clean PASS -- otherwise the regulator is decoration and every log line lies."""
        run = run_regulated(
            CHEAP_JOB,
            executor=IgnoresTheGrant(),
            store=store,
            owner='box-1',
            box=IDLE,
            regulator=_regulator(32.0, 6.0),
        )
        assert run.outcome is Outcome.CRASHED
        assert store.verdict(CHEAP_JOB) == 'INCONCLUSIVE'
        assert WidthNotHonoured.__name__ in store._verdicts[CHEAP_JOB.claim_key()][1]

    def test_an_executor_that_never_answers_a_grant_crashes_the_run(self, store):
        """Silence is the same defect as disobedience: from outside, an unanswered grant and a
        honoured one are indistinguishable, which is precisely what makes it dangerous."""
        run = run_regulated(
            CHEAP_JOB,
            executor=NeverAnswers(),
            store=store,
            owner='box-1',
            box=IDLE,
            regulator=_regulator(32.0, 6.0),
        )
        assert run.outcome is Outcome.CRASHED

    def test_an_unanswered_FINAL_grant_is_caught_after_the_executor_returns(self, store):
        """The hole a next-reading-only check would leave: honour every grant but the last, and the
        run ends before anything notices."""

        class HonoursAllButTheLast:
            def execute(self, job: Job, regulator: Regulator) -> tuple[str, str]:
                regulator.honour(regulator.reading().workers)
                regulator.reading()
                return 'PASS', 'done'

        run = run_regulated(
            CHEAP_JOB,
            executor=HonoursAllButTheLast(),
            store=store,
            owner='box-1',
            box=IDLE,
            regulator=_regulator(32.0),
        )
        assert run.outcome is Outcome.CRASHED
        assert store.verdict(CHEAP_JOB) == 'INCONCLUSIVE'

    def test_an_UNREADABLE_capacity_mid_run_is_a_crash_not_a_guess(self, store):
        """Neither scarcity nor plenty. The run says nothing rather than picking whichever reading
        lets it continue."""
        run = run_regulated(
            CHEAP_JOB,
            executor=Cooperative(units=3),
            store=store,
            owner='box-1',
            box=IDLE,
            regulator=_regulator(32.0, None),
        )
        assert run.outcome is Outcome.CRASHED
        assert CapacityUnreadable.__name__ in store._verdicts[CHEAP_JOB.claim_key()][1]

    def test_an_executor_may_run_NARROWER_than_granted(self, store):
        """The permitted direction, stated so the handshake is not read as "exactly N or crash".
        Under-using the box is always safe; the grant is a ceiling."""

        class Modest:
            def execute(self, job: Job, regulator: Regulator) -> tuple[str, str]:
                assert regulator.reading().workers == 8
                regulator.honour(1)
                return 'PASS', 'done'

        run = run_regulated(
            CHEAP_JOB, executor=Modest(), store=store, owner='box-1', box=IDLE, regulator=_regulator(32.0)
        )
        assert run.outcome is Outcome.ANSWERED
        assert run.final_workers == 1


# ------------------------------------------------------------------ the door is unchanged


class TestTheDoorIsUNCHANGED:
    def test_a_job_too_big_for_the_box_is_still_REFUSED_before_the_claim(self, store):
        """Regulation is added to the middle; it does not reopen the entry decision. A job admitted
        by a regulator that would have been refused by admission is a new bug, not a feature."""
        huge = Job(id='huge', kind=TEST_RUN, exclusivity='cheap', ram_gib=10_000.0)
        ex = Cooperative()
        run = run_regulated(huge, executor=ex, store=store, owner='box-1', box=IDLE, regulator=_regulator(32.0))
        assert run.outcome is Outcome.REFUSED
        assert ex.widths == []
        assert store.claim_owner(huge) is None
        assert run.adjustments == ()

    def test_a_refused_job_is_still_refused_through_run_one(self, store):
        """The unregulated entry point keeps its exact behaviour -- both paths share one ordering."""
        huge = Job(id='huge', kind=TEST_RUN, exclusivity='cheap', ram_gib=10_000.0)

        class Unregulated:
            def execute(self, job: Job) -> tuple[str, str]:
                return 'PASS', ''

        assert run_one(huge, executor=Unregulated(), store=store, owner='box-1', box=IDLE) is Outcome.REFUSED

    def test_an_ALREADY_ANSWERED_job_is_not_regulated_either(self, store):
        """Reading capacity for work nobody is going to do is a decision taken about nothing."""
        store.record_verdict(CHEAP_JOB, verdict='PASS', detail='')
        ex = Cooperative()
        run = run_regulated(CHEAP_JOB, executor=ex, store=store, owner='box-1', box=IDLE, regulator=_regulator(32.0))
        assert run.outcome is Outcome.ALREADY_ANSWERED
        assert ex.widths == []


class TestTheRegulatorOnItsOwn:
    def test_the_first_reading_is_measured_against_the_ASKED_width(self, store):
        """A run that starts narrow because the box was already busy is itself a shrink, and the
        caller must see it: the alternative is a first reading that always reports UNCHANGED and a
        constrained start that looks exactly like an unconstrained one."""
        reg = _regulator(6.0)
        grant = reg.reading()
        assert grant.previous == 8
        assert grant.workers == 2
        assert grant.regulation is Regulation.SHRANK

    def test_every_adjustment_carries_the_READING_it_was_derived_from(self, store):
        """A width with no number behind it cannot be checked by the person reading the report."""
        reg = _regulator(6.0)
        assert reg.reading().available_gib == 6.0

    def test_every_adjustment_states_a_REASON(self, store):
        reg = _regulator(6.0)
        assert reg.reading().reason

    def test_honouring_a_grant_that_was_never_ISSUED_is_refused(self, store):
        """The handshake has two ends. An executor reporting a width nobody granted has not been
        regulated -- it has been guessing, and a guess accepted here would read as compliance."""
        with pytest.raises(WidthNotHonoured):
            _regulator(32.0).honour(4)

    def test_a_ZERO_width_run_is_refused_at_CONSTRUCTION(self, store):
        """A caller asking for no workers is a configuration error, not a permanent yield."""
        with pytest.raises(ValueError, match='max_workers'):
            Regulator(Readings(32.0), per_worker_gib=2.0, reserve_gib=2.0, max_workers=0)
