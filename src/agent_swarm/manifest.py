"""ONE ENVIRONMENT RECORD, SEVERAL QUESTIONS -- and they want different equivalence relations.

Today a single key answers three questions that are not the same question:

    placement     may this box run the job at all?
    trust         may a result produced elsewhere be adopted here?
    diagnostics   why do these two boxes differ?

A KEY WIDE ENOUGH FOR TRUST IS TOO WIDE FOR PLACEMENT. Trust must move on anything that could
change an outcome, which includes a patch release of a dependency and a byte of a machine-local
test input. Ask that key whether a box may RUN the job and it says no to a machine that differs in
a formatter's version -- and a placement relation that moves on every unrelated upgrade is
indistinguishable from having no placement relation at all.

SO A PROJECTION IS A NAMED, VERSIONED FUNCTION from the manifest to a key, and it is spelled that
way -- `trust/v1`, `placement/v1` -- so a stored key says which equivalence produced it. Two bare
16-hex strings from different relations compare equal by accident; a :class:`ProjectedKey` cannot.

THE IDENTITY PROJECTION IS THE DEFAULT AND IT IS EXACT. `trust/v1` selects every manifest line and
hands it to `environment.compute_envkey` unchanged, so `trust_key == envkey` -- not "equivalent to",
the same characters. That is the strictest possible policy, it is what the consuming project
enforces today, and it is already correct. Widening it buys fleet WIDTH, not correctness, so a
projection layer that widened trust by merely ARRIVING would be a declaration that lies in its
purest form: nothing fails, and every verdict silently becomes adoptable on a box that could have
changed its answer. The identity is therefore not a special case in the key computation -- it is
what selecting all the lines DOES, which is why it cannot drift from the existing behaviour.

WHY DIAGNOSTICS IS NOT A PROJECTION HERE, said plainly rather than registered as one and quietly
misused. "Why do two boxes differ" needs BOTH operands and a dependency closure; no function of a
single manifest can answer it, and a hash cannot be diffed -- which is the defect
`environment.env_manifest` was introduced to fix. It is :func:`explain`, a function of two
manifests, and it returns graded changes rather than a third key.

NOTHING HERE CHANGES AN EXISTING KEY COMPUTATION. This module reads `environment` and computes
nothing of its own; the current behaviour is expressible, and expressed, as `trust/v1`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from agent_swarm import environment

__all__ = [
    'DEFAULT_PROJECTION',
    'PLACEMENT',
    'PROJECTIONS',
    'TRUST',
    'EnvManifest',
    'MalformedManifest',
    'ProjectedKey',
    'Projection',
    'explain',
    'projection',
]

#: The prefix `placement/v1` puts on a distribution NAME so its selected line cannot be mistaken for
#: a manifest line. A bare `numpy` carries neither `==` nor `=`, and `environment.split_manifest`
#: reads such a line as the interpreter -- so an unprefixed placement selection would be a sequence
#: that silently re-parses as something else the moment anyone treats it as a manifest. It is not
#: one: it is a line set that exists to be hashed.
_PRESENT = 'present:'


class MalformedManifest(ValueError):
    """A manifest missing the interpreter line.

    Every projection here reads it -- a different Python is a different runtime for every line of a
    suite, so it can never be dropped -- and refusing at construction beats each projection
    discovering the absence separately and answering differently about it.
    """


@dataclass(frozen=True)
class EnvManifest:
    """The FULL record: the readable lines `environment.env_manifest` produces.

    KEPT VERBATIM, in order, because a projection discards and a diff cannot. A record that stored
    only what its default projection needed could never answer the diagnostics question, and the
    day someone wanted it the evidence would already be gone.
    """

    lines: tuple[str, ...]

    @classmethod
    def from_lines(cls, lines: Iterable[str]) -> EnvManifest:
        record = cls(tuple(lines))
        _, files = environment.split_manifest(record.lines)
        if 'interpreter' not in files:
            raise MalformedManifest('a manifest with no interpreter line cannot answer any environment question')
        return record

    @property
    def distributions(self) -> dict[str, str]:
        """`{canonical name: version}` for every `name==version` line."""
        return environment.split_manifest(self.lines)[0]

    @property
    def files(self) -> dict[str, str]:
        """`{path: digest}` for the machine-local test inputs, plus `interpreter`."""
        files = dict(environment.split_manifest(self.lines)[1])
        del files['interpreter']
        return files

    @property
    def interpreter(self) -> str:
        return environment.split_manifest(self.lines)[1]['interpreter']

    def key(self, using: Projection | None = None) -> str:
        """The key under `using`, DEFAULTING TO THE IDENTITY -- see the module docstring.

        The default is the strictest relation on purpose: an omitted argument must never widen
        trust, so the one projection safe to get by accident is the one already enforced.
        """
        return (using or DEFAULT_PROJECTION).key(self)

    def projected(self, using: Projection | None = None) -> ProjectedKey:
        """The key WITH the name of the relation that produced it."""
        chosen = using or DEFAULT_PROJECTION
        return ProjectedKey(projection=chosen.spelling, key=chosen.key(self))


@dataclass(frozen=True)
class Projection:
    """A named, versioned equivalence relation over environments.

    `version` IS PART OF THE SPELLING, not metadata beside it. Changing which lines a projection
    selects changes which environments it calls equal -- so a stored key computed under the old
    rule and one computed under the new must not be comparable, and they are not, because the
    spelling differs. A projection that changed in place would make every historical key a claim
    about a relation that no longer exists.
    """

    name: str
    version: int
    #: Which of the three questions this relation answers, in one sentence. Required: a projection
    #: whose purpose is not written down acquires a second one by use.
    question: str
    #: The lines this relation keeps. A LIST, not a hash, so the selection is inspectable -- the
    #: reason `environment.env_manifest` exists at all.
    select: Callable[[EnvManifest], list[str]]

    @property
    def spelling(self) -> str:
        return f'{self.name}/v{self.version}'

    @property
    def is_identity(self) -> bool:
        """Whether this relation discards nothing.

        COMPUTED, NOT DECLARED, and that is the whole guarantee: a flag saying "this is the strict
        one" is a sentence, whereas this asks the selection about a real manifest and would go
        False the moment the selection started dropping a line.
        """
        probe = EnvManifest.from_lines(('cpython-0.0.0-probe', 'a==1', 'b==2', 'p/q=0123456789abcdef'))
        return self.select(probe) == list(probe.lines)

    def key(self, record: EnvManifest) -> str:
        """The key, through the SAME computation every other environment key goes through.

        One derivation, two consumers: a projection cannot disagree with `environment` about how a
        line set becomes a key, because it does not do that arithmetic itself.
        """
        return environment.compute_envkey(self.select(record))


@dataclass(frozen=True)
class ProjectedKey:
    """A key together with the relation it belongs to."""

    projection: str
    key: str

    def __str__(self) -> str:
        return f'{self.projection}:{self.key}'


def _identity(record: EnvManifest) -> list[str]:
    """Everything, unchanged and in order -- so the key IS `environment.compute_envkey`'s."""
    return list(record.lines)


