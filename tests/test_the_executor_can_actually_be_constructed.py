"""`AgentTaskExecutor` had NO constructible arguments. Two protocols, zero implementations.

MEASURED 2026-08-12, and it is worse than the "no call site" it was filed as:

    grep -rn "def verify(self"      src/agent_swarm/   -> only the Protocol
    grep -rn "def fingerprint(self" src/agent_swarm/   -> only the Protocol

`Verifier` and `Workspace` were declared, documented, depended upon by the executor's whole argument
-- and implemented nowhere. So the agent-execution path was not merely unwired: it could not be
wired, by anyone, without first writing two adapters nobody had noticed were missing. A protocol
with no implementation is a description of an intention, and the suite was green because every test
supplied its own fake.

THE TWO ADAPTERS ARE THE PRODUCTION HALF. `CommandVerifier` is the definition of done as an operator
actually states it -- a command line -- and `TreeWorkspace` is "did anything change" on a real
directory. Neither may name a project; both are `agent_swarm`'s, and the argv belongs to the caller.
"""

from __future__ import annotations

import sys

import pytest

from agent_swarm.adapters import CommandVerifier, TreeWorkspace
from agent_swarm.agent_executor import INCONCLUSIVE, Verifier, Workspace
from agent_swarm.job import TEST_RUN, Job

JOB = Job(id='alpha', kind=TEST_RUN)


def _python(*code: str) -> list[str]:
    """An argv that runs THIS interpreter, so the tests need no shell and no fixture binary."""
    return [sys.executable, '-c', '; '.join(code)]


class TestCommandVerifierAnswersInThreeWords:
    """A verifier that cannot tell "it failed" from "it did not run" is the defect this package
    exists to remove -- it converts "I do not know" into a verdict AGAINST the work, and the work is
    then marked done-and-wrong rather than re-runnable."""

    def test_exit_zero_is_PASS(self):
        verdict, _detail = CommandVerifier(_python('pass'), timeout_s=30).verify(JOB)
        assert verdict == 'PASS'

    def test_a_nonzero_exit_is_FAIL(self):
        verdict, _detail = CommandVerifier(_python('import sys; sys.exit(3)'), timeout_s=30).verify(JOB)
        assert verdict == 'FAIL'

    def test_a_command_that_CANNOT_START_is_INCONCLUSIVE_not_FAIL(self):
        """THE DISCRIMINATING ASSERTION. A missing binary, a bad path, a permission error -- none of
        those are a statement about the code under test, and reporting FAIL would attribute the
        operator's misconfiguration to somebody's work and close the item.
        """
        verdict, detail = CommandVerifier(['no-such-binary-anywhere-xyzzy'], timeout_s=30).verify(JOB)
        assert verdict == INCONCLUSIVE
        assert 'could not run' in detail

    def test_a_TIMEOUT_is_INCONCLUSIVE_not_FAIL(self):
        """The other half, and the one that bites in production: a gate that hangs has not failed,
        it has not answered. INCONCLUSIVE is also the word that means re-runnable.
        """
        verdict, detail = CommandVerifier(_python('import time; time.sleep(30)'), timeout_s=0.5).verify(JOB)
        assert verdict == INCONCLUSIVE
        assert 'timed out' in detail and '0.5' in detail

    def test_every_verdict_is_in_the_shared_vocabulary(self):
        """A fourth word here would be laundered into a verdict far away. `AgentTaskExecutor` raises
        on an unknown one, so this asserts the source rather than waiting for that.
        """
        from agent_swarm.store import VERDICTS

        for argv in (_python('pass'), _python('import sys; sys.exit(1)'), ['no-such-binary-anywhere-xyzzy']):
            verdict, _detail = CommandVerifier(argv, timeout_s=5).verify(JOB)
            assert verdict in VERDICTS


class TestTheDetailIsUsefulAndBOUNDED:
    def test_the_detail_carries_the_exit_code(self):
        _verdict, detail = CommandVerifier(_python('import sys; sys.exit(7)'), timeout_s=30).verify(JOB)
        assert 'exit 7' in detail

    def test_the_detail_carries_the_TAIL_of_the_output(self):
        """The tail, not the head: a failure's cause is at the END of a run, and a head-truncated
        report shows the banner of a tool that was about to say something useful.
        """
        _verdict, detail = CommandVerifier(
            _python(r"print('EARLY_MARKER'); print('LATE_MARKER')"), timeout_s=30
        ).verify(JOB)
        assert 'LATE_MARKER' in detail

    def test_the_detail_is_TRUNCATED_because_it_lands_in_a_forge_comment(self):
        """A whole gate log is megabytes. It goes into a comment on a work item, so an untruncated
        detail is a request the forge rejects -- the verdict lost to the size of its own evidence.
        """
        _verdict, detail = CommandVerifier(_python(r"print('x' * 100000)"), timeout_s=30, detail_tail=500).verify(JOB)
        assert len(detail) < 1500, f'detail was {len(detail)} chars; it goes in a comment'
        assert 'truncated' in detail


