"""`submit` -- the verb without which the integration plane has a queue and no producers.

THE GAP THIS CLOSES. `swarmctl`'s workbench was `list` / `take` / `report`: three verbs that all
CONSUME. `submission.create` and the whole queue existed as Python objects no participant could
reach, which for going live is indistinguishable from not having built them.

TWO PROPERTIES ARE ASSERTED HARDER THAN THE REST.

* **IT IS ONE VERB FOR ALL THREE PARTICIPANT KINDS.** A person at a terminal, a human-controlled
  agent and a controller's subagent run the same command through the same entry point, and nothing
  in it reads who is calling. `TestOneVerbForEveryParticipantKind` plants all three and asserts the
  results are identical but for the name that was passed.
* **A DEVIATION IS ACCEPTED AND RECORDED, NEVER REFUSED.** A test asserting refusal would build the
  path lock the whole model argues against, so the test here asserts the opposite: the submission
  LANDS, and the deviation is reported in both directions. Git detects real collisions exactly, at
  merge time; a declaration is intent and routing.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agent_swarm.evidence import Effects
from agent_swarm.submission import effects_of, observed_paths, read, submitted_ordinals
from agent_swarm.workbench_cli import Exit, main

TRUNK = 'trunk'


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, check=True, timeout=60)
    return out.stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A real checkout with a real remote -- `submit` pushes a ref and nothing here is faked."""
    bare = tmp_path / 'remote.git'
    subprocess.run(['git', 'init', '--bare', '-q', str(bare)], check=True, timeout=60)
    work = tmp_path / 'work'
    subprocess.run(['git', 'init', '-q', '-b', TRUNK, str(work)], check=True, timeout=60)
    _git(work, 'config', 'user.email', 'x@example.com')
    _git(work, 'config', 'user.name', 'x')
    (work / 'base.txt').write_text('base\n', encoding='utf-8')
    _git(work, 'add', '-A')
    _git(work, 'commit', '-qm', 'trunk base')
    _git(work, 'remote', 'add', 'origin', str(bare))
    return work


def _branch(root: Path, name: str, files: dict[str, str]) -> str:
    _git(root, 'checkout', '-q', '-b', name, TRUNK)
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
    _git(root, 'add', '-A')
    _git(root, 'commit', '-qm', f'work on {name}')
    head = _git(root, 'rev-parse', 'HEAD')
    _git(root, 'checkout', '-q', TRUNK)
    return head


def _submit(checkout: Path, capsys, *extra: str, owner: str = 'someone', intent: str = 'do the thing') -> dict:
    """Run the REAL entry point -- argument parsing, dispatch and exit code included."""
    code = main(
        [
            '--owner',
            owner,
            'submit',
            '--base',
            TRUNK,
            '--intent',
            intent,
            '--git-root',
            str(checkout),
            '--git-remote',
            'origin',
            '--json',
            *extra,
        ]
    )
    assert code == Exit.OK, capsys.readouterr().err
    return json.loads(capsys.readouterr().out)


