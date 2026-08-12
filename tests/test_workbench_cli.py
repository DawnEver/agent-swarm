"""The CLI, raced and exit-coded through `main()` -- the entry point a human actually types.

WHY THE RACE IS REPEATED HERE AND NOT ONLY IN `test_pull_surface.py`. That file races `Workbench`,
which proves the SURFACE is a compare-and-swap. It says nothing about whether the CLI reaches that
surface: a `main()` that claimed by some other route, or that reported a verdict without checking
the claim, would pass every test there. The property everyone actually depends on is "a human typing
`take` and a runner ticking cannot both hold one item", and the only test that states it is one that
goes through `main()` on one side and `ForgeStore.try_claim` on the other.

EVERY ASSERTION IS ON THE RETURN VALUE, not on the printing. A CLI whose failures live only in prose
is a CLI nothing can script, and a test that asserted on stdout would pass for one that always
exited 0 while printing the word "error".

THE WORKBENCH IS INJECTED, so this whole file runs offline against `RecordingForge` while exercising
the REAL parser, the REAL dispatch and the REAL exit codes. Without that seam these tests would have
to be `live_forge`, i.e. never run.
"""

from __future__ import annotations

import json
import sys
import threading
import time

import pytest

from agent_swarm.forge import ForgeError
from agent_swarm.forge_store import ForgeStore, Role
from agent_swarm.job import TEST_RUN, Job
from agent_swarm.pull import Workbench
from agent_swarm.testing import RecordingForge
from agent_swarm.workbench_cli import Exit, build_parser, default_owner, main, visible_rows

NAMESPACE = 'cli-test'
JOB = Job(id='plain', kind=TEST_RUN)
KEY = JOB.claim_key()
NEEDY = Job(id='needy', kind=TEST_RUN)

#: A child that always succeeds / always fails, spelled through this interpreter so the tests need
#: no shell and run identically on Windows. `sys.executable` rather than `python`: the name on PATH
#: may not be the interpreter running the suite.
OK_CMD = [sys.executable, '-c', 'pass']
FAIL_CMD = [sys.executable, '-c', 'raise SystemExit(3)']


@pytest.fixture
def forge() -> RecordingForge:
    return RecordingForge()


@pytest.fixture
def submitter(forge) -> ForgeStore:
    return ForgeStore(NAMESPACE, forge, role=Role.SUBMITTER)


def _bench(forge, *, owner: str = 'human', capabilities=(), lease_seconds: float = 300.0) -> Workbench:
    store = ForgeStore(NAMESPACE, forge, role=Role.RUNNER, lease_seconds=lease_seconds)
    return Workbench(store, owner=owner, capabilities=capabilities)


