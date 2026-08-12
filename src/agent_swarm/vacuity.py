"""Find module-level collections a check consumes that are EMPTY or that can never match.

A guard whose declared job is contradicted by its actual intersection with the population it filters
passes by VACUITY: green, permanently, while filtering nothing. This module finds three shapes of
that, and none of them is about any particular project -- they are properties of Python source and
of `pytest.mark.parametrize`.

PROVENANCE. Extracted 2026-08-12 from motronics' `scripts/gate/vacuity_sieve.py`, 448 own code lines
at 2.23 % project density. What stayed behind is the REPLAY DENYLIST (which of a project's own
symbols make a replay unsafe) and which directories to scan. Both arrive here as arguments.

WHY THREE LAYERS, and why none subsumes another. Two instances found by hand (2026-07-28) dictate
the first two, and the third exists because neither could tell those two apart from their correct
neighbours:

  [A] RUNTIME SIZE. A collection built by a comprehension over discovered inputs has no size in the
      source text. So layer A IMPORTS each module -- plain importlib, no pytest session -- and calls
      `len()` on every module-level collection something checks. Size 0 while something iterates it
      is the whole finding. Static text cannot do this.

  [B] ARITY MISMATCH, pure AST, no import. For every `X in COLL` where COLL is a module-level
      collection of fixed-arity tuples, compare that arity against X's. A bare name or a string
      literal tested against a set of 2-tuples can never match -- statically provable. The confirmed
      instance had SIZE 2 and was a complete no-op, so layer A is blind to it.

  [C] CALL-SITE POPULATION. Size is not a claim about anything: a 2-element guard that filters
      correctly and a 2-element guard that matches nothing are indistinguishable by size and by
      arity. The number that makes both obvious is the INTERSECTION. For every `X in GUARD`, layer C
      replays the enclosing parametrized test once per parameter with the comparison replaced by a
      probe, records every value of X that actually REACHES the test, and reports `M of N`.
      **`0 of N` with N > 0 is the defect signal; `UNKNOWN` is not a clean result.**

      Layer C's population is MEASURED. Where it cannot be produced, the site is reported as
      `UNKNOWN <reason>` and NEVER as a number.

WHAT LAYER C CANNOT DO -- read before trusting a count.
  * It replays a test's own body, so it observes the population OF THIS ENVIRONMENT (same caveat as
    layer A: a guard that is empty only where some optional tool is missing is full here).
  * It refuses any test taking a FIXTURE, because supplying one means running pytest. Those sites
    are UNKNOWN, and that is most of a suite.
  * It refuses a test carrying any mark other than `parametrize`, and any body naming a DENYLISTED
    symbol. Safety, not analysis: a replay is real execution.
  * A guard nothing tests membership against has no call site and therefore no population -- it is
    absent from C entirely, which is correct: it makes no claim to be vacuous about.

THE REPLAYABLE SHAPE -- how a NEW membership guard gets measured for free. A test is replayable when
ALL of these hold: it is parametrized ONLY by pure `@pytest.mark.parametrize` decorators whose
argvalues are resolvable from the imported module; every function parameter is declared by one of
those decorators; it carries no other mark; its body names no denylisted symbol; and the membership
test sits in the function BODY rather than inside a `parametrize` argvalues comprehension -- that
one BUILDS the population and is never executed by the replay.
"""

from __future__ import annotations

import ast
import copy
import importlib
import itertools
import sys
import time
import traceback
from collections.abc import Sequence, Sized
from pathlib import Path
from types import ModuleType

__all__ = [
    'MUTABLE_TYPES',
    'REPLAYABLE_MARKS',
    'SCALARISH_LHS',
    'SELF_TEST_FIXTURE',
    'consumed_by',
    'element_arity',
    'layer_a',
    'layer_b',
    'layer_c',
    'module_level_collections',
    'render_report',
    'scan_paths',
    'self_test',
    'tuple_arity',
]