class TestTheVerbExistsAndPublishes:
    def test_a_submission_really_lands_on_the_remote(self, checkout, capsys):
        _branch(checkout, 'lane-a', {'a.txt': 'a\n'})
        got = _submit(checkout, capsys, '--head', 'lane-a')

        assert got['ordinal'] == 1
        assert submitted_ordinals(_store(checkout)) == (1,)
        landed = read(_store(checkout), 1)
        assert landed.intent == 'do the thing'
        assert landed.head == _git(checkout, 'rev-parse', 'lane-a')

    def test_a_second_submission_takes_the_next_ordinal(self, checkout, capsys):
        _branch(checkout, 'lane-a', {'a.txt': 'a\n'})
        _branch(checkout, 'lane-b', {'b.txt': 'b\n'})
        assert _submit(checkout, capsys, '--head', 'lane-a')['ordinal'] == 1
        assert _submit(checkout, capsys, '--head', 'lane-b')['ordinal'] == 2

    def test_the_revisions_are_RESOLVED_so_a_branch_that_moves_does_not_move_the_submission(self, checkout, capsys):
        """A branch NAME is a mutable pointer. A submission naming one would describe different code
        an hour later, and the queue would merge something nobody proposed."""
        head = _branch(checkout, 'lane-a', {'a.txt': 'a\n'})
        got = _submit(checkout, capsys, '--head', 'lane-a')
        _git(checkout, 'checkout', '-q', 'lane-a')
        _git(checkout, 'commit', '-q', '--allow-empty', '-m', 'more work after submitting')
        _git(checkout, 'checkout', '-q', TRUNK)

        assert got['head'] == head
        assert read(_store(checkout), 1).head == head

    def test_a_head_that_is_not_a_commit_is_refused_rather_than_published(self, checkout, capsys):
        """`git rev-parse` echoes an unknown argument back and exits 0, so the naive spelling would
        publish a plausible-looking string that resolves to nothing -- and the failure would surface
        days later, inside a merge, as `HeadNotPresent`."""
        code = main(
            [
                '--owner',
                'x',
                'submit',
                '--base',
                TRUNK,
                '--head',
                'no-such-branch',
                '--intent',
                'i',
                '--git-root',
                str(checkout),
                '--git-remote',
                'origin',
            ]
        )
        assert code == Exit.NOT_SUBMITTED
        assert 'no-such-branch' in capsys.readouterr().err
        assert submitted_ordinals(_store(checkout)) == (), 'a nonexistent head was published anyway'


class TestOneVerbForEveryParticipantKind:
    def test_the_three_kinds_use_the_SAME_surface_and_are_told_apart_only_by_name(self, checkout, capsys):
        """AN EXECUTOR IS A KIND, NOT A LAYER. There is deliberately no human API: a second entry
        point for people would be a second protocol to keep in step with this one, and the one that
        drifted would be the one nobody was running."""
        results = {}
        for participant in ('ada@workstation', 'agent-session-7', 'controller-3/subagent-2'):
            branch = participant.replace('/', '-').replace('@', '-')
            _branch(checkout, branch, {f'{branch}.txt': 'x\n'})
            results[participant] = _submit(checkout, capsys, '--head', branch, owner=participant)

        assert [r['participant'] for r in results.values()] == list(results)
        assert sorted(r['ordinal'] for r in results.values()) == [1, 2, 3]
        # Every other field of the protocol is identical in shape -- no kind took a different path.
        assert {frozenset(r) for r in results.values()} == {frozenset(next(iter(results.values())))}

    def test_it_needs_no_forge_credentials(self, checkout, capsys):
        """`--repo` and `--namespace` build a `Workbench` against the Gitea API, which this verb never
        touches. Demanding them would make the producer verb unusable in exactly the place it is most
        useful -- a lane with a checkout and no credentials."""
        _branch(checkout, 'lane-a', {'a.txt': 'a\n'})
        assert _submit(checkout, capsys, '--head', 'lane-a')['ordinal'] == 1