def _placement(record: EnvManifest) -> list[str]:
    """The interpreter, and WHICH distributions are installed -- never their versions.

    SCOPE, STATED SO NO READER SUPPLIES "everything". This relation observes exactly two things: the
    interpreter identity, and the SET of installed distribution names. It does NOT observe versions
    and it does NOT observe the machine-local files tests read -- both of those can change an
    outcome, which is why they decide trust, and why this key must never be used to adopt a result.
    """
    return [record.interpreter, *sorted(f'{_PRESENT}{name}' for name in record.distributions)]


TRUST = Projection(
    name='trust',
    version=1,
    question='may a result produced elsewhere be adopted here?',
    select=_identity,
)

PLACEMENT = Projection(
    name='placement',
    version=1,
    question='may this box run the job at all?',
    select=_placement,
)

#: Every relation, by spelling. Adding one is adding an entry here plus its selection function;
#: nothing resolves a projection by reaching for a module attribute.
PROJECTIONS: dict[str, Projection] = {p.spelling: p for p in (TRUST, PLACEMENT)}

#: THE DEFAULT IS THE IDENTITY. See the module docstring: an omitted argument must not widen trust.
DEFAULT_PROJECTION = TRUST


def projection(spelling: str) -> Projection:
    """Resolve `name/vN`, or KeyError NAMING the known spellings.

    Deliberately no fallback to a default: a caller that asked for a specific relation and silently
    got another would be adopting results under an equivalence it never chose.
    """
    try:
        return PROJECTIONS[spelling]
    except KeyError:
        raise KeyError(f'unknown projection {spelling!r}; known: {", ".join(sorted(PROJECTIONS))}') from None


def explain(theirs: EnvManifest, mine: EnvManifest, *, closure: frozenset[str]) -> list[environment.EnvChange]:
    """The diagnostics question: WHAT separates two environments, each entry graded for reuse.

    `closure` IS REQUIRED and has no default here for the reason `environment` gives: which
    distribution roots a project's dependency graph is the consumer's fact, and a default would let
    every caller that omitted it grade against somebody else's. An EMPTY closure is read by
    `environment.env_diff` as "cannot tell" and blocks reuse -- not as "nothing is relevant".
    """
    return environment.env_diff(theirs.lines, mine.lines, closure)