#: Layer A ignores these: a collection that is empty BY CONSTRUCTION at import time and filled later
#: is an accumulator, not a vacuous guard. (The negative-control class.)
MUTABLE_TYPES = (list, dict, set, bytearray)

#: LHS shapes whose value is a SCALAR unless the code goes out of its way -- a dict lookup, a
#: `.get()`, an attribute. The confirmed instance's LHS was `flow.get('reference')`, so restricting
#: to `ast.Tuple` alone made layer B fail its own positive control on the first run. A bare
#: `ast.Name` is deliberately NOT here: a variable holding a tuple is ordinary and would flood.
SCALARISH_LHS = (ast.Call, ast.Subscript, ast.Attribute)

#: A test carrying any other mark may need an environment we are not in, or is declared-failing.
#: Only a pure parametrize is replayable.
REPLAYABLE_MARKS = frozenset({'parametrize'})


def module_level_collections(tree: ast.Module) -> dict[str, ast.expr]:
    """Every `NAME = <expr>` at module level whose value could be a collection.

    Includes comprehensions and calls (`sorted(...)`, `frozenset(...)`), because the two confirmed
    instances were one literal and one comprehension. Type is settled at runtime by layer A.
    """
    out: dict[str, ast.expr] = {}
    for node in tree.body:
        targets = (
            node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        )
        value = getattr(node, 'value', None)
        if value is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id.isupper():
                out[target.id] = value
    return out


def tuple_arity(node: ast.expr) -> int | None:
    """Arity of a fixed-shape element: N for an N-tuple literal, 1 for a string literal, else None."""
    if isinstance(node, ast.Tuple):
        return len(node.elts)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return 1
    return None  # a variable / call: arity not decidable from the text


def element_arity(value: ast.expr) -> int | None:
    """Uniform arity of a literal collection's elements, or None if not uniform / not a literal."""
    if not isinstance(value, ast.Tuple | ast.List | ast.Set):
        return None
    arities = {tuple_arity(elt) for elt in value.elts}
    if len(arities) != 1 or None in arities:
        return None
    return arities.pop()


def consumed_by(tree: ast.Module) -> dict[str, set[str]]:
    """For each module-level NAME, the check constructs that consume it.

    A collection nothing checks is not a guard, so its size carries no claim. This is the difference
    between a candidate and a legitimately-small constant.
    """
    out: dict[str, set[str]] = {}

    def mark(node: ast.expr, kind: str) -> None:
        for inner in ast.walk(node):
            if isinstance(inner, ast.Name):
                out.setdefault(inner.id, set()).add(kind)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ('parametrize', 'skipif', 'xfail'):
                for arg in node.args:
                    mark(arg, node.func.attr)
        elif isinstance(node, ast.Compare) and node.ops and isinstance(node.ops[0], ast.In | ast.NotIn):
            mark(node.comparators[0], 'membership')
        elif isinstance(node, ast.For | ast.comprehension):
            mark(node.iter, 'iteration')
    return out


def layer_b(paths: list[Path], root: Path) -> list[str]:
    """Membership tests that can NEVER match: LHS arity != the collection's element arity.

    Decidable from the text alone -- a bare string tested against a set of 2-tuples is
    0-of-everything regardless of the data -- so this layer imports nothing.
    """
    hits = []
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'))
        except (SyntaxError, UnicodeDecodeError):
            continue
        colls = module_level_collections(tree)
        arities = {name: arity for name, value in colls.items() if (arity := element_arity(value)) is not None}
        if not arities:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare) or not node.ops:
                continue
            if not isinstance(node.ops[0], ast.In | ast.NotIn):
                continue
            right = node.comparators[0]
            if not isinstance(right, ast.Name) or right.id not in arities:
                continue
            want, got = arities[right.id], tuple_arity(node.left)
            where = f'{path.relative_to(root).as_posix()}:{node.lineno}  `... in {right.id}`'
            if got is not None and got != want:
                hits.append(f'{where}: LHS arity {got} vs element arity {want} -- CANNOT match')
            elif got is None and want >= 2 and isinstance(node.left, SCALARISH_LHS):
                hits.append(f'{where}: elements are {want}-tuples, LHS is a scalar-ish {type(node.left).__name__}')
    return hits


