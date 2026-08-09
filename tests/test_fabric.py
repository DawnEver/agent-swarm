"""The fabric transport: L0, measured rather than assumed.

WHAT WAS ESTABLISHED, from a plain bash shell with NO Claude session involved (fabric 0.1.17,
2026-08-10). This mattered because "callable from a library process" and "callable only from inside
a Claude session" are different systems, and only the first can run a fleet unattended:

    ping                      3 nodes ALIVE (G, WS1, WS2), 0/64 sessions each
    spawn+send+close, codex   exitCode 0, reply 'BANANA', spawn 633 ms, total 7306 ms
    spawn+send+close, claude  exitCode 1, reply = a model-selection ERROR in fluent English
    node down                 ECONNREFUSED in 18 ms
    unknown provider          JSON-RPC -32000 naming the configured providers
    bogus model, claude       exitCode 1, fluent English explaining the problem
    bogus model, codex        exitCode 0 and an EMPTY reply

THE CLAUDE RESULT IS WHY THE EXECUTOR EXISTS IN THE SHAPE IT DOES. That session started, ran a turn,
answered in confident prose and closed -- and the prose was an error message. Anything keying "done"
off the text would have scored it as work completed. The exit code disagreed, so `completed` is
keyed to the exit code -- AND THE EXIT CODE IS NOT ENOUGH EITHER, which a later probe corrected me
on: codex given the same bogus model exits 0 with an empty reply. The two providers fail with
different signatures and neither signal is trustworthy alone, which is why the guard that refuses to
call an inert session a success lives in the EXECUTOR, where it can see what the job was.

THE OFFLINE HALF IS NOT A MOCK OF THE LIVE HALF. It tests the pure decisions -- version ordering,
ping parsing, what counts as an unavailable transport -- and those are where a wrong answer silently
pins the fleet to an unmeasured version or reports a dead fleet as a busy one.
"""

from __future__ import annotations

import pytest

from agent_swarm.agent_executor import AgentTaskExecutor, SessionRunner, StaticBrief
from agent_swarm.fabric import (
    MEASURED_SESSIONS_PER_NODE,
    FabricSessionRunner,
    SessionTransportUnavailable,
    fleet_capacity,
    latest_plugin_dir,
    parse_ping,
)
from agent_swarm.job import AGENT_TASK, Job

TASK = Job(id='probe', kind=AGENT_TASK)

REAL_PING = (
    'G ALIVE v0.1.17 up=12597s cpu=32 free=11627MB sessions=0/64\n'
    'WS1 ALIVE v0.1.17 up=12048s cpu=32 free=9924MB sessions=0/64\n'
    'WS2 ALIVE v0.1.17 up=12590s cpu=24 free=42381MB sessions=0/64\n'
)


class TestPingIsParsedIntoFACTS:
    def test_the_real_output_parses(self):
        nodes = parse_ping(REAL_PING)
        assert [n.name for n in nodes] == ['G', 'WS1', 'WS2']
        assert nodes[2].free_mb == 42381
        assert nodes[0].max_sessions == MEASURED_SESSIONS_PER_NODE

    def test_a_DEAD_node_is_absent_rather_than_reported_as_empty(self):
        """Zero capacity means "full and working". A dead node rendered as a node with no free slots
        would let a caller report an unreachable fleet as a merely busy one -- and a busy fleet is
        something you wait for.
        """
        nodes = parse_ping(f'{REAL_PING}WS3 DEAD ECONNREFUSED: connect ECONNREFUSED 10.0.0.9:7777\n')
        assert [n.name for n in nodes] == ['G', 'WS1', 'WS2']

    def test_free_slots_is_derived_not_declared(self):
        assert parse_ping('G ALIVE v0.1.17 up=1s cpu=8 free=100MB sessions=60/64')[0].free_slots == 4

    def test_free_slots_never_goes_NEGATIVE(self):
        """A node reporting more sessions than its ceiling is possible after a config change, and a
        negative capacity would propagate into admission arithmetic as a phantom allowance.
        """
        assert parse_ping('G ALIVE v0.1.17 up=1s cpu=8 free=100MB sessions=70/64')[0].free_slots == 0

    def test_garbage_is_skipped_not_guessed_at(self):
        assert parse_ping('this is not a ping line\n\n') == []

    def test_the_fleet_ceiling_is_the_SESSION_cap_not_the_memory(self):
        """Worth an assertion because it decides which knob to turn. Three nodes at 64 is 192 slots
        against ~56 GB free; at ~200 MB per session memory would allow ~280. The operator-declared
        ceiling binds first, so raising RAM buys nothing until it moves.
        """
        nodes = parse_ping(REAL_PING)
        slots = sum(n.free_slots for n in nodes)
        by_memory = sum(n.free_mb for n in nodes) // 200
        assert slots < by_memory, 'if memory ever binds first, this comment is stale and so is the plan'