class TestListSaysWhatWasOBSERVED:
    def test_it_exits_OK_and_names_the_work(self, forge, submitter, capsys):
        submitter.register(JOB)
        assert main(['list'], workbench=_bench(forge)) == Exit.OK
        assert KEY in capsys.readouterr().out

    def test_an_EMPTY_result_is_still_OK_because_looking_and_seeing_nothing_SUCCEEDED(self, forge, capsys):
        """The distinction the whole surface rests on, at the last place it can be lost. An empty
        queue is not a failure; failing to look is.
        """
        assert main(['list'], workbench=_bench(forge)) == Exit.OK
        out = capsys.readouterr().out
        assert 'VISIBLE' in out
        assert 'nothing to do' not in out.lower(), 'the empty case was laundered into a conclusion'

    def test_the_empty_message_NAMES_NUMBERS_a_person_can_act_on(self, forge, submitter, capsys):
        """ "18 visible, 0 you can do" sends someone to their capabilities; "18 visible, 18 you can
        do, 0 free" sends them to the board. Neither is reachable from the word "nothing".
        """
        for i in range(3):
            submitter.register(Job(id=f'needs{i}', kind=TEST_RUN), requires=['a-licensed-tool'])
        assert main(['list'], workbench=_bench(forge, capabilities=())) == Exit.OK
        out = capsys.readouterr().out
        assert 'observed: 3 open item(s)' in out
        assert '0 within this box' in out

    def test_an_UNREACHABLE_forge_is_a_DIFFERENT_exit_code_from_an_empty_one(self, forge, capsys):
        """THE JUDGED PROPERTY. Both render as "no work" to a careless CLI; they are opposite facts.
        `ForgeError` is what an unreachable forge raises -- there is no `ForgeUnreachable` type in
        this package, and this is where the distinction it was reaching for is preserved.
        """

        class Unreachable(RecordingForge):
            def list_work_items(self, *, state='all'):
                msg = 'connection refused'
                raise ForgeError(msg)

        assert main(['list'], workbench=_bench(Unreachable())) == Exit.FORGE_UNREACHABLE
        assert 'could not reach the forge' in capsys.readouterr().err

    def test_json_carries_the_same_facts_as_data(self, forge, submitter, capsys):
        submitter.register(JOB)
        assert main(['list', '--json'], workbench=_bench(forge)) == Exit.OK
        payload = json.loads(capsys.readouterr().out)
        assert payload['offered'] == [KEY]
        assert payload['visible'] == 1
        assert payload['bound_bit'] is False

    def test_a_BOUND_look_says_so_rather_than_reporting_no_work(self, forge, submitter, capsys):
        """ "No work available" and "none in the first K" are different sentences, and a person acts
        differently on each. `--limit 1` with a claimed head is the smallest case that shows it.
        """
        submitter.register(JOB)
        for i in range(4):
            submitter.register(Job(id=f'other{i}', kind=TEST_RUN))
        ForgeStore(NAMESPACE, forge, role=Role.RUNNER).try_claim(JOB, owner='the-ci-runner')

        assert main(['list', '--limit', '1', '--json'], workbench=_bench(forge)) == Exit.OK
        payload = json.loads(capsys.readouterr().out)
        assert payload['examined'] == 1
        assert payload['capable'] == 5
        assert payload['bound_bit'] is True

    def test_a_ZERO_limit_is_refused_rather_than_reporting_an_empty_queue(self, forge):
        """A bound that examines nothing is indistinguishable at the surface from an empty queue --
        the confusion `Survey` exists to prevent, arriving through a flag instead of a stale read.
        """
        with pytest.raises(ValueError, match='limit must be positive'):
            main(['list', '--limit', '0'], workbench=_bench(forge))


class TestKIsDerivedFromTheScreen:
    def test_it_comes_from_the_REAL_terminal_height(self, monkeypatch):
        """K was left unimplemented until a CLI existed because it is a property of a screen, not a
        taste. This asserts it is READ, not that it equals some number -- a value assertion cannot
        tell a derivation from a constant that happens to agree.
        """
        monkeypatch.setenv('COLUMNS', '80')
        monkeypatch.setenv('LINES', '50')
        tall = visible_rows()
        monkeypatch.setenv('LINES', '24')
        short = visible_rows()
        assert tall > short, 'K did not change with the terminal height; it is a constant'
        assert short == 20, 'the classic 24-row screen should leave 20 rows of jobs'

    def test_it_never_returns_a_USELESS_bound_on_a_tiny_screen(self, monkeypatch):
        """A two-row terminal must not produce a bound of zero or a negative, which `available`
        refuses -- the CLI would exit on a ValueError for the crime of being run in a small window.
        """
        monkeypatch.setenv('LINES', '2')
        assert visible_rows() >= 1

    def test_an_EXPLICIT_limit_overrides_the_screen(self, forge, submitter, capsys):
        for i in range(6):
            submitter.register(Job(id=f'j{i}', kind=TEST_RUN))
        assert main(['list', '--limit', '2', '--json'], workbench=_bench(forge)) == Exit.OK
        assert json.loads(capsys.readouterr().out)['examined'] == 2


