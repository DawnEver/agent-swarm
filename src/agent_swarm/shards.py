"""REASSEMBLY: folding N slice results back into one verdict, which is the dangerous half.

WHAT THIS IS. `compose_shard_verdict` from motronics' `scripts/ci/ci_tick.py`. It names no project
noun: it takes ``{index: result}`` and returns a verdict, and the three result words are the same
three every tier in this package already speaks.

WHY REASSEMBLY AND NOT SPLITTING. Sharding is the only mechanism that turns fleet size into a
faster answer, and splitting a suite is arithmetic. Putting the pieces back together is where a
green gets minted for work nobody did, which is why this function was written and tested BEFORE
anything could produce a partial result for it.

PRECEDENCE, and each line of it is a decision rather than a convenience::

    any slice FAIL                     -> FAIL           a real result about the code
    else any INCONCLUSIVE or MISSING   -> INCONCLUSIVE   nothing is claimed
    all present and PASS               -> PASS

**A MISSING SLICE RANKS WITH INCONCLUSIVE, NEVER WITH PASS.** A lost slice and a crashed slice are
the same epistemic state -- that fraction has no result -- and treating absence as success would
mint a green for work that never ran, arriving through the door built to make things faster.

**FAIL OUTRANKS BOTH.** If one slice genuinely reddened, that is a fact about the code, and another
slice's infrastructure noise does not unsay it.

SLICES ARE 1-INDEXED, matching the convention of whatever performs the partition. Holding a second,
0-based convention here and converting at the boundary is how an off-by-one ships: one slice
silently never runs, and by the rule above that reads as INCONCLUSIVE rather than as a wrong green
-- but it reads as INCONCLUSIVE FOREVER, for a reason nobody can see. One convention, owned by the
partitioner.
"""

from __future__ import annotations

PASS = 'PASS'
FAIL = 'FAIL'
INCONCLUSIVE = 'INCONCLUSIVE'


def compose(reported: dict[int, str], n_shards: int) -> dict:
    """Fold ``{slice index: result}`` into ONE verdict for the whole job.

    Returns a payload carrying `result` and a `reason` NAMING THE SLICES responsible. The reason is
    not decoration: a bare INCONCLUSIVE tells an operator nothing about whether to wait for a
    straggler or to go and look at a dead runner.

    Raises:
        ValueError: `n_shards` is below 1, or a reported index is outside ``1..n_shards``. The
            latter means the caller and the job disagree about N, which is never safe to ignore
            silently: some fraction of the work is unaccounted for in a partition that is supposed
            to be exact.
    """
    if n_shards < 1:
        msg = f'n_shards must be >= 1, got {n_shards}'
        raise ValueError(msg)
    if bad := sorted(i for i in reported if not 1 <= i <= n_shards):
        msg = f'shard index out of range for a {n_shards}-way split: {bad} (shards are 1..n)'
        raise ValueError(msg)

    failed = sorted(i for i, r in reported.items() if r == FAIL)
    if failed:
        return {'result': FAIL, 'reason': f'shard(s) {failed} of {n_shards} reported FAIL'}

    missing = sorted(set(range(1, n_shards + 1)) - set(reported))
    unclear = sorted(i for i, r in reported.items() if r != PASS)
    if missing or unclear:
        parts = []
        if missing:
            parts.append(f'{len(missing)} of {n_shards} shard(s) never reported: {missing}')
        if unclear:
            parts.append(f'shard(s) {unclear} INCONCLUSIVE')
        return {'result': INCONCLUSIVE, 'reason': '; '.join(parts)}

    return {'result': PASS, 'reason': f'all {n_shards} shard(s) PASS'}


def next_unfinished(reported: dict[int, str], n_shards: int) -> int | None:
    """The lowest slice in ``1..n`` with no result yet, or ``None`` when the set is complete.

    FILLS HOLES, DOES NOT APPEND: a slice whose runner died leaves a gap, and a picker that only
    ever took the next unstarted index would leave that gap permanent -- which by :func:`compose`
    is INCONCLUSIVE forever.

    LOWEST-FIRST IS DELIBERATE, NOT LAZY. With N runners polling, a deterministic order means two
    racing runners pick the SAME slice and the claim lease resolves it -- a mechanism that already
    exists and is already tested. Random picking would spread runners faster and make the collision
    path rare, which is the same as making it untested.
    """
    done = set(reported)
    return next((i for i in range(1, n_shards + 1) if i not in done), None)
