"""Branch protection requires the per-runner FAMILY of the gate context, never the bare name.

THE FAILURE THIS PREVENTS IS PERMANENT AND SILENT-LOOKING. Every verifier now publishes its commit
status under `<context>/<runner>`, so that two verifiers disagreeing about one tree cannot overwrite
each other on a shared key. The consequence is that NOBODY publishes `<context>` itself any more.

A protection rule naming the bare `<context>` is, to Gitea, a glob with no metacharacters. It matches
nothing, forever. `main` freezes: every merge waits on a check no process writes, and the symptom --
merges hanging -- reads as a broken gate rather than as a rule naming a check that has no producer.
That is this repo's own "a flag existing is not a runner running", landing on the merge path with no
way to notice from inside the code.

MEASURED, at tag v1.26.4 -- the version this instance runs (`GET /api/v1/version` ->
`{'version': '1.26.4'}`), read from Gitea's source rather than inferred from its docs:

* `services/pull/commit_status.go:31` compiles each required context with `glob.Compile(ctx)`,
  the in-tree `modules/glob`, called with NO separator runes;
* `modules/glob/glob.go:114-124` therefore sets `nonSeparatorChars = "."`, so `*` compiles to `.*`
  and DOES cross `/`. `<context>/*` matches `<context>/G-bf92f8b5`;
* a required context matched by nothing yields `CommitStatusPending`, and `IsPullCommitStatusPass`
  returns `state.IsSuccess()` -- so a fully-down fleet BLOCKS. The safe direction, by construction
  rather than by our arrangement.

WHY THE SUFFIX IS TESTED AND NOT JUST DOCUMENTED. The claim "this rule covers every runner" is a
claim about a GUARD'S SCOPE, which AGENTS.md names as the worst kind to get wrong: a behaviour-lie
makes you distrust the code, a scope-lie makes you route around a working check. So this plants a
realistic runner name and asserts the REAL pattern matches it, rather than re-deriving Gitea's
traversal or trusting the docstring above.
"""

from __future__ import annotations

import re

import pytest

from agent_swarm import swarmctl as _swarmctl

pytestmark = pytest.mark.unit


@pytest.fixture(scope='module')
def swarmctl():
    """The module under test, as an ORDINARY IMPORT.

    It used to be loaded by path with `importlib.util.spec_from_file_location`, because it lived in
    another project as a bare script that nothing could import. Here it is a module of this package,
    so the loader preamble is gone -- and with it a whole class of mistake, since a hand-rolled load
    can silently execute a DIFFERENT file from the one an import would resolve.
    """
    return _swarmctl


@pytest.fixture
def sent(swarmctl, monkeypatch):
    """Capture the body `protect` would PUT, without reaching a server."""
    bodies: list[dict] = []
    provider = swarmctl.GiteaProvider('http://host:9000', 'Org', None, 'admin')
    monkeypatch.setattr(provider, 'protections', lambda _o, _r: [])
    monkeypatch.setattr(provider, 'api', lambda _m, _p, body=None: bodies.append(body or {}))
    provider.protect('Org', 'Repo', 'main', 'someproject/gate')
    assert bodies, 'protect() sent nothing'
    return bodies[0]


def test_the_required_context_is_a_PATTERN_not_the_bare_name(sent):
    """THE DEFECT ITSELF. The bare name has no producer since verdicts moved to per-runner keys."""
    assert sent['status_check_contexts'] == ['someproject/gate/*'], sent['status_check_contexts']


def _gitea_glob(pattern: str) -> re.Pattern[str]:
    """Gitea 1.26.4's compilation of a required context, for the syntax this repo actually uses.

    NOT a general glob engine -- it covers `*` and literals, which is all a status context contains,
    and it is written here so the assertion below is about GITEA'S rule rather than about Python's
    `fnmatch`, whose `*` also crosses `/` but for unrelated reasons. If this ever needs `?` or `[]`,
    that is the moment to stop modelling and measure against the server instead.
    """
    return re.compile('^' + '.*'.join(re.escape(part) for part in pattern.split('*')) + '$')


def test_the_pattern_matches_a_REAL_runner_context(sent):
    """Planted, not re-derived: `G-bf92f8b5` is the shape `ci.py status` prints for this fleet."""
    pattern = _gitea_glob(sent['status_check_contexts'][0])
    assert pattern.match('someproject/gate/G-bf92f8b5')


def test_the_pattern_does_NOT_match_the_bare_context(sent):
    """The discriminating half, and it is not pedantry: it pins that the family and the bare name are
    DIFFERENT strings, which is the whole reason a rule written against one is a permanent block.
    """
    assert not _gitea_glob(sent['status_check_contexts'][0]).match('someproject/gate')


def test_the_pattern_does_not_swallow_a_NEIGHBOURING_namespace(sent):
    """`<context>*` would match `someproject/gateway/...`. The suffix is `/` + `*` for that reason, and
    without this test the two spellings are indistinguishable by every other assertion here.
    """
    assert not _gitea_glob(sent['status_check_contexts'][0]).match('someproject/gateway/x')


# THE SEAM THIS FILE CANNOT CLOSE FROM HERE, and it is deliberately left open.
#
# The strongest version of this file's claim is "the base context the protection rule requires is
# the one the PUBLISHER writes" -- a drift made impossible rather than merely unlikely. That test
# exists, and it stayed behind in the consumer's suite, because it must ask BOTH ends for the name
# and one of those ends is the consumer's own publisher. Asserting it here would mean this package
# naming a project's check, which is the exact coupling `test_this_package_names_no_specific_project`
# refuses.
#
# What is assertable here is the half that belongs to this package: whatever base it is given, the
# rule it writes is that base's per-runner FAMILY. The tests above pin that against a context that
# is deliberately NOT any real project's.


def test_onboard_REFUSES_to_protect_without_a_context(swarmctl):
    """The other half of removing the built-in default. An unset name must not reach `protect` as an
    empty required status -- protection that looks enabled and requires no check is worse than the
    default it replaced, and it is silent.
    """
    args = type('A', (), {'protect': True, 'status_context': '', 'branch': 'main', 'repo': 'Org/Repo'})()
    with pytest.raises(SystemExit, match='status context'):
        swarmctl.cmd_onboard(swarmctl.GiteaProvider('http://host:9000', 'Org', None, 'admin'), args)
