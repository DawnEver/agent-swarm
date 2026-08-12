"""WHAT A RUN ACTUALLY PRODUCED -- a record with required fields, never a log that reads well.

THE PATH THIS EXISTS TO CLOSE is `attempt -> "tests passed" -> merge`. Every step of it is an
inference from prose: somebody read output, recognised a shape they associate with success, and
advanced a pointer. The inference is not usually wrong, which is exactly the problem -- it fails in
the direction of an unearned green, silently, and the artefact left behind cannot be re-examined
because the only thing stored was the conclusion.

So evidence is an ARTIFACT and it carries, structurally, the five things a reader would otherwise
have to reconstruct or assume:

    tree          WHICH code was judged -- a digest, not a branch name, which moves under you
    environment   IN WHAT it was judged (see `manifest` for what an environment key is and is not)
    counts        the structured outcome, per column, rather than a word
    effects       what the run OBSERVABLY changed, beside what it DECLARED it would
    artifacts     the logs and reports, by digest, so "the log" names one specific byte string

ABSENCE IS A REFUSAL, NOT A DEFAULT. Every optional field with a plausible default is a place where
"nobody measured this" and "this measured zero" render identically, and the second is the reading a
tired reader will take. :class:`IncompleteEvidence` is raised at construction and names EVERY
missing field at once: one at a time teaches the requirement by attrition, and a caller that has to
iterate starts guessing.

THE ZERO-COLLECTED RUN IS THE CHEAPEST GREEN THERE IS. A suite that collected nothing has zero
failures, so any check spelled `failed == 0` passes on it. :attr:`Evidence.supports_pass` therefore
requires a non-empty population, and it reads `errors` as well as `failed` -- a run that could not
even import its tests fails in the column a one-column check does not look at.

WHAT THIS DOES NOT DO, said here so no reader supplies it. It does not decide a verdict: PASS /
FAIL / INCONCLUSIVE is the verdict plane's word and :attr:`supports_pass` is a statement about the
EVIDENCE, namely whether it could support a pass at all. It does not run anything, parse anything,
or reach the disk. And it does not refuse a deviation between declared and observed effects -- by
design, a submission whose observed effects exceed its declared intent is ACCEPTED and the deviation
is recorded as input to review and integration order. Scope is intent and routing; it is not a lock,
and a refusal here would build the path lock the whole model argues against.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

__all__ = [
    'Artifact',
    'Effects',
    'Evidence',
    'IncompleteEvidence',
    'RunCounts',
]

#: An artifact digest, permissively long-ish rather than exactly 64: a consumer may store a
#: truncated sha256 and the point of the check is to catch a FILENAME or a status word pasted into
#: the field, which is what makes an artifact reference unverifiable while looking complete.
_DIGEST_RE = re.compile(r'^[0-9a-f]{16,}$')

#: The count columns, in the order a report renders them. Named once: a second spelling of this
#: tuple would drift, and the field it forgot would be the one nothing checks.
COUNT_FIELDS = ('passed', 'failed', 'errors', 'skipped', 'xfailed', 'xpassed')


class IncompleteEvidence(ValueError):
    """A record that cannot answer one of its own questions.

    Raised for a missing field, an empty identity, a negative count and a malformed artifact
    digest -- every case where the record would otherwise exist while being unable to support the
    conclusion it will be read for.
    """


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise IncompleteEvidence(message)


@dataclass(frozen=True)
class RunCounts:
    """The outcome as COLUMNS. Every field is required and none may be negative.

    `xpassed` is carried and deliberately does not affect :attr:`Evidence.supports_pass`: an
    unexpected pass is a stale expectation to tighten, not a broken tree, and folding it into the
    pass/fail decision would make every recovery look like a regression.
    """

    passed: int
    failed: int
    errors: int
    skipped: int
    xfailed: int
    xpassed: int

    def __post_init__(self) -> None:
        for name in COUNT_FIELDS:
            value = getattr(self, name)
            _require(isinstance(value, int) and value >= 0, f'{name} must be a non-negative count, got {value!r}')

    @property
    def total(self) -> int:
        """Every test the run accounted for, skips included -- the POPULATION, not the survivors."""
        return sum(getattr(self, name) for name in COUNT_FIELDS)

    @property
    def executed(self) -> int:
        """The population minus the tests that never ran. Zero here is a vacuous run."""
        return self.total - self.skipped

    def to_mapping(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in COUNT_FIELDS}

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> RunCounts:
        missing = [name for name in COUNT_FIELDS if name not in data]
        _require(not missing, f'run counts are missing: {", ".join(missing)}')
        return cls(**{name: int(data[name]) for name in COUNT_FIELDS})  # type: ignore[arg-type]


@dataclass(frozen=True)
class Effects:
    """What the run said it would touch, beside what it did.

    Both are sorted, deduplicated tuples of repository-relative POSIX paths, so two records
    describing the same change compare and hash alike regardless of the order a diff enumerated.

    THE ASYMMETRY IS THE INTERESTING PART, and both directions are kept: `undeclared` is work the
    reader did not expect, `unrealised` is work the reader DID expect and did not get. A report loud
    about the first and silent about the second is the "loud about the harmless direction" shape --
    a declared file that never changed is the likelier sign that a step was skipped.
    """

    declared: tuple[str, ...]
    observed: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ('declared', 'observed'):
            value = getattr(self, name)
            _require(isinstance(value, Iterable) and not isinstance(value, str), f'{name} must be a sequence of paths')
            object.__setattr__(self, name, tuple(sorted({str(path) for path in value})))

    @property
    def undeclared(self) -> tuple[str, ...]:
        """Observed and not declared. RECORDED, never refused -- see the module docstring."""
        return tuple(path for path in self.observed if path not in set(self.declared))

    @property
    def unrealised(self) -> tuple[str, ...]:
        """Declared and never observed."""
        return tuple(path for path in self.declared if path not in set(self.observed))

    @property
    def deviates(self) -> bool:
        return bool(self.undeclared or self.unrealised)

    def to_mapping(self) -> dict[str, list[str]]:
        return {'declared': list(self.declared), 'observed': list(self.observed)}

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> Effects:
        missing = [name for name in ('declared', 'observed') if name not in data]
        _require(not missing, f'effects are missing: {", ".join(missing)}')
        return cls(declared=tuple(data['declared']), observed=tuple(data['observed']))  # type: ignore[arg-type]


@dataclass(frozen=True)
class Artifact:
    """One stored output, referenced BY DIGEST.

    A name alone -- `gate.log` -- names a file that can be rewritten, truncated or regenerated, so
    a record referring to one cannot say which bytes it was read from. The digest is what makes
    "the log" a specific byte string rather than a path that happens to exist.
    """

    name: str
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        _require(bool(self.name), 'an artifact needs a name')
        _require(
            bool(_DIGEST_RE.match(self.sha256)),
            f'{self.name}: {self.sha256!r} is not a hex digest, so the artifact cannot be verified',
        )
        _require(isinstance(self.size_bytes, int) and self.size_bytes >= 0, f'{self.name}: size must not be negative')

    def to_mapping(self) -> dict[str, object]:
        return {'name': self.name, 'sha256': self.sha256, 'size_bytes': self.size_bytes}

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> Artifact:
        missing = [name for name in ('name', 'sha256', 'size_bytes') if name not in data]
        _require(not missing, f'artifact is missing: {", ".join(missing)}')
        return cls(name=str(data['name']), sha256=str(data['sha256']), size_bytes=int(data['size_bytes']))  # type: ignore[arg-type]


#: The fields with no default. Enumerated so :meth:`Evidence.from_mapping` can name every absent one
#: in a single refusal rather than raising about the first.
REQUIRED_FIELDS = ('tree', 'environment', 'counts', 'effects', 'artifacts')


@dataclass(frozen=True)
class Evidence:
    """One run, recorded so that the conclusion can be re-derived from what is stored.

    `artifacts` MAY BE EMPTY and that is a real state -- a run that produced no stored output. It is
    still required as a field: an omitted key and an empty tuple would otherwise read alike, and
    "nobody collected the logs" is the one of those two worth seeing.
    """

    tree: str
    environment: str
    counts: RunCounts
    effects: Effects
    artifacts: tuple[Artifact, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require(bool(self.tree), 'evidence needs the digest of the tree that was judged')
        _require(bool(self.environment), 'evidence needs the key of the environment it was judged in')
        _require(isinstance(self.counts, RunCounts), 'counts must be a RunCounts, not a word')
        _require(isinstance(self.effects, Effects), 'effects must be an Effects')
        object.__setattr__(self, 'artifacts', tuple(self.artifacts))

    @property
    def refusal_reason(self) -> str:
        """Why this evidence cannot support a pass, or `''` when it can.

        A REASON RATHER THAN A BOOL, for the reason `environment.EnvChange` carries one: the caller
        that will not merge needs to say what it saw, and re-deriving it from the counts at the call
        site is how two sites come to disagree about the same record.
        """
        if self.counts.executed == 0:
            return 'the run collected no test that executed, so zero failures means nothing'
        broken = [name for name in ('failed', 'errors') if getattr(self.counts, name)]
        if broken:
            return 'the run reported ' + ', '.join(f'{getattr(self.counts, name)} {name}' for name in broken)
        return ''

    @property
    def supports_pass(self) -> bool:
        """Whether this record could support a pass AT ALL. Not itself a verdict -- see the module
        docstring; the verdict plane owns PASS / FAIL / INCONCLUSIVE and may refuse for its own
        reasons on evidence that satisfies this.
        """
        return not self.refusal_reason

    def to_mapping(self) -> dict[str, object]:
        return {
            'tree': self.tree,
            'environment': self.environment,
            'counts': self.counts.to_mapping(),
            'effects': self.effects.to_mapping(),
            'artifacts': [artifact.to_mapping() for artifact in self.artifacts],
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> Evidence:
        """Rebuild a record, naming EVERY missing field in one refusal."""
        missing = [name for name in REQUIRED_FIELDS if name not in data]
        _require(not missing, f'evidence is missing required field(s): {", ".join(missing)}')
        return cls(
            tree=str(data['tree']),
            environment=str(data['environment']),
            counts=RunCounts.from_mapping(data['counts']),  # type: ignore[arg-type]
            effects=Effects.from_mapping(data['effects']),  # type: ignore[arg-type]
            artifacts=tuple(Artifact.from_mapping(item) for item in data['artifacts']),  # type: ignore[union-attr]
        )

    def to_json(self) -> str:
        """Canonical JSON: sorted keys, no incidental whitespace, so the text is content-addressed."""
        return json.dumps(self.to_mapping(), sort_keys=True, separators=(',', ':'))

    @classmethod
    def from_json(cls, text: str) -> Evidence:
        data = json.loads(text)
        _require(isinstance(data, Mapping), 'evidence JSON must be an object')
        return cls.from_mapping(data)

    def digest(self) -> str:
        """A content address for the whole record.

        Over :meth:`to_json`, so the digest covers every field the record serialises and cannot
        drift from it: a field added to the mapping enters the digest in the same edit.
        """
        return hashlib.sha256(self.to_json().encode()).hexdigest()[:16]
