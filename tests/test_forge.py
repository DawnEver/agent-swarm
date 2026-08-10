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
    DEFAULT_GITEA_BASE_URL,
    GITHUB_CONFIRMED,
    GITHUB_DIVERGENCES,
    GITHUB_UNMEASURED,
    Comment,
    Forge,
    ForgeError,
    GiteaForge,
    GitHubForge,
    WorkItem,
    default_forge,
)
from conftest import LIVE_REPO

#: Every method the store may call. Derived from the protocol rather than typed out, so a method
#: added to `Forge` tomorrow is covered by these tests today.
FORGE_METHODS = sorted(name for name in Forge.__protocol_attrs__ if not name.startswith('_'))

#: How many POSITIONAL arguments each protocol method takes. Indexed by the parametrised refusal
#: tests below, so a method added to `Forge` and forgotten here raises KeyError -- loudly -- rather
#: than silently going untested.
ARITY = {
    'list_work_items': 0,
    'work_item': 1,
    'create_work_item': 0,  # keyword-only
    'add_comment': 2,
    'update_comment': 3,
    'comments': 1,
    'delete_comment': 2,
    'labels': 1,
    'add_label': 2,
    'remove_label': 2,
    'close_work_item': 1,
    'state': 1,
    'retire_work_item': 1,
    'set_status': 1,  # sha positional; state/context/description keyword-only
}


#: Keyword-only arguments per method. Tabulated rather than special-cased in `_call`, so a new
#: method with keyword-only arguments is a table entry and not a branch nobody remembers to add.
KWARGS = {
    'create_work_item': {'title': 't', 'body': 'b'},
    'set_status': {'state': 'success', 'context': 'c', 'description': 'd'},
}


def _call(forge, method: str):
    kwargs = KWARGS.get(method, {})
    return getattr(forge, method)(*[1] * ARITY[method], **kwargs)


class TestTheSeamCoversWhatTheStoreNeeds:
    def test_the_protocol_is_not_empty(self):
        assert FORGE_METHODS

    def test_the_arity_table_covers_exactly_the_protocol(self):
        assert sorted(ARITY) == FORGE_METHODS

    def test_gitea_satisfies_it(self):
        assert isinstance(GiteaForge('http://127.0.0.1:1', 'o/r', username='swarm-agent'), Forge)

    def test_github_satisfies_it_STRUCTURALLY(self):
        """It must be substitutable at the type level even though every call refuses. A backend
        that could not even be constructed would hide the gap instead of naming it.
        """
        assert isinstance(GitHubForge('o/r'), Forge)

    def test_the_default_forge_is_a_forge(self):
        assert isinstance(default_forge(repo=LIVE_REPO), Forge)

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

    def test_the_heartbeat_can_be_EDITED_in_place(self):
        """One comment per runner, edited -- not appended, and not a shared body.

        An APPENDED beat keeps advertising a capability that has been withdrawn: a vendor tool
        uninstalled between beats stays visible in the older comment forever, and the stream grows
        without bound against the 500-comment recycle limit. A SHARED body is worse -- a mutable
        slot on an API with no compare-and-swap, where at a hundred runners one beat erases another.
        """
        assert 'update_comment' in FORGE_METHODS

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
        forge = GiteaForge('http://127.0.0.1:1', 'o/r', username='swarm-agent')
        assert forge.repo == 'o/r'
        assert forge.base_url == 'http://127.0.0.1:1'

    def test_a_trailing_slash_does_not_produce_a_double_slash_url(self):
        assert GiteaForge('http://host:9000/', 'o/r', username='swarm-agent').base_url == 'http://host:9000'