class TestVersionSelectionIsNUMERIC:
    def test_a_two_digit_patch_beats_a_one_digit_one(self, tmp_path):
        """`0.1.9` must not beat `0.1.17`, and a lexical sort says it does. The wrong answer here
        silently pins the fleet to a version whose behaviour was never measured -- and everything
        would keep working, differently.
        """
        for name in ('0.1.9', '0.1.10', '0.1.17', '0.1.15'):
            (tmp_path / name).mkdir()
        assert latest_plugin_dir(tmp_path).name == '0.1.17'

    def test_a_missing_cache_REFUSES_rather_than_returning_a_default(self, tmp_path):
        with pytest.raises(SessionTransportUnavailable, match='no fabric plugin cache'):
            latest_plugin_dir(tmp_path / 'absent')

    def test_an_empty_cache_refuses_too(self, tmp_path):
        with pytest.raises(SessionTransportUnavailable, match='no fabric version'):
            latest_plugin_dir(tmp_path)

    def test_non_version_directories_are_ignored(self, tmp_path):
        (tmp_path / '0.1.2').mkdir()
        (tmp_path / 'scratch').mkdir()
        assert latest_plugin_dir(tmp_path).name == '0.1.2'


class TestTheTransportIsOPTIONAL:
    """`node` is a hard dependency of this ADAPTER and must never become one of `agent_swarm`."""

    def test_the_package_imports_with_no_node_present(self):
        """Asserted by the fact that this module imported at all. The refusal must happen at USE."""
        assert FabricSessionRunner is not None

    def test_construction_performs_no_io(self):
        """So a fleet config can be built on a box that cannot run sessions."""
        runner = FabricSessionRunner(provider='codex', node_binary='definitely-not-a-real-binary')
        assert runner.provider == 'codex'

    def test_a_missing_node_binary_raises_at_RUN(self, tmp_path):
        runner = FabricSessionRunner(node_binary='definitely-not-a-real-binary', plugin_dir=tmp_path)
        with pytest.raises(SessionTransportUnavailable, match='not on PATH'):
            runner.run('do the thing', job=TASK)

    def test_a_directory_that_is_not_a_plugin_is_refused_by_NAME(self, tmp_path):
        """ "Not a plugin" and "plugin whose spawn failed" need different responses, so the check is
        for the file that would actually be imported rather than for the directory existing.
        """
        runner = FabricSessionRunner(plugin_dir=tmp_path)
        with pytest.raises(SessionTransportUnavailable, match='does not look like a fabric plugin'):
            runner.run('do the thing', job=TASK)

    def test_it_satisfies_the_SEAM(self):
        assert isinstance(FabricSessionRunner(), SessionRunner)

    def test_an_unavailable_transport_becomes_INCONCLUSIVE_not_a_crash(self, tmp_path):
        """The executor already turns any session exception into INCONCLUSIVE, which is the honest
        verdict for a box that could not run the work: nobody ran it, so nobody knows. Asserted here
        because the two modules are what make it true together.
        """

        class _Verifier:
            def verify(self, job):  # pragma: no cover -- must never be reached
                raise AssertionError('the gate was asked about work that never ran')

        class _Workspace:
            def fingerprint(self) -> str:
                return 'unchanged'

        executor = AgentTaskExecutor(
            session=FabricSessionRunner(plugin_dir=tmp_path),
            verifier=_Verifier(),
            workspace=_Workspace(),
            brief=StaticBrief(),
        )
        verdict, detail = executor.execute(TASK)
        assert verdict == 'INCONCLUSIVE'
        assert 'SessionTransportUnavailable' in detail