class TestADeviationIsACCEPTEDAndRECORDED:
    def test_observed_effects_BEYOND_the_declaration_do_not_stop_the_submission(self, checkout, capsys):
        """**THE PROPERTY THE DESIGN ARGUES FOR, so it is asserted in the accepting direction.** A
        refusal here would be a path lock wearing an organisational hat."""
        _branch(checkout, 'lane-a', {'declared.txt': 'a\n', 'surprise.txt': 'b\n'})

        got = _submit(checkout, capsys, '--head', 'lane-a', '--path', 'declared.txt')

        assert got['ordinal'] == 1, 'the submission was refused for exceeding its declaration'
        assert got['undeclared'] == ['surprise.txt']
        assert got['deviates'] is True
        assert submitted_ordinals(_store(checkout)) == (1,)

    def test_the_UNREALISED_direction_is_reported_too(self, checkout, capsys):
        """A report loud about the harmless direction and silent about the harmful one is the shape
        this repo names: a declared file that never changed is the likelier sign a step was skipped."""
        _branch(checkout, 'lane-a', {'declared.txt': 'a\n'})

        got = _submit(checkout, capsys, '--head', 'lane-a', '--path', 'declared.txt', '--path', 'promised.txt')

        assert got['unrealised'] == ['promised.txt']
        assert got['undeclared'] == []
        assert got['deviates'] is True

    def test_the_deviation_is_reported_in_the_HUMAN_output_as_well_as_the_json(self, checkout, capsys):
        _branch(checkout, 'lane-a', {'declared.txt': 'a\n', 'surprise.txt': 'b\n'})
        code = main(
            [
                '--owner',
                'x',
                'submit',
                '--base',
                TRUNK,
                '--head',
                'lane-a',
                '--intent',
                'i',
                '--path',
                'declared.txt',
                '--git-root',
                str(checkout),
                '--git-remote',
                'origin',
            ]
        )
        out = capsys.readouterr().out
        assert code == Exit.OK
        assert 'ACCEPTED' in out and 'surprise.txt' in out

    def test_the_record_is_the_EXISTING_declared_versus_observed_field(self, checkout, capsys):
        """WIRED INTO `evidence.Effects`, not into a second record beside it. The same object a
        verdict's evidence carries is the one that answers this question at submit time."""
        _branch(checkout, 'lane-a', {'declared.txt': 'a\n', 'surprise.txt': 'b\n'})
        _submit(checkout, capsys, '--head', 'lane-a', '--path', 'declared.txt')

        effects = effects_of(_store(checkout), read(_store(checkout), 1))

        assert isinstance(effects, Effects)
        assert effects.declared == ('declared.txt',)
        assert effects.observed == ('declared.txt', 'surprise.txt')
        assert effects.undeclared == ('surprise.txt',)

    def test_the_record_is_DERIVED_so_it_cannot_go_stale_against_the_commits(self, checkout, capsys):
        """The published submission carries `base`, `head` and `declared_paths`, so any later reader
        recomputes this exactly. A snapshot written at submit time would be a number that drifts."""
        _branch(checkout, 'lane-a', {'a.txt': 'a\n'})
        _submit(checkout, capsys, '--head', 'lane-a', '--path', 'a.txt')
        submission = read(_store(checkout), 1)

        assert effects_of(_store(checkout), submission) == effects_of(_store(checkout), submission)
        assert not effects_of(_store(checkout), submission).deviates


class TestTheObservedSetIsTheParticipantsOwnWork:
    def test_a_trunk_that_moved_on_does_not_become_this_participants_effects(self, checkout):
        """THREE DOTS, NOT TWO. Two would fold everyone else's commits into the observed set, and the
        deviation report would then be loud about work the submitter never did."""
        head = _branch(checkout, 'lane-a', {'mine.txt': 'a\n'})
        (checkout / 'somebody-elses.txt').write_text('x\n', encoding='utf-8')
        _git(checkout, 'add', '-A')
        _git(checkout, 'commit', '-qm', 'the trunk moved on')

        assert observed_paths(_store(checkout), TRUNK, head) == ('mine.txt',)

    def test_an_unreadable_diff_RAISES_rather_than_reading_as_an_empty_one(self, checkout):
        """ "The diff is empty" and "I could not take the diff" are opposite facts."""
        from agent_swarm.refstore import RefUnreachable

        with pytest.raises(RefUnreachable):
            observed_paths(_store(checkout), TRUNK, '0' * 40)


def _store(root: Path):
    from agent_swarm.refstore import GitRefStore, ambient_identity

    return GitRefStore(root, 'origin', withhold_writes=lambda: False, identity=ambient_identity)
