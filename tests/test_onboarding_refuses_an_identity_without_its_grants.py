"""An identity is verified the SECOND it is created, not months later on somebody else's machine.

USER DIRECTIVE 2026-08-11: 「onboarding 时验证,而不是安装时发现」-- verify at onboarding rather
than discover at install time. "Which repositories must this identity be able to read" is DATA, and
it was nothing at all.

WHAT THAT COST, measured. A role account was minted with read on one repository and no grant on
another. Nothing checked, so it looked healthy: `verify` printed teams, membership, attachment and
units, all correct. The absence surfaced MONTHS LATER, on a DIFFERENT machine, as
`uv pip install -e .[all,dev]` failing in dependency RESOLUTION with a 404 buried in it -- because a
`git+http://` dependency authenticated as that account and Gitea answers 404, not 403, for a private
repository the caller may not see. Four days and two machines between cause and symptom.

THE INSTALL-SIDE PRE-FLIGHT CATCHES IT AT THE FAR END. This catches it at the NEAR one -- the second
where it is cheap, and where the person who can grant the access is already at a keyboard.

THE PROPERTY THAT MUST NOT BE LOST is the three-valued one. A forge that is down produces the same
non-success as a missing grant, and an onboarding that fails closed on transience is one an operator
learns to bypass. `refused` and `unanswerable` are separate buckets and only the first fails a run.
"""

from __future__ import annotations

import argparse

import pytest

from agent_swarm import credentials, swarmctl


class _Forge:
    """A scripted `_call`, keyed by repo path. Records what was asked.

    `refused` returns None the way `GiteaProvider._call` does for an ALLOWED status -- which is how
    a 401/403/404 arrives -- and `down` RAISES, the way it does when the host is unreachable. A
    double that returned a falsy value for both would erase the very distinction under test.
    """

    def __init__(self, *, refused: tuple[str, ...] = (), down: tuple[str, ...] = ()) -> None:
        self.refused, self.down = refused, down
        self.asked: list[str] = []

    def _call(self, _method, path, _body=None, *, allow=(), auth='token', raw_token=None):
        self.asked.append(path)
        assert auth == 'raw' and raw_token, 'the probe must use the CREDENTIAL under test, not the run token'
        assert 404 in allow, 'Gitea hides an unreadable repo behind 404; it must be an allowed refusal'
        if any(name in path for name in self.down):
            msg = 'unreachable'
            raise swarmctl.Fail(msg)
        return None if any(name in path for name in self.refused) else {'full_name': path}


class TestTheProbeSeparatesNoGrantFromNoForge:
    def test_a_readable_repository_answers_true(self) -> None:
        assert credentials.probe_readable('Org', 'ok', 'tok', call=_Forge()._call) is True

    def test_a_refusal_is_FALSE_and_a_404_counts_as_one(self) -> None:
        """Gitea answers 404 -- deliberate information hiding -- for a private repo the caller may
        not see. Reading that as "absent, therefore not our problem" is what made the original
        defect invisible from the client.
        """
        assert credentials.probe_readable('Org', 'hidden', 'tok', call=_Forge(refused=('hidden',))._call) is False

    def test_an_unreachable_forge_is_NONE_and_never_FALSE(self) -> None:
        """THE DISCRIMINATING ASSERTION. Collapsing this into False turns a network hiccup into an
        accusation about somebody's permissions, and sends an operator to an administrator who
        cannot help them.
        """
        assert credentials.probe_readable('Org', 'x', 'tok', call=_Forge(down=('x',))._call) is None


class TestTheTwoBucketsStaySeparate:
    def test_a_missing_grant_lands_in_refused(self) -> None:
        refused, unanswerable = credentials.unreadable_repositories(
            ['Org/ok', 'Org/hidden'], 'tok', call=_Forge(refused=('hidden',))._call
        )
        assert refused == ['Org/hidden'] and unanswerable == []

    def test_a_down_forge_lands_in_unanswerable_and_NOTHING_is_refused(self) -> None:
        refused, unanswerable = credentials.unreadable_repositories(
            ['Org/a', 'Org/b'], 'tok', call=_Forge(down=('Org/',))._call
        )
        assert refused == [] and unanswerable == ['Org/a', 'Org/b']

    def test_a_refusal_is_self_controlling_when_a_sibling_is_merely_unreachable(self) -> None:
        """A `False` PROVES the transport worked -- the server answered -- so a refusal needs no
        separate control probe. This pins that reasoning: a run where one repo is refused and
        another is unreachable reports exactly one of each, rather than merging them either way.
        """
        refused, unanswerable = credentials.unreadable_repositories(
            ['Org/hidden', 'Org/flaky'], 'tok', call=_Forge(refused=('hidden',), down=('flaky',))._call
        )
        assert refused == ['Org/hidden'] and unanswerable == ['Org/flaky']

    def test_a_malformed_requirement_raises_rather_than_being_skipped(self) -> None:
        with pytest.raises(ValueError, match='OWNER/NAME'):
            credentials.unreadable_repositories(['not-a-repo'], 'tok', call=_Forge()._call)