def dotted_name(path: Path, root: Path) -> str:
    """The module's REAL dotted name -- see :func:`layer_a` for why a synthetic one is a false zero."""
    rel = path.relative_to(root).with_suffix('')
    return '.'.join(rel.parts[1:] if rel.parts[0] == 'src' else rel.parts)


def import_for(path: Path, root: Path) -> ModuleType:
    """Import the module at `path`. Raises; both callers record the failure as its own finding."""
    return importlib.import_module(dotted_name(path, root))


def layer_a(paths: list[Path], root: Path, min_size: int) -> tuple[list[str], list[str], int]:
    """Import each module and size every module-level collection a check consumes.

    Hits are collections that are EMPTY, or smaller than `min_size`, while something checks them.
    Both confirmed instances sit in that window (one at 0, one at 2). Size alone is not a verdict --
    this is a triage list.
    """
    hits: list[str] = []
    failures: list[str] = []
    sized = 0
    sys.path.insert(0, str(root))  # so a namespace-package test tree resolves
    for path in paths:
        text = path.read_text(encoding='utf-8')
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        names = module_level_collections(tree)
        if not names:
            continue
        checks = consumed_by(tree)
        # Import under the module's REAL dotted name, not a synthetic one. Test trees are commonly
        # namespace packages and several test directories are real packages; a synthetic name breaks
        # both absolute and relative imports, which silently dropped the 29 richest modules -- i.e.
        # exactly where the confirmed instance lived -- on this sieve's own first run. A sieve that
        # skips its calibration target reports a false zero.
        try:
            module = import_for(path, root)
        except BaseException:  # noqa: BLE001 -- a module that will not import is a separate finding
            failures.append(f'{path.relative_to(root).as_posix()}: {traceback.format_exc(limit=0).strip()}')
            continue
        for name in names:
            value = getattr(module, name, None)
            if not isinstance(value, Sized) or isinstance(value, str | bytes):
                continue
            try:
                length = len(value)
            except TypeError:
                continue  # `Sized` is a duck-type claim, and some libraries' objects lie about it
            sized += 1
            kinds = checks.get(name, set())
            if not kinds or length >= min_size:
                continue
            if length == 0 and isinstance(value, MUTABLE_TYPES) and not isinstance(value, frozenset):
                continue  # an accumulator filled later -- not a guard. See the negative control.
            hits.append(
                f'{path.relative_to(root).as_posix()}  {name}: {type(value).__name__} '
                f'size {length}  <- {",".join(sorted(kinds))}'
            )
    return hits, failures, sized


# ---------------------------------------------------------------- layer C: call-site population


class ProbeInjector(ast.NodeTransformer):
    """Rewrite `X in GUARD` / `X not in GUARD` to call a probe that RECORDS X, then answers.

    Semantics are preserved exactly (the probe returns the same boolean), so the replayed body takes
    the same branches the real test does -- which is the point: the population is the values that
    REACH the test, not the values someone believed would.
    """

    def __init__(self, guards: set[str]) -> None:
        self.guards = guards
        self.sites: dict[str, int] = {}

    # `visit_Compare` is the ast.NodeVisitor dispatch name -- the spelling is the library's.
    def visit_Compare(self, node: ast.Compare) -> ast.expr:
        self.generic_visit(node)
        if len(node.ops) != 1 or not isinstance(node.ops[0], ast.In | ast.NotIn):
            return node
        right = node.comparators[0]
        if not isinstance(right, ast.Name) or right.id not in self.guards:
            return node
        key = f'{right.id}:{node.lineno}'
        self.sites[key] = node.lineno
        call = ast.Call(
            func=ast.Name(id='__probe__', ctx=ast.Load()),
            args=[ast.Constant(value=key), node.left, right],
            keywords=[],
        )
        out: ast.expr = call if isinstance(node.ops[0], ast.In) else ast.UnaryOp(op=ast.Not(), operand=call)
        return ast.fix_missing_locations(ast.copy_location(out, node))


