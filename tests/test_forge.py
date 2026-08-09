"""The forge seam: storage and UI, one implementation per vendor, no decisions anywhere.

WHAT THIS FILE IS FOR. `test_forge_store.py` proves the LOGIC is right. This one proves the SEAM is
real -- that a second vendor can be dropped in without touching the store, and that the vendor we
have not measured says so at the call instead of pretending.

THE UNIMPLEMENTED BACKEND IS TESTED HARDER THAN THE IMPLEMENTED ONE, which is the right way round.
The claim protocol's soundness rests on a property of the DEPLOYMENT -- read-after-write consistency
on the comment list, plus server-assigned monotonic ids -- and we measured that on one single-node
Gitea. A GitHub client written from documentation would inherit none of that evidence and would fail
rarely, silently, and only under load. So the tests below pin the refusal: every protocol method
raises, the message names the experiments, and adding a method to `Forge` without implementing it on
GitHub cannot slip through.
"""

from __future__ import annotations

import pytest

from agent_swarm.forge import (
    GITHUB_UNMEASURED,
    Comment,
    Forge,
    GiteaForge,
    GitHubForge,
    WorkItem,
    default_forge,
)

#: Every method the store may call. Derived from the protocol rather than typed out, so a method
#: added to `Forge` tomorrow is covered by these tests today.
FORGE_METHODS = sorted(name for name in Forge.__protocol_attrs__ if not name.startswith('_'))

#: How many POSITIONAL arguments each protocol method takes. Indexed by the parametrised refusal
#: tests below, so a method added to `Forge` and forgotten here raises KeyError -- loudly -- rather
#: than silently going untested.
ARITY = {
    'list_work_items': 0,
    'create_work_item': 0,  # keyword-only
    'add_comment': 2,
    'comments': 1,
    'delete_comment': 2,
    'labels': 1,
    'add_label': 2,
    'remove_label': 2,
    'close_work_item': 1,
    'state': 1,
    'retire_work_item': 1,
}


def _call(forge, method: str):
    kwargs = {'title': 't', 'body': 'b'} if method == 'create_work_item' else {}
    return getattr(forge, method)(*[1] * ARITY[method], **kwargs)


class TestTheSeamCoversWhatTheStoreNeeds:
    def test_the_protocol_is_not_empty(self):
        assert FORGE_METHODS

    def test_the_arity_table_covers_exactly_the_protocol(self):
        assert sorted(ARITY) == FORGE_METHODS

    def test_gitea_satisfies_it(self):
        assert isinstance(GiteaForge('http://127.0.0.1:1', 'o/r'), Forge)

    def test_github_satisfies_it_STRUCTURALLY(self):
        """It must be substitutable at the type level even though every call refuses. A backend
        that could not even be constructed would hide the gap instead of naming it.
        """
        assert isinstance(GitHubForge('o/r'), Forge)

    def test_the_default_forge_is_a_forge(self):
        assert isinstance(default_forge(), Forge)

    def test_no_REF_operation_survives_anywhere_in_the_seam(self):
        """Refs are abandoned entirely (user directive), so the seam must not offer one -- not even
        a URL to push at. A leftover git method is how a deleted mechanism comes back.
        """
        assert not any('ref' in name or 'git' in name or 'push' in name for name in FORGE_METHODS)

    def test_the_forge_is_not_asked_to_ARBITRATE(self):
        """Who wins a contested claim is decided once, in the store, for every vendor. A `claim`
        method here would be a per-vendor correctness argument -- two of them, only one ever raced.
        """
        assert not any('claim' in name or 'winner' in name for name in FORGE_METHODS)

    def test_a_comment_carries_the_SERVER_id(self):
        """The id is the claim protocol's whole ordering key. A `Comment` without one would leave
        the store arbitrating on list POSITION, which any pagination change silently reorders.
        """
        assert Comment(id=595, body='CLAIM 1 a').id == 595

    def test_posting_a_comment_RETURNS_the_id(self):
        """Not `None`. A runner must learn its own key at insert; re-reading the list to guess which
        comment was its own is exactly the ambiguity the id removes.
        """
        assert Forge.add_comment.__annotations__['return'] == 'int'

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
        assert forge.repo == 'o/r'
        assert forge.base_url == 'http://127.0.0.1:1'

    def test_a_trailing_slash_does_not_produce_a_double_slash_url(self):
        assert GiteaForge('http://host:9000/', 'o/r').base_url == 'http://host:9000'


class TestGitHubRefusesInsteadOfGuessing:
    @pytest.mark.parametrize('method', FORGE_METHODS)
    def test_every_method_raises_NotImplementedError(self, method):
        with pytest.raises(NotImplementedError):
            _call(GitHubForge('o/r'), method)

    @pytest.mark.parametrize('method', FORGE_METHODS)
    def test_the_refusal_NAMES_the_method_and_the_experiments(self, method):
        """ "Not implemented" alone sends the reader to guess what is missing. The message has to
        carry the list, because the list IS the work.
        """
        with pytest.raises(NotImplementedError) as caught:
            _call(GitHubForge('o/r'), method)
        message = str(caught.value)
        assert method in message
        assert 'measured' in message
        assert GITHUB_UNMEASURED[0] in message

    def test_the_FIRST_experiment_is_the_four_round_race(self):
        """Ordering is not cosmetic here: the first entry is what a reader implements against, and
        the blocking unknown is the claim protocol itself, not a pagination limit.
        """
        first = GITHUB_UNMEASURED[0].lower()
        assert 'four' in first
        assert 'barrier' in first
        assert 'one winner' in first

    def test_read_after_write_is_named_as_the_PRECONDITION(self):
        """The protocol is sound only where a lower-id comment is visible to a later reader. That is
        a property of the deployment, and GitHub's is unmeasured -- so it must be in the list, not
        merely in someone's memory.
        """
        joined = ' '.join(GITHUB_UNMEASURED).lower()
        assert 'read-after-write' in joined
        assert 'unverified' in joined or 'unmeasured' in joined

    def test_the_unmeasured_list_names_the_things_that_ACTUALLY_surprised_us(self):
        """Not a generic to-do. Each entry is one experiment, and every one corresponds to a Gitea
        behaviour that was wrong when guessed.
        """
        joined = ' '.join(GITHUB_UNMEASURED).lower()
        for topic in ('comment id', 'monotonic', 'label', 'pagination', 'search', 'delet', 'rate limit'):
            assert topic in joined, f'no experiment listed for {topic!r}'
