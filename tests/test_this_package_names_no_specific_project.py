"""This package must not know which project it is scheduling.

THE CLAIM AND WHY IT WAS FALSE. The design says the forge is "a task database that happens to have
an API" -- no vendor lock-in, no project lock-in. Two lines refuted it at package scope:

    forge.py    DEFAULT_REPO   = 'Tianjie-Zou-Team/motronics-studio'
    status.py   STATUS_CONTEXT = 'motronics/gate'

A generic layer that hard-codes one repository and one check name is this project's own dominant
defect -- a declaration that lies -- with the declaration being the package's name and its README.
Worse than a wrong comment: the DEFAULTS made the coupling invisible. Every caller that omitted the
argument silently got that repo, so nothing ever failed to reveal it, and a second consumer would
have discovered it by writing to somebody else's issue tracker.

SO THE FIX IS A REMOVAL, NOT A RENAME, and the removal must be of the DEFAULT and not merely of the
constant. A default is the mechanism by which the coupling is invisible; a module-level constant
that no signature reaches for is a value nobody uses. Both are gone: `repo` and `context` are
required arguments, and a caller that does not supply one gets a `TypeError` at the call rather than
a surprise on a stranger's repository.

WHERE THE ONE DEFINITION OF THE GATE CONTEXT NOW LIVES. `status.py` used to warn that its
`STATUS_CONTEXT` had a second copy in motronics' `swarmctl` and that the two must not drift. Two
copies of one fact is exactly the failure that comment described, and the way to have one copy is
not to synchronise them -- it is for the fact to have ONE OWNER. The fact "this project's gate check
is called X" is the CONSUMER's, because the consumer is what configures its own branch protection.
So this package holds no copy at all and takes the value as an argument, and `swarmctl`'s becomes
the sole definition rather than the surviving duplicate.

WHAT THIS FILE SCANS, STATED SO THE READER DOES NOT SUPPLY "EVERYTHING". It reads every module under
`src/agent_swarm` and rejects a forbidden token appearing in a NAME or in a string the code can USE
AS A VALUE. It deliberately does NOT reject the token in any DISCARDED string statement -- module, class and
function docstrings, and ATTRIBUTE docstrings, which it used to miss -- nor in comments: several modules
record, correctly, that they were extracted from motronics' `ci_tick.py`, and that is provenance
about where the code came from rather than a dependency on where it runs. Deleting true history to
satisfy a grep would be the reverse of this project's rule about declarations. The distinction is
mechanical -- a docstring is an expression statement, a default is a value -- so it is drawn by the
AST rather than by judgement.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agent_swarm import forge, status
from agent_swarm.forge import ROLE_ACCOUNTS, GiteaForge, default_forge
from agent_swarm.status import StatusPublisher
from agent_swarm.testing import RecordingForge

PACKAGE = Path(forge.__file__).parent

#: Lower-cased substrings that name ONE project rather than a capability. The owner is included
#: because a repo path is two halves and only one of them is the project's name.
#:
#: THE VENDOR TOOLS ARE HERE FOR THE SAME REASON THE REPO IS. `admission.KNOWN_CLASSES` enumerated
#: `vendor:femm` and `vendor:jmag` -- two FEA tools belonging to the consumer, not to the fleet --
#: and the shape rule that replaced them accepts those strings anyway, so NO BEHAVIOURAL test can
#: tell a re-enumeration from the mechanism. Only a source assertion can, which is what this is.
#: The docstring exemption below is what keeps the MEASUREMENTS ("jmag takes 356 s solo") legal:
#: those record where a number came from, and this file's whole point is that provenance is not
#: coupling.
FORBIDDEN = ('motronics', 'tianjie-zou', 'femm', 'jmag')


def _offending(source: str, *, where: str, tokens: tuple[str, ...] = FORBIDDEN) -> list[str]:
    """Every place in `source` one of `tokens` appears as a NAME or a usable VALUE.

    A FUNCTION RATHER THAN AN INLINE LOOP, so the detector itself can be tested against a module
    that DOES offend. A scanner that silently matched nothing -- a typo in a token, a walk that
    misses `ast.Constant` inside an f-string -- would report this package clean forever, which is
    the same green-because-the-instrument-is-broken shape the double-model version exists for.

    `tokens` IS A PARAMETER because a second source-level vocabulary ban exists --
    `test_job.py`'s barrier scan -- and the rule it needs is identical down to the docstring
    exemption. A second copy of this walk is the duplicated-scheme defect, and the copies would
    diverge exactly where this one has already been corrected once (attribute docstrings).
    """
    tree = ast.parse(source)
    # A DISCARDED STRING STATEMENT IS THE DEFINITION, not "the first statement of a module/def".
    # MEASURED 2026-08-11: `layers.py` recorded its provenance in an ATTRIBUTE docstring -- the
    # string written under `DEV_TOOL = ...`, which `help()` renders and every reader calls a
    # docstring -- and this scanner rejected it, because the old set only ever collected `body[0]`.
    # That is a scope narrower than the name, the variant this repo calls worse than a behaviour
    # bug: it does not make you distrust the check, it makes you reword TRUE provenance to appease
    # it. The replacement rule is both exact and simpler -- a bare string expression statement is
    # evaluated and thrown away, so it can never be a value the code uses.
    docstrings = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
    }
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node not in docstrings:
            lowered = node.value.lower()
            hits.extend(f'{where}:{node.lineno} value {node.value!r}' for t in tokens if t in lowered)
        # MEASURED 2026-08-12: this tuple used to stop at `ast.Name | ast.Attribute | ast.arg`, so a
        # `def` or `class` NAME was never scanned -- `def motronics_gate(): ...` passed. A function
        # definition's name is a plain `str` field of `FunctionDef`, not a `Name` node, and nothing
        # said so; the guard's SCOPE was narrower than the sentence "appears in a NAME", which is
        # the variant that makes a reader route around a working check. `keyword` is here for the
        # same reason: `f(motronics_repo=1)` names it at the CALL, where nothing else looks.
        elif isinstance(
            node,
            ast.Name | ast.Attribute | ast.arg | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.keyword,
        ):
            name = (
                getattr(node, 'id', None)
                or getattr(node, 'attr', None)
                or getattr(node, 'arg', None)
                or getattr(node, 'name', None)
                or ''
            )
            hits.extend(f'{where}:{node.lineno} name {name!r}' for t in tokens if t in name.lower())
    return hits


# --------------------------------------------------------------- the detector actually detects


def test_the_scanner_catches_a_hard_coded_repo():
    """The instrument first. This is the exact line that was in `forge.py`."""
    assert _offending("DEFAULT_REPO = 'Tianjie-Zou-Team/motronics-studio'\n", where='x')


def test_the_scanner_catches_a_hard_coded_check_name():
    """And the exact line that was in `status.py`."""
    assert _offending("STATUS_CONTEXT = 'motronics/gate'\n", where='x')


def test_the_scanner_catches_it_hidden_in_a_default_argument():
    """Where it would actually come back. Nobody re-adds a module constant; somebody adds a
    convenient default to one signature, and every caller that omits the argument gets the coupling
    without a line of code naming it.
    """
    assert _offending("def f(repo='Tianjie-Zou-Team/motronics-studio'): ...\n", where='x')


def test_the_scanner_catches_it_in_an_fstring():
    """An f-string's literal halves are `Constant` nodes inside a `JoinedStr`, so a walk that only
    looked at top-level assignments would miss `f'motronics/{name}'` entirely.
    """
    assert _offending("x = f'motronics/{name}'\n", where='x')


def test_the_scanner_exempts_an_ATTRIBUTE_docstring():
    """The 2026-08-11 miss. `help()` renders this string, PEP 258 names it, every reader calls it a
    docstring -- and the scanner did not, because it is not `body[0]` of anything."""
    assert not _offending("DEV_TOOL = frozenset()\n'''extracted from motronics.'''\n", where='x')


def test_the_exemption_does_NOT_cover_a_string_the_code_can_reach():
    """The control, and the one that matters: the exemption is for a DISCARDED statement, so a
    string one line away that is actually bound must still trip. Without this, widening the
    exemption could have quietly turned the whole scanner off."""
    assert _offending("DEV_TOOL = 'motronics'\n'''prose.'''\n", where='x')


def test_the_scanner_catches_it_as_an_identifier():
    """A renamed constant is still the coupling. `MOTRONICS_GATE = _cfg['gate']` names the project
    in the one place a reader looks for what a module knows about the world.
    """
    assert _offending('MOTRONICS_GATE = 1\n', where='x')


def test_the_scanner_catches_an_enumerated_vendor_class():
    """The exact line that was in `admission.py`. A vendor class is a KIND -- "monopolises one
    licensed tool" -- and WHICH tools exist is the consumer's data; a built-in list makes adding one
    a release of this package. It is undetectable behaviourally, because the shape rule accepts
    those two strings too, so it is pinned at the source.
    """
    assert _offending("KNOWN_CLASSES = frozenset({'expensive', 'cheap', 'vendor:femm', 'vendor:jmag'})\n", where='x')


def test_the_scanner_catches_it_in_a_DEF_or_CLASS_name():
    """MEASURED 2026-08-12, and it was a real miss: a function or class name is a `str` field of
    the definition node, not a `Name`, so the walk never saw it. Found by a second caller of this
    scanner planting `def next_generation(...)` and getting a clean bill.
    """
    assert _offending('def motronics_gate(): ...\n', where='x')
    assert _offending('class FemmPool: ...\n', where='x')


def test_the_scanner_catches_it_as_a_KEYWORD_at_the_call_site():
    """`f(motronics_repo=1)` names the coupling where no other node type looks."""
    assert _offending('f(motronics_repo=1)\n', where='x')


def test_the_scanner_ignores_a_docstring():
    """The discriminating half, and the reason the check is AST-based rather than a grep. Several
    modules truthfully record that they were extracted from motronics' `ci_tick.py`; that is
    provenance, not coupling, and deleting true history to satisfy a scanner would be the reverse of
    this project's rule about declarations.
    """
    assert not _offending('"""Extracted from motronics\' ci_tick.py."""\n', where='x')