def _decode_parametrize(dec: ast.expr, module: ModuleType) -> tuple[list[str], list[tuple]] | str:
    """One `parametrize` decorator -> (argnames, rows), or a STRING saying why there is no grid.

    A refusal is carried as its own reason text, never as a bare `None`: the reason is the entire
    value of an UNKNOWN, so it cannot be added without being said.

    Values are taken from the IMPORTED module, so the sieve re-reads the SAME object pytest would
    collect -- no re-derivation, hence nothing to drift from it.
    """
    if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
        return 'undecodable decorator'
    if dec.func.attr not in REPLAYABLE_MARKS:
        return f'mark `{dec.func.attr}`'
    if len(dec.args) < 2 or not isinstance(dec.args[0], ast.Constant):
        return 'undecodable parametrize'
    names = [n.strip() for n in str(dec.args[0].value).split(',')]
    try:
        values = eval(  # the expression already ran at import; this re-reads it
            compile(ast.Expression(body=dec.args[1]), '<sieve>', 'eval'), vars(module)
        )
        return names, [tuple(v) if len(names) > 1 else (v,) for v in _unwrap_params(values)]
    except Exception:  # noqa: BLE001 -- an unresolvable argvalues list is honestly UNKNOWN
        return 'unresolvable argvalues'


def parametrize_grid(func: ast.FunctionDef, module: ModuleType) -> list[dict[str, object]] | str:
    """Every argument combination the decorators declare, or a string saying why there is none."""
    grids: list[tuple[list[str], list[tuple]]] = []
    for dec in func.decorator_list:
        decoded = _decode_parametrize(dec, module)
        if isinstance(decoded, str):
            return decoded
        grids.append(decoded)
    if not grids:
        return 'not parametrized'
    params = {a.arg for a in (*func.args.posonlyargs, *func.args.args, *func.args.kwonlyargs)}
    declared = {n for names, _ in grids for n in names}
    if params - declared:
        return f'takes fixture(s) {sorted(params - declared)}'
    combos = []
    for picked in itertools.product(*[rows for _, rows in grids]):
        kwargs: dict[str, object] = {}
        for (names, _), row in zip(grids, picked, strict=True):
            kwargs.update(dict(zip(names, row, strict=False)))
        combos.append({k: v for k, v in kwargs.items() if k in params})
    return combos


def _unwrap_params(values: object) -> list:
    """`pytest.param(x, marks=...)` wraps its value; the population is the value, not the wrapper."""
    out = []
    for value in values:  # type: ignore[union-attr]
        inner = getattr(value, 'values', None)
        out.append(inner[0] if isinstance(inner, tuple) and len(inner) == 1 else value)
    return out


def _fingerprint(value: object) -> object:
    """A hashable stand-in for counting DISTINCT observations; falls back to repr, never to a guess."""
    try:
        hash(value)
    except TypeError:
        return f'<unhashable {value!r}>'
    return value