class TestOnboardingRefuses:
    def test_enroll_FAILS_when_an_identity_cannot_read_a_required_repository(self, monkeypatch, capsys) -> None:
        """THE DISCRIMINATING ASSERTION of item 2: the run does not complete."""
        forge = _Forge(refused=('hidden',))
        with pytest.raises(swarmctl.Fail, match='WITHOUT the read access'):
            swarmctl._refuse_identities_without_grants(forge, {'swarm-agent': 'tok'}, ['Org/hidden'])
        assert 'FAIL' in capsys.readouterr().out

    def test_an_unreachable_forge_does_NOT_fail_the_run(self, monkeypatch, capsys) -> None:
        """THE OTHER DISCRIMINATING ASSERTION, and the one a careless check fails. Onboarding must
        not refuse because the network hiccuped -- it reports and continues.
        """
        forge = _Forge(down=('Org/',))
        swarmctl._refuse_identities_without_grants(forge, {'swarm-agent': 'tok'}, ['Org/thing'])
        out = capsys.readouterr().out
        assert 'not answered' in out and 'FAIL' not in out

    def test_the_refusal_names_the_identity_the_repo_and_that_the_token_is_KEPT(self, monkeypatch) -> None:
        """A token exists in plaintext exactly once -- Gitea keeps only a hash -- and its name
        cannot be reused. Discarding one to signal a fixable permissions problem would destroy
        something unrecoverable, so the message must say the credential survived.
        """
        forge = _Forge(refused=('hidden',))
        with pytest.raises(swarmctl.Fail) as caught:
            swarmctl._refuse_identities_without_grants(forge, {'swarm-agent': 'tok'}, ['Org/hidden'])
        message = str(caught.value)
        assert 'swarm-agent' in message and 'Org/hidden' in message
        assert 'ARE stored' in message and 'tok' not in message.split('sha256')[0].replace('token', '')

    def test_nothing_is_probed_when_nothing_is_required(self) -> None:
        """Cheapness AND honesty: with no declared requirement there is no claim to check, and a
        check that silently probes something it invented would be a bar the caller never set.
        """
        forge = _Forge()
        swarmctl._refuse_identities_without_grants(forge, {'swarm-agent': 'tok'}, [])
        assert forge.asked == []

    def test_every_issued_identity_is_checked_not_merely_the_first(self) -> None:
        forge = _Forge()
        issued = {f'swarm-{r}': f'tok-{r}' for r in ('agent', 'verifier', 'observer', 'integrator')}
        swarmctl._refuse_identities_without_grants(forge, issued, ['Org/thing'])
        assert len(forge.asked) == 4, 'a check that stops at the first identity certifies the other three'


class TestTheRequiredListIsTheCallersData:
    def _args(self, **kwargs) -> argparse.Namespace:
        base = {'require_repo': None, 'required_repos': '', 'repo': ''}
        return argparse.Namespace(**{**base, **kwargs})

    def test_a_repeatable_flag_is_the_most_explicit_source(self) -> None:
        assert swarmctl.required_repositories(self._args(require_repo=['A/b', 'C/d'])) == ['A/b', 'C/d']

    def test_the_config_or_environment_supplies_a_comma_separated_list(self) -> None:
        assert swarmctl.required_repositories(self._args(required_repos='A/b, C/d')) == ['A/b', 'C/d']

    def test_it_FALLS_BACK_to_the_repo_being_onboarded_rather_than_to_nothing(self) -> None:
        """A default of "check nothing" makes the guard opt-in, and an opt-in guard on a fleet is a
        guard nobody has turned on. The repository already named on the command line is the minimum
        honest requirement and needs no configuration.
        """
        assert swarmctl.required_repositories(self._args(repo='Org/thing')) == ['Org/thing']

    def test_it_names_NO_specific_project(self) -> None:
        """This package onboards machines for any project. A built-in list would be `DEFAULT_REPO`
        under a new spelling -- a vendor-neutral layer holding one project's fact, invisible exactly
        because the default works.
        """
        assert swarmctl.required_repositories(self._args()) == []