class TestGitHubRefusesInsteadOfGuessing:
    """GitHub HAS now been probed, and the refusal survives the good news.

    The blocking unknown -- read-after-write on the comment list -- came back CLEAN: 4/4 rounds, 16
    racers, one winner each. The design is portable. What stops the client being written is the
    three measured DIVERGENCES, and the honest failure mode here would be to read "the protocol
    works on GitHub" as "the client can be written now".
    """

    @pytest.mark.parametrize('method', FORGE_METHODS)
    def test_every_method_raises_NotImplementedError(self, method):
        with pytest.raises(NotImplementedError):
            _call(GitHubForge('o/r'), method)

    @pytest.mark.parametrize('method', FORGE_METHODS)
    def test_the_refusal_names_the_method_the_DIFFERENCES_and_the_UNKNOWNS(self, method):
        """A refusal that said only "not implemented" sends the reader to guess what is missing.
        It has to carry both lists, because between them they ARE the work.
        """
        with pytest.raises(NotImplementedError) as caught:
            _call(GitHubForge('o/r'), method)
        message = str(caught.value)
        assert method in message
        assert GITHUB_DIVERGENCES[0] in message
        assert GITHUB_UNMEASURED[0] in message

    def test_the_refusal_does_not_claim_the_protocol_is_UNMEASURED(self, method='comments'):
        """It was measured and it passed. Saying otherwise would be the mirror-image lie -- a
        refusal justified by evidence that no longer exists, which the next reader would check once,
        find false, and then discount the rest of the message.
        """
        with pytest.raises(NotImplementedError) as caught:
            _call(GitHubForge('o/r'), method)
        assert 'passes' in str(caught.value)

    def test_what_SURVIVED_the_second_forge_is_recorded(self):
        """The portability claim is now evidence, not hope, and it must be findable from the code
        rather than only from a memory file.
        """
        joined = ' '.join(GITHUB_CONFIRMED).lower()
        assert '4 rounds' in joined
        assert 'one winner' in joined
        assert '64/64' in joined

    def test_the_INVERTED_freshness_is_named_as_a_divergence(self):
        """THE SHARPEST ONE. `?labels=` is exact on Gitea and stale up to 6.6 s on GitHub; text
        search is the opposite. So "key on labels, never on text" is a GITEA rule, and a
        vendor-neutral layer that adopted it would be wrong on the second forge -- silently, and
        only for a few seconds at a time, which is the worst duration for a bug to last.
        """
        joined = ' '.join(GITHUB_DIVERGENCES).lower()
        assert 'inverted' in joined
        assert '6.6 s' in joined
        assert 'no query path is fresh on both' in joined

    def test_the_only_read_fresh_on_BOTH_forges_is_named(self):
        """Because that is the one a machine decision may use. A divergence list that named the
        problem without naming the survivor would leave the reader to pick.
        """
        joined = ' '.join(GITHUB_DIVERGENCES).lower()
        assert 'get by issue number' in joined

    def test_pagination_and_the_write_rate_limit_are_named(self):
        """Both silently break something that works on Gitea: a truncated comment list is a claim
        protocol that cannot see a claim, and a 403 after ~24 creates is a fleet-size ceiling that
        has no Gitea equivalent.
        """
        joined = ' '.join(GITHUB_DIVERGENCES).lower()
        assert 'paginate' in joined
        assert 'link' in joined
        assert 'secondary rate limit' in joined
        assert '24 creates' in joined

    def test_the_FIRST_unmeasured_item_is_the_one_that_breaks_OUR_code(self):
        """Ordering is not cosmetic: the first entry is what a reader implements against. The
        blocking unknown is no longer the protocol -- it is that `_item_number` concludes "does not
        exist" from a LIST query, which the GitHub measurement explicitly forbids.
        """
        first = GITHUB_UNMEASURED[0].lower()
        assert 'list' in first
        assert 'does not exist' in first
        assert '_item_number' in first

    def test_the_unmeasured_list_still_names_the_open_experiments(self):
        joined = ' '.join(GITHUB_UNMEASURED).lower()
        for topic in ('pagination', 'rate', 'retire', 'project'):
            assert topic in joined, f'no experiment listed for {topic!r}'