def test_the_scanner_ignores_a_comment():
    """Comments are not in the AST at all. Stated as a test so the exemption is a decision on
    record rather than an accident of the parser.
    """
    assert not _offending('# motronics used to own this\nx = 1\n', where='x')


# --------------------------------------------------------------- and the package is clean


@pytest.mark.parametrize('module', sorted(PACKAGE.glob('*.py')), ids=lambda p: p.name)
def test_no_module_names_a_specific_project(module: Path):
    """THE ARCHITECTURE ASSERTION. Parametrized per file so a new module is covered the moment it
    is added, and so a failure names the file rather than the package.
    """
    hits = _offending(module.read_text(encoding='utf-8'), where=module.name)
    assert not hits, f'{module.name} names a specific project:\n  ' + '\n  '.join(hits)


# --------------------------------------------------------------- the defaults are gone


def test_the_forge_module_has_no_default_repo():
    """A leftover constant nothing reaches for is still the value the next caller reaches for."""
    assert not hasattr(forge, 'DEFAULT_REPO')


def test_the_status_module_has_no_default_context():
    """`swarmctl`'s copy is now the ONLY definition of the gate's name, which is what "one
    definition of one fact" means -- not two spellings kept in step by a comment.
    """
    assert not hasattr(status, 'STATUS_CONTEXT')


