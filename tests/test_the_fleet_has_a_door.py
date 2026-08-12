"""The collaboration half was complete and unreachable. This is the door.

MEASURED 2026-08-11, CONFIRMED 2026-08-12: `FabricSessionRunner`, `AgentTaskExecutor`, `run_one` and
`Fleet` were each built and each tested, and **`Fleet(...)` was constructed only in tests, in both
repositories.** Not a missing feature -- a missing entry point. `tick` is the driver and `clock` is
what repeats; neither assembles a fleet, because assembly is the operator's and had nowhere to live.

WHAT THESE TESTS ARE FOR, beyond "it runs". Three properties decide whether this is shippable:

1. **A misconfigured fleet fails at the FIRST tick, loudly.** `_executor_for` raises rather than
   skipping, because skipping leaves a job claimed-and-abandoned until its lease expires and then
   repeats forever -- a fleet that looks busy and completes nothing. The CLI must SURFACE that, not
   swallow it into a report that reads as a quiet pass.
2. **Idle is not a failure.** Most of a fleet is idle most of the time, and a non-zero exit for it
   makes any supervisor read a healthy quiet box as broken.
3. **A tick was actually ENTERED.** An entry point that assembles a fleet and never ticks reads
   exactly like one that ticked and found nothing: same exit code, same empty report. Only a control
   separates them.
"""

from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path


import pytest

from agent_swarm import fleet_cli
from agent_swarm.agent_executor import AgentTaskExecutor
from agent_swarm.forge_store import Role
from agent_swarm import tick as tick_module
from agent_swarm.job import AGENT_TASK
from agent_swarm.loop import Box
from agent_swarm.testing import RecordingForge
from agent_swarm.tick import Fleet, TickReport


def _fleet_with(fleet: Fleet, **overrides) -> Fleet:
    """A copy of `fleet` with fields replaced. `Fleet` is frozen, so a test that wants a DIFFERENT
    configuration must build one rather than mutate the one under test."""
    import dataclasses

    return dataclasses.replace(fleet, **overrides)


ROADMAP = """
version = 1

[[item]]
key = "alpha"
title = "The one item"
acceptance = "the verdict travels"
rem = "human"
"""

