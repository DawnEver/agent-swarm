"""Hook mechanics: run the formatting hooks before staging, and ask whether the hooks EXIST at all.

EXTRACTED FROM motronics' `scripts/repo/precommit_fix.py`, 2026-08-12. What stayed behind is the
half that reads that project's gate-verdict format; everything here is any repository's.

WHY THIS EXISTS. Measured over two days (2026-07-26/27): **42 commit-retry rounds**. Not one of them
was a bad commit. `pre-commit`'s `ruff-format` hook does what it is designed to do -- rewrite the
file and exit 1 -- and `git commit` does what IT is designed to do: abort. The result is a two-step
dance on every single commit, and the cost is entirely in the interaction, not in either tool.

The fix is ordering, not configuration: **format BEFORE staging, never discover it during
committing.** This runs the hooks over the staged set, restages exactly what they touched, and
reports what changed -- so the commit that follows is a first attempt.

Deliberately NOT a `git commit` wrapper. The commit message and its trailers are the author's
business, and a wrapper that owned them would be a second place where commit policy lives.

THE SECOND HALF -- `hook_installation()` -- asks the question everything above assumes the answer to:
**are the hooks this repository DECLARES actually installed in the hooks directory git will consult?**
A `.pre-commit-config.yaml` is a declaration, and `pre-commit install` is a separate act on a
separate machine; nothing links the two. Measured 2026-08-12 across two checkouts of two different
projects, both with pre-push hooks declared: ZERO non-sample hook files on disk in either. Every push
from either machine had run none of them.

That is the worst shape a declaration-that-lies takes -- a lie about a GUARD'S SCOPE. A lie about
behaviour makes you distrust the code; a lie about scope makes you route AROUND a check you believe
is watching, which is strictly worse than having no check. So this reports the two failure modes
SEPARATELY: *declared-but-absent* (nothing is there) and *present-but-stale* (something is there but
does not point at this configuration). Collapsing them lets a checkout that ran `pre-commit install`
once, years ago, against a config that has since grown three stages, read as protected.

And it is THREE-VALUED at the top: a repository with no `.pre-commit-config.yaml` declares nothing,
which is not the same finding as one that declares five hooks and installed none of them. A file this
module cannot parse is a THIRD thing again and RAISES -- reporting it as "nothing declared" would be
the same collapse one level up.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Resolved, not spelled: a partial executable path is refused by linters, and a missing tool is an
# ERROR here rather than a silent skip -- a formatter that did not run looks exactly like one that
# found nothing to do.
_GIT = shutil.which('git')

# `pre-commit` is NOT resolved from PATH: git invokes it through the venv's hook shim, so on some
# boxes `shutil.which('pre-commit')` is None while the hooks work perfectly -- measured, and it made
# this refuse to run on the very tree whose hooks it was written to absorb. Running it as a MODULE on
# the interpreter already executing this file cannot pick a different environment than the caller's.
_PRE_COMMIT = (sys.executable, '-m', 'pre_commit')


def git(*args: str) -> str:
    """`git <args>` in the current directory, stdout only. A failure reads as empty output."""
    assert _GIT, 'git is required'
    return subprocess.run([_GIT, *args], capture_output=True, text=True, check=False).stdout


def staged_files() -> list[str]:
    """Repo-relative paths in the index."""
    return [line for line in git('diff', '--cached', '--name-only').splitlines() if line.strip()]


def repo_root() -> Path:
    """The toplevel of the checkout this is running in."""
    return Path(git('rev-parse', '--show-toplevel').strip() or '.')


def format_and_restage(*, all_files: bool, files: list[str]) -> int:
    """Run the hooks, restage what they rewrote, and re-run. Returns the exit code to report.

    Exit 0 = the tree is clean for the hooks (whether or not anything was rewritten). Exit 1 = a hook
    FAILED for a reason formatting cannot fix (a real lint error) -- that one is the author's to read.
    """
    scope = ['--all-files'] if all_files else ['--files', *files]
    # `pre-commit run` exits 1 when a hook MODIFIED a file, which is the case this exists to absorb,
    # and also when a hook genuinely failed. The two are told apart below by asking git what actually
    # changed -- not by parsing the hook output, which is prose and would rot.
    proc = subprocess.run([*_PRE_COMMIT, 'run', *scope], capture_output=True, text=True, check=False)
    sys.stdout.write(proc.stdout)
    sys.stdout.write(proc.stderr)

    rewritten = [line for line in git('diff', '--name-only').splitlines() if line.strip()]
    if rewritten:
        assert _GIT, 'git is required'
        subprocess.run([_GIT, 'add', '--', *rewritten], check=False)
        sys.stdout.write(f'[precommit-fix] restaged {len(rewritten)} file(s) the hooks rewrote:\n')
        for path in rewritten:
            sys.stdout.write(f'  {path}\n')

    # A hook can fail for a reason no rewrite fixes (a lint RULE, a syntax error). If the hooks are
    # unhappy AND nothing was rewritten, this is not the retry loop -- it is a real finding, and
    # swallowing it here would turn a red into a green commit.
    still = subprocess.run([*_PRE_COMMIT, 'run', *scope], capture_output=True, text=True, check=False)
    if still.returncode != 0:
        sys.stdout.write(still.stdout)
        sys.stderr.write('[precommit-fix] a hook still fails after formatting -- a real error, not a rewrite\n')
        return 1
    sys.stdout.write('[precommit-fix] hooks clean; `git commit` will not be rewritten out from under you\n')
    return 0


# ---------------------------------------------------------------------------------------------
# Is what this repository DECLARES actually installed?
# ---------------------------------------------------------------------------------------------

#: The verdict for one stage. Four values, and the middle two are the point of the whole module.
INSTALLED = 'installed'
ABSENT = 'declared-but-absent'
STALE = 'present-but-stale'
FOREIGN = 'present-but-not-pre-commits'

#: The three-valued top answer. NOTHING_DECLARED is not a pass and not a failure -- it is the
#: absence of a question, and a caller that wants a configuration to EXIST must say so itself.
PROTECTED = 'protected'
UNPROTECTED = 'unprotected'
NOTHING_DECLARED = 'nothing-declared'

#: pre-commit stamps every file it generates with this line. Its absence means somebody else's hook
#: is sitting in that slot -- a different finding from "stale", because re-installing would DELETE
#: a hand-written hook rather than refresh a managed one.
_GENERATED_MARKER = 'File generated by pre-commit'

#: pre-commit's own deprecated stage spellings. A configuration using them installs the same hook
#: TYPE, so reading them as distinct stages would report a hook absent that is in fact present.
_STAGE_ALIASES = {'commit': 'pre-commit', 'push': 'pre-push', 'merge-commit': 'pre-merge-commit'}

#: A hook entry naming no `stages:` runs at pre-commit -- pre-commit's own default.
_DEFAULT_STAGE = 'pre-commit'

#: The two arguments the generated hook carries in its templated `ARGS=(...)` line.
_ARG = re.compile(r'--(config|hook-type)=([^\s)]+)')


@dataclass(frozen=True)
class StageReport:
    """One git hook type: what the configuration declares for it, and what is on disk."""

    stage: str
    hook_ids: tuple[str, ...]
    status: str
    detail: str
    path: Path


@dataclass(frozen=True)
class InstallReport:
    """Every declared stage of one repository, plus the three-valued top answer."""

    repo: Path
    hooks_dir: Path
    config: Path | None
    stages: tuple[StageReport, ...]

    @property
    def verdict(self) -> str:
        if self.config is None:
            return NOTHING_DECLARED
        return PROTECTED if all(stage.status == INSTALLED for stage in self.stages) else UNPROTECTED

    @property
    def failing(self) -> tuple[StageReport, ...]:
        """Declared stages whose hook is absent, stale, or somebody else's."""
        return tuple(stage for stage in self.stages if stage.status != INSTALLED)