class TestRemovingALabelDetachesEveryIdSharingItsName:
    """`GiteaForge.remove_label`, at the HTTP boundary, because nothing else covers it offline.

    THIS CLASS EXISTS BECAUSE THE MUTANT SURVIVED. The duplicate-verdict bug was found and fixed in
    both `GiteaForge` and `RecordingForge`, and the whole offline suite went red then green -- but
    reverting the **vendor** fix left all 431 tests passing, because every offline test runs against
    the double. Two implementations of one rule, one of them tested, and the untested one is the
    one that talks to the server.

    `_api` is stubbed rather than the network faked: the question here is precisely which requests
    are issued, so the request list IS the assertion. This does not test Gitea -- the `live_forge`
    tests do that -- it tests that we ask Gitea for the right thing.
    """

    def _forge_with_labels(self, monkeypatch, defined: list[dict]):
        forge = GiteaForge('http://127.0.0.1:1', 'o/r', username='swarm-agent')
        calls: list[tuple[str, str]] = []

        def fake_api(method: str, path: str, body: dict | None = None):
            calls.append((method, path))
            if method == 'GET' and path.startswith('/repos/o/r/labels'):
                return defined
            return None

        monkeypatch.setattr(forge, '_api', fake_api)
        return forge, calls

    def test_BOTH_ids_are_deleted_when_a_name_is_duplicated(self, monkeypatch):
        """The measured state: `POST /labels` accepted twelve identical names from twelve racers."""
        forge, calls = self._forge_with_labels(
            monkeypatch, [{'id': 7, 'name': 'verdict:pass'}, {'id': 9, 'name': 'verdict:pass'}]
        )

        forge.remove_label(3, 'verdict:pass')

        deletes = [path for method, path in calls if method == 'DELETE']
        assert deletes == ['/repos/o/r/issues/3/labels/7', '/repos/o/r/issues/3/labels/9'], (
            'only one id was detached -- a same-named label survives and the item carries two verdicts'
        )

    def test_an_UNRELATED_name_is_left_alone(self, monkeypatch):
        """The control: deleting every id in the repo would also pass the test above."""
        forge, calls = self._forge_with_labels(
            monkeypatch, [{'id': 7, 'name': 'verdict:pass'}, {'id': 8, 'name': 'verdict:fail'}]
        )

        forge.remove_label(3, 'verdict:pass')

        assert [p for m, p in calls if m == 'DELETE'] == ['/repos/o/r/issues/3/labels/7']

    def test_removing_an_ABSENT_label_creates_nothing(self, monkeypatch):
        """`_label_id` CREATES a missing label; removal must not borrow that. A removal that created
        the thing it was asked to remove is a write on a read-only intent -- and it would leave a
        fresh unused label behind on every no-op cleanup."""
        forge, calls = self._forge_with_labels(monkeypatch, [{'id': 7, 'name': 'verdict:pass'}])

        forge.remove_label(3, 'verdict:inconclusive')

        assert [m for m, _p in calls if m == 'POST'] == []
        assert [p for m, p in calls if m == 'DELETE'] == []


# ---------------------------------------------------------------------------
# The base URL is operator input and reaches urllib, which honours `file:`.


@pytest.mark.parametrize(
    'bad',
    ['file:///etc/passwd', 'ftp://host/x', 'gopher://host/1', 'host:9000', 'http://', '', 'data:,x'],
)
def test_a_non_http_base_url_is_refused_where_it_enters(bad: str) -> None:
    """`urllib.request` dispatches on the SCHEME. A `file:` base URL would turn every API call into
    a local read that still looks like a forge answering -- work items, claims and verdicts all
    fabricated from disk, with the retry loop dutifully retrying them. Refused at construction,
    which is the one place the value can still be attributed to whoever supplied it.

    THE SAME DEFECT EXISTED TWICE: `swarmctl`'s Gitea client had it too, and was rewritten onto
    `http.client` (no scheme handler to reach at all). Fixed in both rather than in the copy that
    happened to be under the cursor -- a duplicated scheme, not one drifted copy.
    """
    with pytest.raises(ForgeError, match='http'):
        GiteaForge(bad, 'owner/repo', username='swarm-agent')


@pytest.mark.parametrize('good', ['http://host:9000', 'https://forge.example.com', 'https://h/gitea'])
def test_http_and_https_are_accepted(good: str) -> None:
    """The discriminating half: a guard that refused everything would also pass the test above."""
    assert GiteaForge(good, 'owner/repo', username='swarm-agent').base_url.startswith(('http://', 'https://'))


def test_the_default_forge_url_satisfies_its_own_guard() -> None:
    """The shipped default must not be refused by the check added for operator input -- otherwise
    the guard is discovered by the first person to run the thing, not by this suite."""
    assert GiteaForge(DEFAULT_GITEA_BASE_URL, 'owner/repo', username='swarm-agent').base_url