CONFIG = """
repo = "owner/name"
namespace = "ns-door"
owner = "box-1"
roadmap = "roadmap.toml"
available_gib = 64.0

[verifier]
argv = ["python", "-c", "pass"]
"""


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A config directory and a forge that never leaves the box.

    `default_forge` is patched at its DEFINING module, because `fleet_cli` imports it inside the
    functions that use it -- so patching a name bound at import time would silently miss.
    """
    (tmp_path / 'roadmap.toml').write_text(ROADMAP, encoding='utf-8')
    (tmp_path / 'fleet.toml').write_text(CONFIG, encoding='utf-8')

    import agent_swarm.forge as forge_module

    forges: list[RecordingForge] = []

    def _forge(role='agent', *, repo, base_url=None):
        made = RecordingForge()
        forges.append(made)
        return made

    monkeypatch.setattr(forge_module, 'default_forge', _forge)
    return tmp_path


class TestTheConfigRefusesToGuess:
    @pytest.mark.parametrize('key', fleet_cli.REQUIRED)
    def test_every_required_key_is_actually_required(self, workspace, key):
        """Each of these is a way to be confidently WRONG ABOUT SOMEBODY ELSE, not merely an
        inconvenience: a defaulted repo writes into a stranger's tracker, a defaulted namespace makes
        two fleets contend for one claim space, and a defaulted owner answers a job AS someone else
        -- which the claim protocol cannot detect, since owning a claim is being that name.
        """
        lines = [line for line in CONFIG.splitlines() if not line.startswith(f'{key} ')]
        (workspace / 'fleet.toml').write_text('\n'.join(lines), encoding='utf-8')
        with pytest.raises(fleet_cli.ConfigError, match=key):
            fleet_cli.load_config(workspace / 'fleet.toml')

    def test_it_names_EVERY_missing_key_at_once(self, workspace):
        """An operator fixing one error per run against a remote forge pays a round trip for each,
        and the second was knowable at the same instant as the first.
        """
        (workspace / 'fleet.toml').write_text('owner = "box-1"\n', encoding='utf-8')
        with pytest.raises(fleet_cli.ConfigError) as caught:
            fleet_cli.load_config(workspace / 'fleet.toml')
        assert 'repo' in str(caught.value) and 'namespace' in str(caught.value)

    def test_a_missing_verifier_is_refused_by_NAME(self, workspace):
        """The definition of done is the operator's. A package-supplied default would be the
        vendor-neutral-layer-holding-one-project's-fact defect wearing a command line.
        """
        (workspace / 'fleet.toml').write_text(CONFIG.split('[verifier]')[0], encoding='utf-8')
        config = fleet_cli.load_config(workspace / 'fleet.toml')
        with pytest.raises(fleet_cli.ConfigError, match='verifier'):
            fleet_cli.build_fleet(config, base_dir=workspace)

    def test_a_config_error_is_exit_2_and_NOT_a_traceback(self, workspace, capsys):
        (workspace / 'fleet.toml').write_text('owner = "box-1"\n', encoding='utf-8')
        assert fleet_cli.main(['--config', str(workspace / 'fleet.toml')]) == 2
        err = capsys.readouterr().err
        assert 'configuration error' in err and 'Traceback' not in err

    def test_paths_resolve_against_the_CONFIG_not_the_cwd(self, workspace, monkeypatch, tmp_path):
        """A config meaning something different depending on where it was invoked from is the same
        defect as a CWD-relative test scan: right for whoever wrote it, silently another tree for
        everyone else.
        """
        elsewhere = tmp_path / 'elsewhere'
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        fleet = fleet_cli.build_fleet(fleet_cli.load_config(workspace / 'fleet.toml'), base_dir=workspace)
        assert [item.key for item in fleet.roadmap.items] == ['alpha']


class TestTheFleetIsAssembledCorrectly:
    def test_the_two_stores_have_DIFFERENT_roles(self, workspace):
        """`Fleet.__post_init__` refuses interchangeable roles, and handing one store to both is the
        configuration that produced eight duplicate items per round. This asserts the CLI passes the
        right one to each rather than merely that the guard exists.
        """
        fleet = fleet_cli.build_fleet(fleet_cli.load_config(workspace / 'fleet.toml'), base_dir=workspace)
        assert fleet.submitter.role is Role.SUBMITTER
        assert fleet.runner.role is Role.RUNNER

    def test_the_two_stores_SHARE_one_index(self, workspace):
        fleet = fleet_cli.build_fleet(fleet_cli.load_config(workspace / 'fleet.toml'), base_dir=workspace)
        assert fleet.submitter.index is fleet.runner.index

    def test_a_remote_session_gets_NO_workspace(self, workspace):
        """THE GUARD'S INSTRUCTION, NOT A WORKAROUND FOR IT. `AgentTaskExecutor.__init__` refuses a
        local Workspace beside a remote session, because it answers "changed nothing" about a tree
        the session never touched -- confidently, and in the direction that looks safe.
        """
        fleet = fleet_cli.build_fleet(fleet_cli.load_config(workspace / 'fleet.toml'), base_dir=workspace)
        executor = fleet.executors[AGENT_TASK]
        assert isinstance(executor, AgentTaskExecutor)
        assert executor.session.executes_remotely() is True
        assert executor.workspace is None

    def test_no_status_publisher_without_a_status_section(self, workspace):
        """A box without the verifier credential is a LEGITIMATE fleet member. Building a publisher
        unconditionally would make every such box fail at construction.
        """
        fleet = fleet_cli.build_fleet(fleet_cli.load_config(workspace / 'fleet.toml'), base_dir=workspace)
        assert fleet.status is None


class TestTheMisconfiguredFleetFailsAtTheFirstTick:
    """THE PROPERTY MOST WORTH TESTING. A kind with no executor must not be skipped: skipping leaves
    the job claimed-and-abandoned until the lease expires, then repeats forever."""

    def test_a_kind_with_no_executor_RAISES_rather_than_reporting_a_quiet_pass(self, workspace):
        """`_executor_for` raises for exactly this reason, and this drives the REAL `tick`.

        **IT IS BUILT BY EMPTYING THE EXECUTOR MAP, NOT BY SCHEDULING A `TEST_RUN`**, and that is a
        finding rather than a convenience: `roadmap._parse_item` hardcodes `kind=AGENT_TASK`, so a
        roadmap CANNOT schedule any other kind. A test that wrote `kind = "test-run"` into a roadmap
        would silently get an agent task and pass for the wrong reason -- which is what my first
        version of this test did.
        """
        fleet = _fleet_with(
            fleet_cli.build_fleet(fleet_cli.load_config(workspace / 'fleet.toml'), base_dir=workspace), executors={}
        )
        with pytest.raises(KeyError, match='no executor configured'):
            tick_module.tick(fleet, Box(available_gib=64.0))

    def test_main_does_NOT_swallow_an_executor_error_into_exit_zero(self, workspace, monkeypatch):
        """The CLI half of the same property. A configuration error surfacing as a clean exit 0 with
        an empty report is precisely "a fleet that looks busy and completes nothing", one layer up.
        """

        def _explode(*_a, **_k):
            msg = "no executor configured for 'agent-task'"
            raise KeyError(msg)

        monkeypatch.setattr(fleet_cli, 'tick', _explode)
        with pytest.raises(KeyError, match='no executor configured'):
            fleet_cli.main(['--config', str(workspace / 'fleet.toml')])

    def test_the_limit_is_ANNOUNCED_before_the_tick_that_would_hit_it(self, workspace):
        """Better than a puzzling failure: the fleet says at startup which kinds it cannot run. A
        limit met as a strange result costs more than one read at the top of a log.
        """
        fleet = _fleet_with(
            fleet_cli.build_fleet(fleet_cli.load_config(workspace / 'fleet.toml'), base_dir=workspace), executors={}
        )
        limits = ' '.join(fleet_cli.report_known_limits(fleet))
        assert 'agent-task' in limits and 'no executor' in limits

    def test_it_announces_that_agent_tasks_cannot_PASS_today(self, workspace):
        """A REAL SHIPPING LIMIT, printed rather than discovered. Every session is remote, so nothing
        local can say whether it changed anything, and the executor refuses to judge on evidence it
        does not have -- so every agent task answers INCONCLUSIVE until the transport reports
        `SessionOutcome.changed` itself. An operator meeting that as an empty backlog would file a
        bug against the wrong component.
        """
        fleet = fleet_cli.build_fleet(fleet_cli.load_config(workspace / 'fleet.toml'), base_dir=workspace)
        assert any('INCONCLUSIVE' in limit for limit in fleet_cli.report_known_limits(fleet))


class TestOneTickAndOnlyOne:
    def test_a_tick_is_ACTUALLY_ENTERED(self, workspace, monkeypatch, capsys):
        """THE CONTROL, and without it every test in this file passes for a `main` that assembles a
        fleet and returns. An entry point that never ticks reads exactly like one that ticked and
        found nothing: same exit 0, same empty report.
        """
        entered: list[Fleet] = []

        def _tick(fleet, box, **kwargs):
            entered.append(fleet)
            return TickReport()

        monkeypatch.setattr(fleet_cli, 'tick', _tick)
        assert fleet_cli.main(['--config', str(workspace / 'fleet.toml')]) == 0
        assert len(entered) == 1, 'the entry point did not take a turn'

    def test_it_ticks_ONCE_and_does_not_loop(self, workspace, monkeypatch):
        """`clock` is what repeats, from outside, spawning a fresh process per pass so a tick that
        dies takes nothing with it. A loop here would be the second scheduler this design refuses.
        """
        calls = []
        monkeypatch.setattr(fleet_cli, 'tick', lambda *a, **k: calls.append(1) or TickReport())
        fleet_cli.main(['--config', str(workspace / 'fleet.toml')])
        assert len(calls) == 1

    def test_the_box_carries_the_configured_capacity(self, workspace, monkeypatch):
        """`Box.available_gib` of `None` REFUSES capacity-limited work rather than allowing it, so a
        defaulted one produces a fleet that starts cleanly and never runs anything.
        """
        seen = {}
        monkeypatch.setattr(fleet_cli, 'tick', lambda _f, box, **k: seen.update(gib=box.available_gib) or TickReport())
        fleet_cli.main(['--config', str(workspace / 'fleet.toml')])
        assert seen['gib'] == 64.0

    def test_an_IDLE_tick_exits_ZERO(self, workspace, monkeypatch, capsys):
        """Most of a fleet is idle most of the time. A non-zero exit for it makes any supervisor read
        a healthy quiet box as broken and thrash on it.
        """
        monkeypatch.setattr(fleet_cli, 'tick', lambda *a, **k: TickReport())
        assert fleet_cli.main(['--config', str(workspace / 'fleet.toml')]) == 0
        assert 'considered:     -' in capsys.readouterr().out

    def test_submit_is_OPT_IN(self, workspace, monkeypatch):
        """`submit` must run on exactly ONE box -- its own docstring names concurrent submission as
        the residual no re-read closes -- so a flag every box would set by default is the wrong
        default to offer.
        """
        submitted = []
        monkeypatch.setattr(fleet_cli, 'tick', lambda *a, **k: TickReport())
        monkeypatch.setattr(fleet_cli, 'submit', lambda fleet: submitted.append(1) or ['alpha'])

        fleet_cli.main(['--config', str(workspace / 'fleet.toml')])
        assert submitted == [], 'submit ran without being asked; every box would create items'

        fleet_cli.main(['--config', str(workspace / 'fleet.toml'), '--submit'])
        assert submitted == [1]


class TestTheQuietestWayToShipADeadBox:
    """FOUND BY WRITING THE CLI, NOT BY READING IT, and it is the failure mode this file's whole
    premise is about.

    An `expensive` job with no `ram_gib` is priced at an ASSUMED 12.5 GiB plus a 2 GiB reserve, so
    any `available_gib` under ~14.5 blocks every job on the roadmap. The tick then completes cleanly,
    considers nothing, reports idle and exits 0 -- **indistinguishable from a healthy box with an
    empty backlog, forever.** My own first config said `available_gib = 8.0`, which is a plausible
    number for a small box, and the fleet was silently inert.

    `capacity_blocker` is right to refuse. What was missing is anybody SAYING so at the moment the
    number is chosen, which is here.
    """

    def _fleet_on(self, workspace, gib: float):
        return fleet_cli.build_fleet(fleet_cli.load_config(workspace / 'fleet.toml'), base_dir=workspace), Box(
            available_gib=gib
        )

    def test_a_box_too_small_for_ANY_job_says_so(self, workspace):
        fleet, box = self._fleet_on(workspace, 8.0)
        limits = ' '.join(fleet_cli.report_known_limits(fleet, box))
        assert 'NOTHING on this roadmap fits this box' in limits
        assert '12.5' in limits, 'the limit must quote the blocker, or the operator cannot pick a number'

    def test_a_box_that_FITS_is_silent_about_capacity(self, workspace):
        """The discriminating half. A warning that fires for every configuration is noise, and an
        operator who learns to scroll past it will scroll past the one that mattered.
        """
        fleet, box = self._fleet_on(workspace, 64.0)
        assert not any('fits this box' in limit for limit in fleet_cli.report_known_limits(fleet, box))

    def test_the_limit_reaches_STDERR_on_a_real_run(self, workspace, monkeypatch, capsys):
        """Printed, not merely computed. A guard reporting into a discarded stdout is
        indistinguishable from no guard.
        """
        (workspace / 'fleet.toml').write_text(CONFIG.replace('64.0', '8.0'), encoding='utf-8')
        monkeypatch.setattr(fleet_cli, 'tick', lambda *a, **k: TickReport())
        assert fleet_cli.main(['--config', str(workspace / 'fleet.toml')]) == 0
        assert 'NOTHING on this roadmap fits this box' in capsys.readouterr().err


class TestItCannotBecomeAnUnattendedRunner:
    """USER DIRECTIVE, restated 2026-08-12: every loop is started by a HUMAN, in a TERMINAL. Always.

    7x24 does not mean unattended -- it means a long-lived VISIBLE loop, and uptime comes from the
    loop running continuously rather than from nobody having started it. `lifetime` makes it
    structural (Job Object with KILL_ON_JOB_CLOSE; SIGHUP to the process group), `ci_loop` refuses to
    start unbound, and motronics carries a strict-xfail tripwire that reds if a scheduler returns.

    **THIS IS A SOURCE GUARD BECAUSE BEHAVIOUR CANNOT CATCH IT IN THE USEFUL DIRECTION.** A
    `while True: tick(); sleep(60)` would not fail a behavioural test -- it would HANG one, which
    reads as a slow suite rather than as a design violation, and a hanging test is the thing most
    likely to be marked flaky and skipped.

    THE MEASURED FAILURE THIS DEFENDS AGAINST: a scheduled task on this project whose `LastRunTime`
    read 1932 while a source-tree guard reported the runner present and correct. A guard going green
    is not evidence the design was respected -- so the guard has to be over the thing that would
    actually change.
    """

    def _code_tokens(self) -> list[str]:
        """Every token of `fleet_cli` EXCEPT strings and comments.

        The module docstring necessarily spells `--daemon`, `sleep` and `while` in order to forbid
        them, so a substring search would fire on the prohibition itself -- forcing the prose to be
        deleted to keep the guard green, which is how a rule loses its explanation.
        """
        source = Path(fleet_cli.__file__).read_text(encoding='utf-8')
        return [
            token.string
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type not in (tokenize.STRING, tokenize.COMMENT)
        ]

    def test_there_is_no_loop_and_no_sleep_in_the_entry_point(self):
        tokens = self._code_tokens()
        banned = {'while', 'sleep', 'daemon', 'detach', 'fork', 'Timer', 'Thread', 'schtasks', 'nohup', 'Popen'}
        offenders = sorted({t for t in tokens if t in banned})
        assert not offenders, f'{offenders} in a one-shot: this file is becoming a service'

    def test_no_flag_offers_to_run_it_unattended(self):
        """Not even behind a flag. A flag is a SECOND ENTRY POINT, and this project has deleted one
        for exactly that reason -- the locked install path that nobody could express, so everybody
        typed the unlocked command instead.
        """
        source = Path(fleet_cli.__file__).read_text(encoding='utf-8')
        for flag in ('--daemon', '--detach', '--background', '--loop', '--watch', '--interval'):
            assert f"add_argument('{flag}'" not in source, f'{flag} makes this a service behind a flag'

    def test_tick_is_called_EXACTLY_once_in_the_source(self):
        """The behavioural sibling (`test_it_ticks_ONCE_and_does_not_loop`) counts calls in one run;
        this counts them in the SOURCE. A second call site added under a condition no test exercises
        would satisfy the first and not this one.

        COUNTED WITH `ast`, NOT BY TOKEN, and that difference is the reliability of the check:
        `from agent_swarm.tick import ..., tick` contributes two `tick` tokens before any call
        exists, so a token count reads 3 for a CORRECT file and would have to be pinned to 3 -- a
        number that means nothing and that the next reader would "fix" to 1. A `Call` node is the
        thing actually being counted.
        """
        tree = ast.parse(Path(fleet_cli.__file__).read_text(encoding='utf-8'))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'tick'
        ]
        assert len(calls) == 1, f'{len(calls)} `tick` call sites; a one-shot has exactly one'