def layer_c(paths: list[Path], root: Path, budget_s: float, *, denylist: frozenset[str]) -> tuple[list[str], list[str]]:
    """Report every membership guard as `M of N` over the population that reaches its call site.

    ``denylist`` IS REQUIRED AND HAS NO DEFAULT. A replay is REAL EXECUTION, so which symbols make a
    body unsafe to run -- a CLI runner, a subprocess, a live vendor tool, anything that writes -- is
    a fact about the project being scanned, and no package can know it. A default would run somebody
    else's destructive test body while looking like a static analysis. Pass `frozenset()` only if
    you mean "nothing here is unsafe to execute"; a site touching a denylisted name becomes UNKNOWN
    rather than a number bought by running it.
    """
    lines: list[str] = []
    unknown: list[str] = []
    deadline = time.monotonic() + budget_s
    sys.path.insert(0, str(root))
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'))
        except (SyntaxError, UnicodeDecodeError):
            continue
        names = set(module_level_collections(tree))
        if not names or 'membership' not in {k for v in consumed_by(tree).values() for k in v}:
            continue
        try:
            module = import_for(path, root)
        except BaseException as exc:  # noqa: BLE001 -- an unimportable module is a finding, not a skip
            # Say so. A module dropped in SILENCE is precisely the false zero this sieve's own first
            # run produced (29 skipped modules reported as "0 candidates"). Class name only, so the
            # reason histogram groups; layer A prints the full traceback.
            unknown.append(
                f'{path.relative_to(root).as_posix()}  (whole module)  UNKNOWN (would not import: {type(exc).__name__})'
            )
            continue
        guards = {
            name
            for name in names
            if isinstance(getattr(module, name, None), Sized) and not isinstance(getattr(module, name), str | bytes)
        }
        if not guards:
            continue
        rel = path.relative_to(root).as_posix()
        for func in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
            # Strip the decorators FIRST. A membership test inside a `parametrize` argvalues
            # comprehension is part of BUILDING the population, not of consuming it -- and it is
            # never executed by the replay, so probing it yields a bogus `0 of 0`. Two real constants
            # scored exactly that before this existed. Report them as UNKNOWN.
            decorated = ProbeInjector(guards)
            for dec in func.decorator_list:
                decorated.visit(copy.deepcopy(dec))
            unknown.extend(f'{rel}  {k}  UNKNOWN (in a decorator, not the body)' for k in decorated.sites)

            probe_names = ProbeInjector(guards)
            probed = copy.deepcopy(func)  # deepcopy keeps the real line numbers
            probed.decorator_list = []
            probed = probe_names.visit(probed)
            if not probe_names.sites:
                continue
            if time.monotonic() > deadline:
                unknown.extend(f'{rel}  {k}  UNKNOWN (replay budget exhausted)' for k in probe_names.sites)
                continue
            bad = denylist & {n.id for n in ast.walk(func) if isinstance(n, ast.Name)}
            bad |= denylist & {n.attr for n in ast.walk(func) if isinstance(n, ast.Attribute)}
            if bad:
                unknown.extend(f'{rel}  {k}  UNKNOWN (denylisted: {",".join(sorted(bad))})' for k in probe_names.sites)
                continue
            combos = parametrize_grid(func, module)
            if isinstance(combos, str):
                unknown.extend(f'{rel}  {k}  UNKNOWN ({combos})' for k in probe_names.sites)
                continue
            seen: dict[str, list[object]] = {k: [] for k in probe_names.sites}
            matched: dict[str, int] = dict.fromkeys(probe_names.sites, 0)

            def probe(key: str, value: object, guard: object, _seen=seen, _matched=matched) -> bool:
                _seen[key].append(value)
                hit = value in guard  # type: ignore[operator]
                _matched[key] += bool(hit)
                return bool(hit)

            namespace = dict(vars(module))
            namespace['__probe__'] = probe
            try:
                exec(compile(ast.Module(body=[probed], type_ignores=[]), '<sieve-replay>', 'exec'), namespace)  # noqa: S102
                fn = namespace[func.name]
                # A test that FAILS still observed the values that reached the guard, so a raise is
                # not a reason to drop the parameter -- but the count travels with the number,
                # because "9 of 16, and the body raised on 16 of 16" is a different claim.
                raised = 0
                untried = 0
                for i, kwargs in enumerate(combos):
                    # The budget binds per COMBINATION, not just per function. Checking it only
                    # before the loop let one function with a large grid run the whole scan past the
                    # ceiling. Combinations not tried are REPORTED, never silently dropped: a partial
                    # replay is a smaller measurement, and the count says so.
                    if i and time.monotonic() > deadline:
                        untried = len(combos) - i
                        break
                    try:
                        fn(**kwargs)
                    # BaseException, not Exception: `pytest.skip`/`fail` raise OutcomeException,
                    # which derives from BaseException. Narrowing here would send every skipping test
                    # to the outer handler and turn a real measurement into UNKNOWN.
                    except BaseException:  # noqa: BLE001 -- a red/xfailed/skipped test still observed values
                        raised += 1
            except BaseException:  # noqa: BLE001 -- an unreplayable body is UNKNOWN, never a number
                unknown.extend(f'{rel}  {k}  UNKNOWN (replay raised)' for k in probe_names.sites)
                continue
            for key, values in seen.items():
                guard_name = key.split(':')[0]
                guard = getattr(module, guard_name)
                distinct = {_fingerprint(v) for v in values}
                # Iterating a dict yields its keys, which is exactly what `in` tests -- no special case.
                members = sum(1 for member in guard if _fingerprint(member) in distinct)
                cover = f', {members}/{len(guard)} guard members seen'
                # N == 0 is NOT "0 of N": the guard was never consulted, so the replay measured
                # nothing about it. A separate word, because it is a separate claim.
                flag = ''
                if not values:
                    flag = '  <== NEVER CONSULTED'
                elif matched[key] == 0:
                    flag = '  <== 0 OF N'
                noise = f', body raised on {raised}/{len(combos)} params' if raised else ''
                if untried:
                    noise += f', {untried}/{len(combos)} params UNTRIED (replay budget)'
                lines.append(
                    f'{rel}:{probe_names.sites[key]}  {guard_name}(size {len(guard)}): '
                    f'{matched[key]} of {len(values)}  [{len(distinct)} distinct{cover}{noise}]{flag}'
                )
    return lines, unknown