class TestTakeIsTheSameCompareAndSwapThroughTheCLI:
    @pytest.mark.parametrize('round_number', range(4))
    def test_a_human_TYPING_take_and_a_runner_ticking_cannot_both_hold_one_item(self, round_number):
        """THE PROPERTY THE WHOLE SURFACE RESTS ON, raced through `main()` itself.

        Eight CLI invocations against eight bare `ForgeStore.try_claim` calls -- the call a CI runner
        makes -- released together from a `threading.Barrier`. A `main()` that claimed by any other
        route would pass every test in `test_pull_surface.py` and fail here.

        Four rounds on a fresh forge each, because one round with one winner is what a broken
        protocol also does most of the time.
        """
        forge = RecordingForge()
        ForgeStore(NAMESPACE, forge, role=Role.SUBMITTER).register(JOB)
        winners: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(16)

        def as_human(n: int) -> None:
            bench = _bench(forge, owner=f'human-{n}')
            barrier.wait()
            if main(['take', KEY, '--', *OK_CMD], workbench=bench) == Exit.OK:
                with lock:
                    winners.append(f'human-{n}')

        def as_ci(n: int) -> None:
            store = ForgeStore(NAMESPACE, forge, role=Role.RUNNER)
            barrier.wait()
            if store.try_claim(JOB, owner=f'ci-{n}'):
                with lock:
                    winners.append(f'ci-{n}')

        threads = [threading.Thread(target=as_human if i % 2 else as_ci, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(winners) == 1, f'{len(winners)} executors each believed they held it: {winners}'

    def test_a_successful_command_records_PASS_and_exits_OK(self, forge, submitter):
        submitter.register(JOB)
        bench = _bench(forge)
        assert main(['take', KEY, '--', *OK_CMD], workbench=bench) == Exit.OK
        assert bench.store.verdict(JOB) == 'PASS'

    def test_a_FAILING_command_records_FAIL_and_exits_WORK_FAILED(self, forge, submitter):
        """The work failing is not the CLI failing, and the two must be separable by a caller."""
        submitter.register(JOB)
        bench = _bench(forge)
        assert main(['take', KEY, '--', *FAIL_CMD], workbench=bench) == Exit.WORK_FAILED
        assert bench.store.verdict(JOB) == 'FAIL'

    def test_the_claim_is_GIVEN_BACK_after_the_answer(self, forge, submitter):
        submitter.register(JOB)
        bench = _bench(forge)
        main(['take', KEY, '--', *OK_CMD], workbench=bench)
        assert bench.store.claim_owner(JOB) is None

    def test_losing_the_race_starts_NOTHING_and_exits_LOST_THE_RACE(self, forge, submitter, tmp_path):
        """A loser that ran the command anyway would do the work twice and publish nobody's answer.
        Asserted on a SIDE EFFECT -- a file the child would have created -- because an exit code
        alone cannot tell "did not run it" from "ran it and discarded the result".
        """
        submitter.register(JOB)
        ForgeStore(NAMESPACE, forge, role=Role.RUNNER).try_claim(JOB, owner='the-ci-runner')
        witness = tmp_path / 'ran'
        code = main(
            ['take', KEY, '--', sys.executable, '-c', f'open({str(witness)!r}, "w").close()'],
            workbench=_bench(forge),
        )
        assert code == Exit.LOST_THE_RACE
        assert not witness.exists(), 'the loser ran the work anyway'

    def test_work_this_box_CANNOT_DO_exits_MISSING_CAPABILITY_not_LOST_THE_RACE(self, forge, submitter):
        """ "try again" and "never" are different answers, and a caller that cannot tell them apart
        retries a misconfigured box forever.
        """
        submitter.register(NEEDY, requires=['a-licensed-tool'])
        code = main(['take', NEEDY.claim_key(), '--', *OK_CMD], workbench=_bench(forge))
        assert code == Exit.MISSING_CAPABILITY

    def test_a_command_that_CANNOT_START_is_INCONCLUSIVE_and_never_FAIL(self, forge, submitter):
        """Nothing ran, so a FAIL would be a statement about work that never happened -- exactly the
        unearned verdict the vocabulary exists to prevent. INCONCLUSIVE is this case by definition.
        """
        submitter.register(JOB)
        bench = _bench(forge)
        code = main(['take', KEY, '--', 'a-program-that-does-not-exist-xyz'], workbench=bench)
        assert code == Exit.INCONCLUSIVE
        assert bench.store.verdict(JOB) == 'INCONCLUSIVE'

    def test_a_take_with_NO_command_is_a_usage_error_and_claims_nothing(self, forge, submitter):
        """There is no detached take. A `take` with nothing to run would be a claim with no process
        hosting its lease -- the parking defect, reachable by omitting an argument.
        """
        submitter.register(JOB)
        bench = _bench(forge)
        assert main(['take', KEY], workbench=bench) == Exit.USAGE
        assert bench.store.claim_owner(JOB) is None

    def test_a_NONSENSE_key_is_a_usage_error(self, forge):
        assert main(['take', 'not-a-key', '--', *OK_CMD], workbench=_bench(forge)) == Exit.USAGE

    def test_the_separator_is_EATEN_and_not_handed_to_the_child(self, forge, submitter):
        """`argparse.REMAINDER` keeps a leading `--`. Forwarding it verbatim is the failure motronics
        measured in its own installer: the tool read `--` as a program name and blamed the payload
        for a defect in the wrapper.
        """
        submitter.register(JOB)
        bench = _bench(forge)
        assert main(['take', KEY, '--', *OK_CMD], workbench=bench) == Exit.OK
        assert bench.store.verdict(JOB) == 'PASS'


class TestTheBeaterKeepsTheClaimAndStopsTheWorkWhenItCannot:
    def test_a_LONG_command_keeps_the_claim_alive_past_the_lease(self, forge, submitter):
        """THE HEARTBEAT'S WHOLE PURPOSE. With a 0.4 s lease and a 1.2 s child, an unbeaten claim
        would lapse mid-run and another executor could take a job that is still running.
        """
        submitter.register(JOB)
        bench = _bench(forge, lease_seconds=0.4)
        code = main(
            ['take', KEY, '--', sys.executable, '-c', 'import time; time.sleep(1.2)'],
            workbench=bench,
        )
        assert code == Exit.OK
        assert bench.store.verdict(JOB) == 'PASS'

    def test_losing_the_lease_mid_run_KILLS_the_child_and_writes_NO_verdict(self, forge, submitter):
        """A LOG IS NOT A SIGNAL. Another executor may already be running this job and about to
        publish; whatever this child produces is unpublishable at best and a duplicate at worst. So
        the work is killed and nothing is written -- verified by BOTH, since either alone would pass
        for a beater that merely printed a warning.
        """
        submitter.register(JOB)
        bench = _bench(forge, lease_seconds=0.4)

        def steal() -> None:
            time.sleep(0.6)  # after our lease lapses, before the child would finish
            ForgeStore(NAMESPACE, forge, role=Role.RUNNER).release(JOB, owner=bench.owner)
            ForgeStore(NAMESPACE, forge, role=Role.RUNNER).try_claim(JOB, owner='the-ci-runner')

        thief = threading.Thread(target=steal)
        thief.start()
        started = time.monotonic()
        code = main(
            ['take', KEY, '--', sys.executable, '-c', 'import time; time.sleep(30)'],
            workbench=bench,
        )
        elapsed = time.monotonic() - started
        thief.join()

        assert code == Exit.LEASE_LOST
        assert elapsed < 20, f'the child ran {elapsed:.1f}s; it should have been killed on the lost lease'
        assert bench.store.verdict(JOB) is None, 'a verdict was written for a claim we no longer held'

    def test_the_beater_is_a_DAEMON_so_it_cannot_outlive_its_process(self):
        """A non-daemon beater would politely hold a claim open while nobody is watching -- the
        parking defect, reintroduced by the mechanism that exists to prevent it.
        """
        from agent_swarm.claim import Beater  # noqa: PLC0415 -- reading an attribute, not binding a process

        # IMPORTED FROM `claim`, ITS HOME SINCE 2026-08-12. It was a nested class in the CLI, where
        # the only way to reach it was to import a command-line interface -- so the next consumer
        # that needed a heartbeat wrote its own, in another repository, deriving its own cadence in
        # exactly the way `beat_interval` exists to forbid. The assertion is unchanged; only the
        # address is.
        beater = Beater.__new__(Beater)
        beater.__init__(lambda: None, on_lost=lambda _e: None, interval=999.0)
        assert beater._thread.daemon is True

    def test_the_lease_this_CLI_uses_is_SHORT_enough_that_a_closed_terminal_frees_the_work(self):
        """The store's three-hour default exists for a runner that may not beat at all. An
        interactive executor beats, so its lease can be minutes -- which is the entire difference
        between "closed the lid, lost the job for the afternoon" and "lost it in five minutes".
        """
        from agent_swarm.workbench_cli import INTERACTIVE_LEASE_SECONDS  # noqa: PLC0415
        from agent_swarm.forge_store import DEFAULT_LEASE_SECONDS  # noqa: PLC0415

        assert INTERACTIVE_LEASE_SECONDS < DEFAULT_LEASE_SECONDS / 10


class TestReportIsTheSameVerdictNamespace:
    @pytest.mark.parametrize('word', ['PASS', 'FAIL', 'INCONCLUSIVE'])
    def test_the_three_words_round_trip(self, forge, submitter, word):
        submitter.register(JOB)
        bench = _bench(forge)
        bench.take(JOB)
        assert main(['report', KEY, '--verdict', word], workbench=bench) == Exit.OK
        assert bench.store.verdict(JOB) == word

    def test_a_FOURTH_word_is_refused_by_the_PARSER(self, forge):
        """Refused before any I/O and before any claim is touched. argparse exits 2, which is why
        `Exit.USAGE` is 2 -- one CLI, one usage code.
        """
        with pytest.raises(SystemExit) as exit_info:
            main(['report', KEY, '--verdict', 'DONE'], workbench=_bench(forge))
        assert exit_info.value.code == Exit.USAGE

    def test_reporting_on_work_this_owner_does_NOT_hold_exits_LEASE_LOST_and_writes_nothing(self, forge, submitter):
        submitter.register(JOB)
        ForgeStore(NAMESPACE, forge, role=Role.RUNNER).try_claim(JOB, owner='the-ci-runner')
        bench = _bench(forge)
        assert main(['report', KEY, '--verdict', 'PASS'], workbench=bench) == Exit.LEASE_LOST
        assert bench.store.verdict(JOB) is None


class TestIdentity:
    def test_the_owner_is_STABLE_across_processes(self):
        """A fresh identity per invocation would make a person unable to report on their own work,
        and the second process would read as a stranger trying to steal a live claim.
        """
        assert default_owner() == default_owner()
        assert '@' in default_owner()

    def test_it_is_OVERRIDABLE_because_a_TUI_agent_is_not_a_login(self, forge, submitter, capsys):
        submitter.register(JOB)
        main(['--owner', 'tui-agent-7', 'list'], workbench=_bench(forge, owner='tui-agent-7'))
        assert 'tui-agent-7' in capsys.readouterr().out


class TestTheCLIReachesTheSurfaceAndNotPastIt:
    def test_it_builds_no_claim_of_its_own(self):
        """If the CLI posted its own marker, a human and a runner would each hold "the" claim. The
        race above is the behavioural proof; this is the structural one, so a second protocol cannot
        be added here without deleting a test, which is a visible act.

        SEARCH SCOPE: the code tokens of `agent_swarm/workbench_cli.py`, strings and comments
        excluded.
        """
        import io  # noqa: PLC0415
        import tokenize  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        from agent_swarm import workbench_cli  # noqa: PLC0415

        source = Path(workbench_cli.__file__).read_text(encoding='utf-8')
        code = {
            token.string
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type not in (tokenize.STRING, tokenize.COMMENT)
        }
        banned = {'encode_claim', 'add_comment', 'delete_comment', 'update_comment', 'Arbiter'}
        assert not sorted(code & banned), f'the CLI reaches past the pull surface: {sorted(code & banned)}'

    def test_every_verb_is_reachable_from_the_parser(self):
        """A verb that exists in the dispatch table and not in the parser is unreachable, and a verb
        in the parser with no handler is a KeyError in front of a person.
        """
        parser = build_parser()
        actions = [a for a in parser._actions if a.dest == 'verb']
        assert actions, 'no subcommand action'
        assert set(actions[0].choices) == {'list', 'take', 'report'}