class TestTheJobIsInterpolatedAndTheProjectIsNOT:
    def test_the_claim_key_reaches_the_command(self):
        """Same idiom as `StaticBrief`: `{key}` in an argv element. Without it every job runs the
        identical command and the verifier cannot be told WHICH job it is answering.
        """
        _verdict, detail = CommandVerifier(_python(r"print('ran for {key}')"), timeout_s=30).verify(JOB)
        assert JOB.claim_key() in detail

    def test_an_argv_with_no_placeholder_is_left_alone(self):
        """A literal `{` in a command line must not explode. Braces are ordinary shell-adjacent
        characters and an operator will eventually type one.
        """
        verdict, _detail = CommandVerifier(_python(r"print('{}')"), timeout_s=30).verify(JOB)
        assert verdict == 'PASS'

    def test_it_satisfies_the_protocol_it_was_written_for(self):
        assert isinstance(CommandVerifier(['true'], timeout_s=1), Verifier)


class TestTreeWorkspace:
    def test_an_untouched_tree_fingerprints_the_same(self, tmp_path):
        (tmp_path / 'a.txt').write_text('hello', encoding='utf-8')
        workspace = TreeWorkspace(tmp_path)
        assert workspace.fingerprint() == workspace.fingerprint()

    def test_a_changed_SIZE_is_seen(self, tmp_path):
        target = tmp_path / 'a.txt'
        target.write_text('hello', encoding='utf-8')
        before = TreeWorkspace(tmp_path).fingerprint()
        target.write_text('hello world', encoding='utf-8')
        assert TreeWorkspace(tmp_path).fingerprint() != before

    def test_a_NEW_file_is_seen(self, tmp_path):
        (tmp_path / 'a.txt').write_text('hello', encoding='utf-8')
        before = TreeWorkspace(tmp_path).fingerprint()
        (tmp_path / 'b.txt').write_text('new', encoding='utf-8')
        assert TreeWorkspace(tmp_path).fingerprint() != before

    def test_a_file_MOVED_between_directories_is_seen(self, tmp_path):
        """THE BUG IN THE VERSION THIS PROMOTES. `test_end_to_end`'s workspace keyed on `p.name`, so
        two same-sized files with one name in different directories were indistinguishable and a
        move between directories was invisible. The relative PATH costs nothing and says the truth.
        """
        (tmp_path / 'one').mkdir()
        (tmp_path / 'two').mkdir()
        (tmp_path / 'one' / 'a.txt').write_text('hello', encoding='utf-8')
        before = TreeWorkspace(tmp_path).fingerprint()
        (tmp_path / 'one' / 'a.txt').rename(tmp_path / 'two' / 'a.txt')
        assert TreeWorkspace(tmp_path).fingerprint() != before

    def test_a_same_LENGTH_edit_is_NOT_seen_and_the_docstring_says_so(self, tmp_path):
        """THE NAMED HOLE, asserted so it cannot be quietly forgotten or quietly fixed.

        This errs toward "unchanged", and that direction is DELIBERATE: `AgentTaskExecutor` turns
        "changed nothing" into INCONCLUSIVE, which is re-runnable. The opposite error -- reporting a
        change that did not happen -- would send an untouched tree to the verifier and let a green
        it did not earn become a PASS attributed to this task.
        """
        target = tmp_path / 'a.txt'
        target.write_text('aaaaa', encoding='utf-8')
        before = TreeWorkspace(tmp_path).fingerprint()
        target.write_text('bbbbb', encoding='utf-8')
        assert TreeWorkspace(tmp_path).fingerprint() == before, 'the hole closed; update the docstring'
        assert 'same-length' in (TreeWorkspace.__doc__ or ''), 'the limit must be stated where it is read'

    def test_it_satisfies_the_protocol_it_was_written_for(self, tmp_path):
        assert isinstance(TreeWorkspace(tmp_path), Workspace)


def test_neither_adapter_can_be_built_without_the_caller_naming_the_thing():
    """THE PROJECT-NEUTRALITY PROPERTY, as a constructor signature rather than a hope.

    These are the two places in the package most likely to acquire a default: a verifier wants to be
    `gate.py` and a workspace wants to be the repo root. A default in either would be
    `DEFAULT_REPO` under a new spelling -- a vendor-neutral layer holding one project's fact,
    invisible exactly because the default works. `test_this_package_names_no_specific_project`
    catches the NOUN; this catches the shape that would make a noun tempting.
    """
    with pytest.raises(TypeError):
        CommandVerifier()  # type: ignore[call-arg] -- argv has no default
    with pytest.raises(TypeError):
        TreeWorkspace()  # type: ignore[call-arg] -- root has no default