def render_report(
    paths: list[Path],
    root: Path,
    *,
    layers: str,
    min_size: int,
    budget_s: float,
    denylist: frozenset[str],
    show_unknown: bool = False,
) -> list[str]:
    """Run the requested layers and return the whole report as lines.

    THE COUNTS ARE THE REPORT, and the three layer-C headline numbers are deliberately separate
    claims that a single "findings" total would merge: sites with a MEASURED population, of those
    the ones intersecting the population in NOTHING, and of those the ones NEVER CONSULTED (N == 0).
    A guard that was never reached has not been shown to be vacuous -- it has not been measured at
    all -- and collapsing the two is how a scan reports a clean bill it did not earn.

    THE UNKNOWN COUNT IS PRINTED EVEN WHEN THE OTHER NUMBERS ARE GOOD, with its reason histogram,
    because UNKNOWN IS NOT A CLEAN RESULT: it is the population this scan could not produce, and a
    report that showed only the measured half would be the same instrument-that-lies shape the whole
    module exists to find.
    """
    lines = [f'python files scanned: {len(paths)}', '']

    if 'b' in layers:
        hits = layer_b(paths, root)
        lines.append(f'[B] membership tests whose LHS arity cannot match the collection: {len(hits)}')
        lines.extend(f'      {hit}' for hit in hits)
        lines.append('')

    if 'a' in layers:
        hits, failures, sized = layer_a(paths, root, min_size)
        lines.append(f'[A] module-level collections sized at import: {sized}')
        lines.append(f'[A] checked AND smaller than {min_size} (candidates):     {len(hits)}')
        lines.extend(f'      {hit}' for hit in hits)
        lines.append(f'[A] modules that would not import:            {len(failures)}')
        lines.extend(f'      {failure}' for failure in failures)
        lines.append('')

    if 'c' in layers:
        hits, unknown = layer_c(paths, root, budget_s, denylist=denylist)
        vacuous = [h for h in hits if h.endswith('0 OF N')]
        never = [h for h in hits if h.endswith('NEVER CONSULTED')]
        lines.append(f'[C] membership call sites with a MEASURED sample: {len(hits)}')
        lines.append(f'[C] of those, intersecting the sample in NOTHING: {len(vacuous)}')
        lines.append(f'[C] of those, never reached at all (N == 0):           {len(never)}')
        lines.extend(f'      {hit}' for hit in sorted(hits))
        lines.append(f'[C] sites whose sample is UNKNOWN (not a clean result): {len(unknown)}')
        reasons: dict[str, int] = {}
        for site in unknown:
            reasons[site.split('UNKNOWN ')[-1]] = reasons.get(site.split('UNKNOWN ')[-1], 0) + 1
        lines.extend(
            f'      {count:5d}  {reason}' for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1])[:12]
        )
        if show_unknown:
            lines.extend(f'      {site}' for site in sorted(unknown))
    return lines


