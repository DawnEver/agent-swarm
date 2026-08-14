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

from agent_swarm import signing

__all__ = [
    'Artifact',
    'Effects',
    'Evidence',
    'IncompleteEvidence',
    'RecordVerdict',
    'RunCounts',
    'sign_verdict',
    'verify_verdict',
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
    #: WHO PRODUCED THIS RECORD. Optional because a run's evidence can exist before it is judged --
    #: but a record with no signer is not yet attestable, and :meth:`sign` refuses to sign it as if
    #: it were someone's. `None` (the default) is therefore "not produced", never "produced by nobody".
    signer: str | None = None

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
            'signer': self.signer,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> Evidence:
        """Rebuild a record, naming EVERY missing field in one refusal.

        `signer` is OPTIONAL -- a record predating attestation, or one not yet produced, has no
        signer -- so its absence is not a refusal; it defaults to `None`.
        """
        missing = [name for name in REQUIRED_FIELDS if name not in data]
        _require(not missing, f'evidence is missing required field(s): {", ".join(missing)}')
        return cls(
            tree=str(data['tree']),
            environment=str(data['environment']),
            counts=RunCounts.from_mapping(data['counts']),  # type: ignore[arg-type]
            effects=Effects.from_mapping(data['effects']),  # type: ignore[arg-type]
            artifacts=tuple(Artifact.from_mapping(item) for item in data['artifacts']),  # type: ignore[union-attr]
            signer=str(data['signer']) if data.get('signer') else None,
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
        drift from it: a field added to the mapping enters the digest in the same edit. `signer` is
        in that mapping, so the digest -- and therefore any tag computed over it -- is bound to who
        produced the record.
        """
        return hashlib.sha256(self.to_json().encode()).hexdigest()[:16]

    def sign(self, key: bytes | str) -> str:
        """A tag authenticating THIS record, bound to its producer.

        Requires a `signer`: a record nobody produced cannot be signed as if it were someone's. Signs
        the CONTENT ADDRESS (:meth:`digest`), so the tag authenticates every field the record
        serialises -- `signer` included -- and moves with the record's canonical text.
        """
        if not self.signer:
            raise IncompleteEvidence('cannot sign an evidence with no signer')
        return signing.sign(key, self.digest().encode())

    def verify(self, key: bytes | str, tag: str) -> bool:
        """Whether `tag` is a valid signature of THIS record under `self.signer`'s key.

        The reader must hold the signer role's key -- the very property that keeps an untrusted forge
        from manufacturing it. A record with no signer is not verifiable and answers False rather
        than raising: on the must-verify read path an unverifiable verdict is noise, not an error.
        """
        if not self.signer:
            return False
        return signing.verify(key, self.digest().encode(), tag)


@dataclass(frozen=True)
class RecordVerdict:
    """A VERDICT AS A SIGNED PAYLOAD -- the answer, the evidence, and the tag that makes the two
    trustworthy together.

    §3.1: a verdict is a payload signed by the producing role's key; a reader verifies BEFORE the
    verdict counts; an unsigned or badly-signed verdict is detectable noise, never a verdict. The
    `tag` binds the verdict WORD and the ref identity to the evidence's content address, so a tag
    cannot be lifted onto a different verdict, a different tree or a different producer and still
    verify. `tag` may be empty -- that is an UNSIGNED record, which :func:`verify_verdict` refuses on
    the read path rather than the constructor, so "was never signed" stays a detectable state.
    """

    testkey: str
    kind: str
    envkey: str
    verdict: str
    evidence: Evidence
    tag: str
    signer: str

    def __post_init__(self) -> None:
        _require(bool(self.signer), 'a verdict needs a producer')
        _require(self.signer == self.evidence.signer, 'the verdict producer and the evidence signer must agree')


def _verdict_payload(testkey: str, kind: str, envkey: str, verdict: str, evidence_digest: str) -> bytes:
    """The canonical bytes the verdict's tag authenticates.

    Canonical (sorted keys, no whitespace) for the same reason :meth:`Evidence.to_json` is: the
    reader recomputes exactly what the producer signed, and a drift between the two is a verdict
    that can never verify.
    """
    return json.dumps(
        {
            'testkey': testkey,
            'kind': kind,
            'envkey': envkey,
            'verdict': verdict,
            'evidence': evidence_digest,
        },
        sort_keys=True,
        separators=(',', ':'),
    ).encode()


def sign_verdict(
    key: bytes | str,
    *,
    testkey: str,
    kind: str,
    envkey: str,
    verdict: str,
    evidence: Evidence,
) -> RecordVerdict:
    """Sign an `evidence` (which must carry a `signer`) into a verdict payload binding the ref
    identity and the verdict word to the evidence's content address. Returns the signed record.

    THIS IS THE PRODUCER-SIDE HOOK. The verdict write path lives in the consumer (motronics'
    `ci_tick`), which calls this with the role's key and stores the resulting `RecordVerdict`;
    agent-swarm supplies the primitive and the structure, and the write path binding is the
    consumer's next step.
    """
    if not evidence.signer:
        raise IncompleteEvidence('cannot sign a verdict for evidence with no signer')
    tag = signing.sign(key, _verdict_payload(testkey, kind, envkey, verdict, evidence.digest()))
    return RecordVerdict(
        testkey=testkey,
        kind=kind,
        envkey=envkey,
        verdict=verdict,
        evidence=evidence,
        tag=tag,
        signer=evidence.signer,
    )


def verify_verdict(record: RecordVerdict, key: bytes | str) -> bool:
    """THE MUST-VERIFY READ PATH. Whether `record` is a verdict this reader may count: the tag is
    present and is a valid signature under `key` (the producer role's key) of exactly this payload.

    An unsigned (empty-tag) or badly-signed record answers False -- it is detectable noise, never a
    verdict, and it must not advance anything. The forge, which never holds `key`, cannot compute a
    valid tag no matter what it has seen or moved.
    """
    if not record.tag:
        return False
    return signing.verify(
        key,
        _verdict_payload(record.testkey, record.kind, record.envkey, record.verdict, record.evidence.digest()),
        record.tag,
    )