class TestTheSeamNeededNoProviderBRANCH:
    """The claim that makes this an abstraction rather than a wrapper.

    `claude` and `codex` are genuinely different backends -- a native CLI and an app-server -- and
    both were driven through the SAME fields and answered in the same shape. If a second provider had
    needed a different field, the seam would be in the wrong place, and the honest response would
    have been to say so rather than to add a branch.
    """

    def test_the_runner_takes_the_provider_as_DATA(self):
        assert FabricSessionRunner(provider='claude').provider == 'claude'
        assert FabricSessionRunner(provider='codex').provider == 'codex'

    def test_no_provider_specific_branch_exists(self):
        import io
        import tokenize
        from pathlib import Path

        from agent_swarm import fabric as fabric_module

        source = Path(fabric_module.__file__).read_text(encoding='utf-8')
        code = [
            token.string
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type not in (tokenize.STRING, tokenize.COMMENT)
        ]
        assert 'claude' not in code, 'a provider name reached the code, not just the defaults'

    def test_WRITE_defaults_to_false(self):
        """A runner that always granted write would leave the executor's no-change guard as the only
        thing between a stray session and the repository.
        """
        assert FabricSessionRunner().write is False


@pytest.mark.live_fabric
class TestTheRealFleet:
    def test_the_fleet_answers_from_a_plain_process(self):
        """THE QUESTION THAT DECIDED THE ARCHITECTURE. If fabric were reachable only from inside a
        Claude session, no unattended runner could ever spawn agent work.
        """
        nodes = fleet_capacity()
        assert nodes, 'no fabric node answered'
        assert all(n.max_sessions > 0 for n in nodes)

    def test_a_real_session_spawns_sends_and_closes(self):
        """End to end, with a question whose answer is checkable. A transport test whose assertion
        was "it did not raise" would pass for a session that returned nothing at all.
        """
        outcome = FabricSessionRunner(provider='codex').run(
            'Reply with exactly the word BANANA and nothing else.', job=TASK
        )
        assert outcome.completed is True
        assert 'BANANA' in outcome.self_report

    def test_the_two_providers_FAIL_DIFFERENTLY_and_neither_signature_is_trusted_alone(self):
        """MEASURED, and it corrected an assumption I had already written down.

        Given a model that does not exist:
          * `claude` returns exitCode 1 with fluent English explaining the problem.
          * `codex` returns exitCode **0** with an EMPTY reply.

        So neither signal alone is sufficient. The exit code catches the first and misses the
        second; the text catches the second and is actively misleading for the first, since the
        claude reply reads like a completed report. This is exactly why `completed` means only
        "the session process ended cleanly" and why the executor's no-change guard -- not this
        layer -- is what refuses to call a silently inert session a success.
        """
        outcome = FabricSessionRunner(provider='codex', model='definitely-not-a-model').run(
            'Reply with the word BANANA.', job=TASK
        )
        assert outcome.self_report.strip() == '', 'codex answered a bogus model; re-measure the claim above'

    def test_a_silently_INERT_session_still_cannot_produce_a_PASS(self):
        """The property that actually matters, asserted end to end rather than inferred.

        A session that exits 0 having done nothing is the case the transport cannot detect. The
        executor covers it by refusing to attribute an unchanged tree to a task -- so the gate is
        never even asked, and no PASS can be manufactured.
        """

        class _Verifier:
            def verify(self, job):  # pragma: no cover -- reaching this IS the bug
                raise AssertionError('the gate was asked about a tree nothing touched')

        class _Workspace:
            def fingerprint(self) -> str:
                return 'unchanged'

        executor = AgentTaskExecutor(
            session=FabricSessionRunner(provider='codex', model='definitely-not-a-model'),
            verifier=_Verifier(),
            workspace=_Workspace(),
            brief=StaticBrief(),
        )
        verdict, detail = executor.execute(TASK)
        assert verdict == 'INCONCLUSIVE'
        assert 'changed nothing' in detail

    def test_an_unknown_PROVIDER_is_a_transport_failure_not_a_bad_session(self):
        """It is a configuration error, and reporting it as a session that went badly would send the
        reader to read a transcript that does not exist.
        """
        with pytest.raises(SessionTransportUnavailable, match='not found'):
            FabricSessionRunner(provider='not-a-provider').run('hello', job=TASK)