def scan_paths(root: Path, roots: Sequence[str]) -> list[Path]:
    """Every `.py` under each of ``roots``, sorted, with caches excluded.

    ``roots`` HAS NO DEFAULT: which directories of a tree are worth scanning is that tree's layout,
    and a package guessing `src/` and `tests/` would silently scan nothing in a repo shaped
    differently -- reporting zero findings, which reads identically to a clean tree.
    """
    return sorted(p for d in roots for p in (root / d).rglob('*.py') if '__pycache__' not in p.parts)


# ---------------------------------------------------------------- the controls

#: THE WORD IN THE REPORT IS `sample`, NOT `population`, and the reason is a guard rather than a
#: preference: `test_job.py::TestTheSubstrateKnowsNothingAboutBARRIERS` refuses `population` as a
#: VALUE anywhere in this package, because a scheduler that learns the word has started modelling a
#: fan-out generation. The sense here is unrelated -- the values that reach a call site -- but the
#: guard scans strings, not senses, and weakening a working architectural check to keep one word
#: would be the wrong trade. Prose is exempt, so the docstrings above still say what they mean.
#:
#: THE SELF-TEST FIXTURE. The FIRST run of this sieve reported 0/0 on a real tree while silently
#: skipping the 29 modules that carried the matrices -- a false zero that looked exactly like a clean
#: result. Controls are how a null result becomes evidence rather than a hope.
#:
#: THE VENDOR NAMES ARE GONE FROM IT (2026-08-12) and nothing was lost: the original spelled one
#: project's FEA tools into the planted guards, and no layer here reads a name. What each control
#: exercises is a SHAPE, so the shapes are what it plants.
SELF_TEST_FIXTURE = """
import pytest

# POSITIVE 1 -- an empty guard something checks. Must be flagged by A.
_EMPTY_GUARD = frozenset()
# POSITIVE 2 -- the confirmed instance's exact shape: 2-tuples tested against a bare call.
_ARITY_GUARD = (('toola', 'default'), ('toola', 'remesh'))
# NEGATIVE 1 -- an accumulator, empty by construction, filled at runtime. Must NOT be flagged.
_CACHE = {}
# NEGATIVE 2 -- legitimately small AND correctly shaped. Must NOT be flagged by either layer.
_PAIR_ONLY_LEGS = (('toola', 'default'),)
# NEGATIVE 3 -- small, but nothing checks it. Not a guard, so not a claim.
_UNCHECKED = ('a',)

def uses(flow, axes):
    if flow.get('reference') not in _EMPTY_GUARD:
        return None
    if flow.get('reference') not in _ARITY_GUARD:   # bare str vs 2-tuples -- B must flag
        return None
    if (axes['vendor'], axes['method']) not in _PAIR_ONLY_LEGS:   # correct -- B must not flag
        return None
    _CACHE.setdefault(flow, 1)
    return True

@pytest.mark.parametrize('leg', _ARITY_GUARD)
def test_leg(leg):
    assert leg

# --- layer C controls -------------------------------------------------------------------------
_SAMPLED = ('a', 'b', 'c', 'd')
# POSITIVE 3 -- non-empty, correctly shaped, and intersects the sampled values in NOTHING. Invisible to
# both A (size 2) and B (arity 1 vs 1). C must report `0 of 4`.
_MATCHES_NOTHING = ('x', 'y')
# NEGATIVE 4 -- same size, same shape, and it does filter. C must report a non-zero count.
_MATCHES_SOME = ('a', 'y')

@pytest.mark.parametrize('item', _SAMPLED)
def test_sampled(item):
    if item in _MATCHES_NOTHING:
        return
    assert item in _MATCHES_SOME or item != 'a'

# NEGATIVE 5 -- a fixture means pytest, so C must refuse it as UNKNOWN rather than invent an N.
@pytest.mark.parametrize('item', _SAMPLED)
def test_needs_a_fixture(item, tmp_path):
    assert item not in _MATCHES_NOTHING or tmp_path
"""