def hooks_dir(repo: Path) -> Path:
    """The directory git will ACTUALLY consult for hooks in ``repo``.

    Asked of git, never string-built. `core.hooksPath` redirects it repository-wide, and a worktree
    resolves to the main checkout's single shared directory rather than getting one of its own -- so
    a guard that appends ``.git/hooks`` to a path answers about a directory git is not using, and
    from a worktree it answers about a directory that does not exist at all.
    """
    out = subprocess.run(
        [_GIT or 'git', '-C', str(repo), 'rev-parse', '--git-path', 'hooks'],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.strip()
    # `--git-path` answers relative to the repository when it can, absolute when it cannot.
    return Path(out) if Path(out).is_absolute() else (repo / out)


def declared_stages(config: Path) -> dict[str, tuple[str, ...]]:
    """Map git hook type -> the hook ids ``config`` declares for it.

    SCOPE OF THIS PARSER, stated because a reader would otherwise supply "it reads YAML": it reads
    the `id:` and `stages:` keys of the block-sequence form a `.pre-commit-config.yaml` is written
    in, which is the only form pre-commit's own documentation uses. It does NOT implement YAML --
    no anchors, no multi-document streams, no flow mappings. Rather than guess at a file it does not
    understand it RAISES: a configuration whose `repos:` skeleton is missing, or that yields no hook
    id at all, is an UNREAD file, and reporting one as "declares nothing" would be the same
    two-things-as-one collapse this section exists to refuse, one level up.
    """
    raw = config.read_text(encoding='utf-8').splitlines()
    lines = ['' if line.lstrip().startswith('#') else line.rstrip() for line in raw]

    if not any(line.startswith('repos:') for line in lines):
        raise ValueError(f'{config}: no top-level `repos:` -- not a pre-commit configuration this can read')

    # (hook id, its declared stages). Accumulated first, resolved to hook types second, so the
    # scanner has one job and the stage-defaulting rule lives in one readable place.
    hooks: list[tuple[str, list[str]]] = []
    open_indent = -1

    for line in lines:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        # A new list item at or left of the current hook's indent ends it -- the next hook, or the
        # next `- repo:` block entirely.
        if hooks and stripped.startswith('- ') and indent <= open_indent:
            open_indent = -1
        if stripped.startswith('- id:'):
            hooks.append((stripped.split(':', 1)[1].strip(), []))
            open_indent = indent
        elif open_indent >= 0 and stripped.startswith('stages:'):
            # `stages: [a, b]`. The block form (`stages:` then `- a` on following lines) is written
            # by neither configuration this serves; it leaves the list empty, so the hook falls to
            # the default stage. Widening the parser to a form nothing writes would put untested
            # code between a guard and its answer.
            value = stripped.split(':', 1)[1].strip()
            hooks[-1][1][:] = [item.strip().strip('\'"') for item in value.strip('[]').split(',') if item.strip()]

    by_stage: dict[str, list[str]] = {}
    for hook_id, stages in hooks:
        for stage in stages or [_DEFAULT_STAGE]:
            by_stage.setdefault(_STAGE_ALIASES.get(stage, stage), []).append(hook_id)

    if not by_stage:
        raise ValueError(f'{config}: parsed no hook ids -- refusing to report an unread file as an empty one')
    return {stage: tuple(ids) for stage, ids in sorted(by_stage.items())}


def _inspect(path: Path, *, stage: str, config_name: str) -> tuple[str, str]:
    """Classify one hook file on disk. Returns ``(status, detail)``."""
    if not path.exists():
        return ABSENT, 'no file at this path -- this hook has never been installed, or was removed'
    text = path.read_text(encoding='utf-8', errors='ignore')
    if _GENERATED_MARKER not in text:
        return FOREIGN, 'a hook file exists but pre-commit did not generate it; installing would overwrite it'
    args = dict(_ARG.findall(text))
    if 'config' not in args:
        return STALE, 'generated with no --config argument, so it cannot be shown to point at this configuration'
    if Path(args['config']).name != config_name:
        return STALE, f'points at {args["config"]}, not {config_name}'
    if args.get('hook-type') != stage:
        return STALE, f'installed as --hook-type={args.get("hook-type")!r} while occupying the {stage} slot'
    return INSTALLED, f'pre-commit, --config={args["config"]} --hook-type={stage}'


def hook_installation(repo: Path, *, config_name: str = '.pre-commit-config.yaml') -> InstallReport:
    """Answer, for ``repo``, whether every stage its configuration declares has a live hook file."""
    directory = hooks_dir(repo)
    config = repo / config_name
    if not config.exists():
        return InstallReport(repo=repo, hooks_dir=directory, config=None, stages=())
    reports = []
    for stage, ids in declared_stages(config).items():
        path = directory / stage
        status, detail = _inspect(path, stage=stage, config_name=config_name)
        reports.append(StageReport(stage=stage, hook_ids=ids, status=status, detail=detail, path=path))
    return InstallReport(repo=repo, hooks_dir=directory, config=config, stages=tuple(reports))


def report_installation(argv: list[str] | None = None) -> int:
    """CLI. Exit 0 = every declared hook is live, 1 = at least one is not, 2 = nothing is declared.

    THREE EXIT CODES BECAUSE THERE ARE THREE ANSWERS. Folding 2 into 0 would let a repository that
    lost its configuration report exactly as reassuringly as one that is fully installed -- the
    failure this section is about, one level up.
    """
    parser = argparse.ArgumentParser(description='Are the hooks this repository declares installed?')
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--config-name', default='.pre-commit-config.yaml')
    args = parser.parse_args(argv)

    report = hook_installation(args.repo.resolve(), config_name=args.config_name)
    sys.stdout.write(f'[hooks-installed] the hooks directory git will use: {report.hooks_dir}\n')
    if report.verdict == NOTHING_DECLARED:
        sys.stdout.write(f'[hooks-installed] NOTHING DECLARED -- no {args.config_name} in {report.repo}\n')
        return 2
    for stage in report.stages:
        sys.stdout.write(f'  {stage.stage:<18} {stage.status:<28} {stage.detail}\n')
        sys.stdout.write(f'  {"":<18} declares: {", ".join(stage.hook_ids)}\n')
    if report.verdict == PROTECTED:
        sys.stdout.write('[hooks-installed] OK -- every declared stage has a live hook.\n')
        return 0
    install = ' '.join(f'-t {stage.stage}' for stage in report.stages)
    sys.stdout.write(
        f'[hooks-installed] UNPROTECTED -- {len(report.failing)} of {len(report.stages)} declared stage(s) '
        f'are not live. Every commit or push through this checkout ran none of them.\n'
        f'  Install with:  pre-commit install --install-hooks {install}\n'
    )
    return 1


if __name__ == '__main__':
    raise SystemExit(report_installation())