def test_default_forge_will_not_guess_a_repo():
    """A DEFAULT is what made the coupling invisible: every caller that omitted the argument got
    that repo and nothing ever failed to say so. A `TypeError` at the call is the whole fix.
    """
    with pytest.raises(TypeError):
        default_forge('agent')  # type: ignore[call-arg]


def test_default_forge_still_builds_a_forge_when_told_which_repo():
    """The discriminating half: a function that refused everything would satisfy the test above and
    leave the package unusable.
    """
    built = default_forge('verifier', repo='owner/name')
    assert isinstance(built, GiteaForge)
    assert built.repo == 'owner/name'
    assert built.username == ROLE_ACCOUNTS['verifier']


def test_default_forge_takes_the_base_url_too():
    """The DEPLOYMENT is still defaulted -- it is this swarm's own host, not a project -- but it
    must be overridable, or a second deployment is a source edit.
    """
    assert default_forge('agent', repo='o/n', base_url='https://forge.example.com/x').base_url.endswith('/x')


def test_a_status_publisher_will_not_guess_a_check_name():
    """A publisher that defaulted its context would write to a check name the caller never chose --
    and a commit status cannot be deleted on Gitea, so the wrong name is permanent.
    """
    with pytest.raises(TypeError):
        StatusPublisher(RecordingForge(username=ROLE_ACCOUNTS['verifier']), runner='ws1')  # type: ignore[call-arg]


def test_a_status_publisher_uses_the_context_it_was_given():
    """The discriminating half, and it also pins the composition: the caller supplies the BASE and
    the publisher appends its own runner, so the caller cannot accidentally supply a full context
    and get the writer's name twice.
    """
    publisher = StatusPublisher(
        RecordingForge(username=ROLE_ACCOUNTS['verifier']), context='someproject/gate', runner='ws1'
    )
    assert publisher.context == 'someproject/gate/ws1'
