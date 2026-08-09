"""The forge seam: storage and UI, one implementation per vendor, no decisions anywhere.

WHAT THIS FILE IS FOR. `test_forge_store.py` proves the LOGIC is right. This one proves the SEAM is
real -- that a second vendor can be dropped in without touching the store, and that the vendor we
have not measured says so at the call instead of pretending.

THE UNIMPLEMENTED BACKEND IS TESTED HARDER THAN THE IMPLEMENTED ONE, which is the right way round.
Every Gitea behaviour this package relies on was a SURPRISE when raced -- the assignee is not a CAS,
duplicate labels are accepted, `POST /git/refs` is 405, `?q=` misses issues that exist. A GitHub
client written from documentation would be four more guesses wearing the same interface. So the
tests below pin the refusal: every protocol method raises, the message names the experiment, and
adding a method to `Forge` without implementing it on GitHub cannot slip through.
"""

from __future__ import annotations

import pytest

from agent_swarm.forge import (
    GITHUB_UNMEASURED,
    Forge,
    GiteaForge,
    GitHubForge,
    WorkItem,
    default_forge,
)

#: Every method the store may call. Derived from the protocol rather than typed out, so a method
#: added to `Forge` tomorrow is covered by these tests today.
FORGE_METHODS = sorted(name for name in Forge.__protocol_attrs__ if not name.startswith('_'))


class TestTheSeamCoversWhatTheStoreNeeds:
    def test_the_protocol_is_not_empty(self):
        assert FORGE_METHODS

    def test_gitea_satisfies_it(self):
        assert isinstance(GiteaForge('http://127.0.0.1:1', 'o/r'), Forge)

    def test_github_satisfies_it_STRUCTURALLY(self):
        """It must be substitutable at the type level even though every call refuses. A backend
        that could not even be constructed would hide the gap instead of naming it.
        """
        assert isinstance(GitHubForge('o/r'), Forge)

    def test_the_default_forge_is_a_forge(self):
        assert isinstance(default_forge(), Forge)

    def test_neither_client_is_asked_to_CLAIM(self):
        """The compare-and-swap is a git ref push, the one atomic primitive both vendors share, so
        it must not appear in the vendor seam at all. A `try_claim` here would be a per-vendor
        correctness argument -- two of them, only one ever raced.
        """
        assert 'try_claim' not in FORGE_METHODS
        assert not any('claim' in name for name in FORGE_METHODS)

    def test_a_work_item_states_only_vendor_NEUTRAL_facts(self):
        item = WorkItem(number=1, title='t', state='open')
        assert (item.number, item.title, item.state) == (1, 't', 'open')


class TestConstructionTouchesNothing:
    def test_building_a_client_performs_no_io(self):
        """A store built against an unreachable host must still refuse a bad verdict word, and that
        refusal has to be about the WORD. A constructor that connected, or read a credential, would
        make the failure about the network instead -- and on a good day would hide the bug entirely.
        """
        forge = GiteaForge('http://127.0.0.1:1', 'o/r')
        assert forge.git_url() == 'http://127.0.0.1:1/o/r.git'

    def test_the_git_url_carries_NO_credential(self):
        """A URL with a secret in it is written into `.git/config` and echoed by every git trace.
        The credential helper supplies the token instead, per request.
        """
        url = GiteaForge('http://host:9000', 'o/r').git_url()
        assert '@' not in url
        assert 'token' not in url


class TestGitHubRefusesInsteadOfGuessing:
    @pytest.mark.parametrize('method', FORGE_METHODS)
    def test_every_method_raises_NotImplementedError(self, method):
        forge = GitHubForge('o/r')
        with pytest.raises(NotImplementedError):
            # Positional junk is fine: nothing may reach a body that could use it.
            getattr(forge, method)(
                *[1] * _arity(method), **({'title': 't', 'body': 'b'} if method == 'create_work_item' else {})
            )

    @pytest.mark.parametrize('method', FORGE_METHODS)
    def test_the_refusal_NAMES_the_method_and_the_experiments(self, method):
        """ "Not implemented" alone sends the reader to guess what is missing. The message has to
        carry the list, because the list IS the work.
        """
        forge = GitHubForge('o/r')
        with pytest.raises(NotImplementedError) as caught:
            getattr(forge, method)(
                *[1] * _arity(method), **({'title': 't', 'body': 'b'} if method == 'create_work_item' else {})
            )
        message = str(caught.value)
        assert method in message
        assert 'measured' in message
        assert GITHUB_UNMEASURED[0] in message

    def test_the_unmeasured_list_names_the_things_that_ACTUALLY_surprised_us(self):
        """Not a generic to-do. Each entry is one experiment, and every one of them corresponds to
        a Gitea behaviour that was wrong when guessed.
        """
        joined = ' '.join(GITHUB_UNMEASURED).lower()
        for topic in ('label', 'pagination', 'search', 'delet', 'rate limit'):
            assert topic in joined, f'no experiment listed for {topic!r}'


def _arity(method: str) -> int:
    """How many positional arguments a protocol method takes, excluding keyword-only ones."""
    return {
        'git_url': 0,
        'list_work_items': 0,
        'create_work_item': 0,
        'add_comment': 2,
        'comments': 1,
        'labels': 1,
        'add_label': 2,
        'remove_label': 2,
        'close_work_item': 1,
        'state': 1,
        'retire_work_item': 1,
    }[method]


class TestTheArityTableIsNotAllowedToDrift:
    def test_it_covers_exactly_the_protocol(self):
        """The parametrised refusal tests index this table. A method added to `Forge` and forgotten
        here would raise KeyError -- loudly -- rather than silently going untested.
        """
        for method in FORGE_METHODS:
            assert isinstance(_arity(method), int)