def self_test(tmp_root: Path, *, denylist: frozenset[str] = frozenset()) -> list[tuple[str, bool]]:
    """Run the positive and negative controls. Returns ``[(label, passed)]`` for the caller to print.

    A LIST RATHER THAN A COUNT, and rather than printing here: a control that fails must say WHICH,
    and a package that wrote to stdout would decide the caller's report format for it.

    ``denylist`` defaults to EMPTY here and only here, because the fixture is this module's own
    source and is known safe to execute -- the argument exists so a caller can prove its real
    denylist does not accidentally disqualify the controls.
    """
    (tmp_root / 'tests').mkdir(parents=True, exist_ok=True)
    fixture = tmp_root / 'tests' / 'test_sieve_selftest_fixture.py'
    fixture.write_text(SELF_TEST_FIXTURE, encoding='utf-8')

    b_hits = layer_b([fixture], tmp_root)
    a_hits, failures, _sized = layer_a([fixture], tmp_root, min_size=1)
    c_hits, c_unknown = layer_c([fixture], tmp_root, budget_s=60.0, denylist=denylist)

    return [
        ('B flags the arity mismatch (_ARITY_GUARD)', any('_ARITY_GUARD' in h for h in b_hits)),
        ('B ignores the correct comparison (_PAIR_ONLY_LEGS)', not any('_PAIR_ONLY_LEGS' in h for h in b_hits)),
        ('A flags the empty guard (_EMPTY_GUARD)', any('_EMPTY_GUARD' in h for h in a_hits)),
        ('A ignores the accumulator (_CACHE)  [negative control]', not any('_CACHE' in h for h in a_hits)),
        ('A ignores the unchecked constant (_UNCHECKED)', not any('_UNCHECKED' in h for h in a_hits)),
        (
            'C reports the shape A and B are both blind to as 0 of 4 (_MATCHES_NOTHING)',
            any('_MATCHES_NOTHING(size 2): 0 of 4' in h for h in c_hits),
        ),
        (
            'C reports the filtering neighbour as non-zero (_MATCHES_SOME)  [negative control]',
            any(
                h.split('_MATCHES_SOME(size 2): ')[1].startswith(('1 ', '2 ', '3 ', '4 '))
                for h in c_hits
                if '_MATCHES_SOME(size 2): ' in h
            ),
        ),
        (
            'C refuses a fixture-taking test as UNKNOWN, not a number  [negative control]',
            any('takes fixture(s)' in u for u in c_unknown)
            # Both functions test `_MATCHES_NOTHING`; a wrongly-replayed fixture test would add a
            # SECOND reported site, so the COUNT is the discriminating assertion, not the presence.
            and sum('_MATCHES_NOTHING(size 2)' in h for h in c_hits) == 1,
        ),
        ('the fixture imported at all', not failures),
    ]
