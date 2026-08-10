"""Break each guarded property on purpose, and check that exactly one test notices.

WHY THIS IS A COMMITTED TOOL AND NOT A ONE-OFF. Revert-and-rerun is the sharpest check available
here, and it has a silent failure mode: a revert that does not actually revert reads exactly like a
test that does not discriminate, and the conclusion drawn is the opposite of the truth. It happened
three ways in one session --

  1. a `sed` whose escaping did not match, leaving the file untouched;
  2. a mutation applied to a source tree the tests never imported (no `pythonpath`, a non-editable
     install answering `import agent_swarm` instead);
  3. a mutation that was invalid Python, so the run reddened at COLLECTION and no test ever ran.

Each produced a confident wrong answer. So the three guards below are the point of this file, not
incidental to it: the pattern must be PRESENT before editing, the file must have CHANGED after, and
the module that answers is PRINTED rather than assumed. A `[GREEN]` line here is a real finding; a
`[SKIP-PATTERN-ABSENT]` line is not evidence in either direction and says so.

Run:  python tools/mutation_sweep.py
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = Path(r'C:\Users\linxu\Documents\PEMC\motronics-studio\.venv\Scripts\python.exe')

# (label, relative file, old, new, test file to run)
MUTATIONS = [
    (
        'in-process _item_numbers cache goes INERT',
        'src/agent_swarm/forge_store.py',
        'cached = self._item_numbers.get(title)',
        'cached = None',
        'tests/test_cost_axes.py',
    ),
    (
        'on-disk INDEX goes inert (register stops remembering)',
        'src/agent_swarm/forge_store.py',
        '        self._remember(job, number)\n        return number',
        '        return number',
        'tests/test_cost_axes.py tests/test_item_index.py tests/test_forge_store.py',
    ),
    (
        '_label_ids cache goes INERT',
        'src/agent_swarm/forge.py',
        'cached = self._label_ids.get(name)',
        'cached = None',
        'tests/test_cost_axes.py',
    ),
    (
        'credential cache goes INERT (shells out every time)',
        'src/agent_swarm/forge.py',
        'if self._token is not None:',
        'if False:',
        'tests/test_cost_axes.py',
    ),
    (
        'credential seam INLINED (cache becomes unobservable)',
        'src/agent_swarm/forge.py',
        'def _run_credential_helper(self',
        'def _inlined_credential_helper(self',
        'tests/test_cost_axes.py tests/test_provenance.py',
    ),
    (
        'a log call INTERPOLATES a credential-bearing name',
        'src/agent_swarm/forge.py',
        "self._token = line[len('password=') :]",
        "self._token = line[len('password=') :]\n                _ = f'token {self._token}'",
        'tests/test_provenance.py',
    ),
    (
        'the credential helper is run with check=True',
        'src/agent_swarm/forge.py',
        'check=False,',
        'check=True,',
        'tests/test_provenance.py',
    ),
    (
        'a raw accessor appears beside the redacted one',
        'src/agent_swarm/provenance.py',
        # A FUNCTION, not a dataclass field. Inserting a defaulted field ahead of a non-defaulted
        # one is invalid Python, so that version reddened at collection with no test ever running --
        # a mutation that "works" by breaking the import proves nothing.
        'def redact_url_credentials',
        "def raw_direct_url_text(path) -> str:\n    return (path / 'direct_url.json').read_text(encoding='utf-8')\n\n\ndef redact_url_credentials",
        'tests/test_provenance.py',
    ),
    (
        'redaction: the COLON discriminator is dropped',
        'src/agent_swarm/provenance.py',
        're.compile(r\'(//)[^/@\\s"]*:[^/@\\s"]*@\')',
        "re.compile(r'(//)[^/\\s\"]*@')",
        'tests/test_provenance.py',
    ),
    (
        'redaction: the match becomes GREEDY (unbounded)',
        'src/agent_swarm/provenance.py',
        're.compile(r\'(//)[^/@\\s"]*:[^/@\\s"]*@\')',
        "re.compile(r'(//).*:.*@')",
        'tests/test_provenance.py',
    ),
    (
        'the timing test stops CALLING running_provenance',
        'tests/test_end_to_end.py',
        "print(f'  {running_provenance()}')",
        "print('  (no provenance)')",
        'tests/test_provenance.py',
    ),
    (
        'RecordingForge.retire only CLOSES, never retitles (a gentle double)',
        'tests/test_forge_store.py',
        "suffix = '' if current.title.endswith(' (retired)') else ' (retired)'",
        "suffix = ''",
        'tests/test_forge_store.py tests/test_store.py',
    ),
]


def run(files: str) -> tuple[int, str]:
    proc = subprocess.run(
        [str(PY), '-m', 'pytest', *files.split(), '-q', '-p', 'no:cacheprovider', '--tb=no', '-rf', '--color=no'],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    failed = sorted(
        {line.split(' ')[1].split('::')[-1] for line in proc.stdout.splitlines() if line.startswith('FAILED')}
    )
    return proc.returncode, '\n'.join(f'      - {f}' for f in failed) or '      (none)'


def main() -> None:
    # WHICH TREE ANSWERS -- and it must be asked the way PYTEST asks it. A bare `python -c` does not
    # get the `pythonpath` from `[tool.pytest.ini_options]`, so the first version of this line
    # printed site-packages while the tests were reading `src/`: the tool reporting the very defect
    # it exists to catch, about itself. Asked here through a real pytest session instead.
    probe = ROOT / 'tests' / 'test_zz_which_tree_answers.py'
    probe.write_text(
        'import agent_swarm.forge_store as m\n\n\ndef test_print_module():\n    print(m.__file__)\n',
        encoding='utf-8',
    )
    try:
        which = subprocess.run(
            [str(PY), '-m', 'pytest', str(probe), '-q', '-s', '-p', 'no:cacheprovider', '--color=no'],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        probe.unlink(missing_ok=True)
    module = next((line.strip() for line in which.stdout.splitlines() if 'forge_store.py' in line), '')
    print(f'MODULE UNDER TEST: {module}\n')
    if not module.startswith(str(ROOT / 'src')):
        raise SystemExit(
            f'REFUSING TO SWEEP: the tests import {module or "<unknown>"}, not this tree. Every\n'
            f'result would be a verdict about that other copy -- which is how a whole night of\n'
            f'"sabotage-verified" claims came to mean nothing. Fix the import path first.'
        )

    for label, rel, old, new, files in MUTATIONS:
        path = ROOT / rel
        backup = path.read_text(encoding='utf-8')
        if old not in backup:
            print(f'[SKIP-PATTERN-ABSENT] {label}\n      pattern not found in {rel} -- NOT evidence')
            continue
        path.write_text(backup.replace(old, new, 1), encoding='utf-8')
        assert path.read_text(encoding='utf-8') != backup, 'mutation did not land'
        try:
            code, failed = run(files)
        finally:
            path.write_text(backup, encoding='utf-8')
        verdict = 'RED (survives)' if code != 0 else 'GREEN -- NOT DISCRIMINATED'
        print(f'[{verdict}] {label}\n{failed}')


if __name__ == '__main__':
    main()
