"""The install must be a COPY, not a live view of this working tree.

THE HAZARD, in one sentence: `agent-swarm` is a dependency of motronics' gate, and no motronics
verdict records which version of it decided the result. An editable install would make that worse in
the sharpest possible way -- every uncommitted edit in this repo would be live inside every gate on
that box, and a verdict would be decided partly by code that exists in no commit.

**THIS IS A CROSS-REPO ASSERTION AND IT MUST BE READ AS ONE.** It reaches out of this repository and
inspects a DIFFERENT project's interpreter. It is not "the venv is safe"; it is exactly one claim
about exactly one directory, and the reasons that matters are below.

**IT DOES NOT COVER THE FAILURE WE ACTUALLY HIT.** On 2026-08-10 an install landed in that
interpreter while a gate was starting. It was a NON-editable install from a committed sha -- the
correct kind -- and it was still enough to change the interpreter under a running verdict. Refusing
editable installs closes one door in a room with two. The other door is TIMING, and nothing here
closes it: that needs an install-time interlock, or the provenance line in the gate log that
lane-transport is adding. Without this paragraph the test reads as a guarantee of something it does
not check, which would make it worse than absent.
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

import pytest

from agent_swarm.provenance import Provenance, read_provenance, redact_url_credentials, running_provenance

#: The interpreter every gate on this box runs under. A LITERAL PATH, not discovery: the whole point
#: is to assert about one specific environment, and a check that searched for "some venv" would pass
#: by finding a different one.
MOTRONICS_SITE_PACKAGES = Path('C:/Users/linxu/Documents/PEMC/motronics-studio/.venv/Lib/site-packages')


def _write_dist_info(root: Path, *, editable: bool | None, version: str = '0.1.0') -> Path:
    dist_info = root / f'agent_swarm-{version}.dist-info'
    dist_info.mkdir(parents=True)
    if editable is not None:
        payload = {'dir_info': {'editable': editable}, 'url': 'file:///somewhere/agent-swarm'}
        (dist_info / 'direct_url.json').write_text(json.dumps(payload), encoding='utf-8')
    return dist_info


class TestReadingAnInstallsOrigin:
    """Unit-tested against fabricated dist-info, so the checker itself is not taken on trust."""

    def test_an_editable_install_is_reported_as_editable(self, tmp_path):
        _write_dist_info(tmp_path, editable=True)
        found = read_provenance(tmp_path)
        assert found is not None
        assert found.editable is True

    def test_a_copy_install_is_reported_as_a_copy(self, tmp_path):
        _write_dist_info(tmp_path, editable=False)
        assert read_provenance(tmp_path).editable is False

    def test_NOT_INSTALLED_is_None_and_must_not_read_as_fine(self, tmp_path):
        """A source checkout running with `PYTHONPATH=src` has no dist-info at all. `None` is
        "absent", and the caller decides whether absent is acceptable where it stands -- this
        function must not decide that for it.
        """
        assert read_provenance(tmp_path) is None

    def test_an_install_with_NO_direct_url_records_nothing(self, tmp_path):
        """A wheel from an index has no `direct_url.json`. That is not editable, and it is also not
        provenance -- `recorded` is what tells the two apart.
        """
        _write_dist_info(tmp_path, editable=None)
        found = read_provenance(tmp_path)
        assert found.editable is False
        assert found.recorded is False

    def test_a_CORRUPT_direct_url_is_treated_as_EDITABLE(self, tmp_path):
        """The one answer this must never give is "corrupt provenance, therefore clean install".
        Unreadable is not evidence of safety, so it fails toward the refusal.
        """
        dist_info = _write_dist_info(tmp_path, editable=False)
        (dist_info / 'direct_url.json').write_text('{"dir_info": ', encoding='utf-8')
        assert read_provenance(tmp_path).editable is True

    def test_the_text_is_kept_NEAR_VERBATIM_for_a_gate_log(self, tmp_path):
        """`__version__` is `0.1.0` on every commit so far and distinguishes nothing, so a gate log
        must print what pip wrote -- a summary is a place for a bug to hide, and the line itself is
        what another engineer can compare against their own box.

        NEAR-verbatim, not verbatim: the one edit is the credential redaction, and it is not
        optional. See `TestTheProvenanceLineCannotCarryACredential` for why those two requirements
        conflict and why removing exactly the credential settles it -- the credential is also the
        only part of the URL that carries no comparison value.
        """
        _write_dist_info(tmp_path, editable=False)
        text = read_provenance(tmp_path).direct_url_text
        assert text is not None
        assert json.loads(text)['url'].startswith('file://')


class TestTheGateInterpreterHoldsACopy:
    """The cross-repo assertion. One claim, one directory -- see this module's docstring for what it
    deliberately does NOT cover."""

    @pytest.mark.skipif(
        not MOTRONICS_SITE_PACKAGES.is_dir(),
        reason='the motronics venv is not on this box; the claim is about that interpreter and no other',
    )
    def test_agent_swarm_is_not_installed_EDITABLE_there(self):
        """An editable install would put every uncommitted edit in this repo inside every gate on
        that box, and a verdict would then be decided partly by code that exists in no commit and
        can be reproduced by nobody.

        NOT INSTALLED IS A PASS, and deliberately so: this asserts "not editable", not "present".
        Whether it should be installed at all is the other project's decision, and a test that
        demanded presence would fail on every box that does not run gates.
        """
        found = read_provenance(MOTRONICS_SITE_PACKAGES)
        if found is None:
            pytest.skip('agent_swarm is not installed in that interpreter; nothing to be editable')
        assert isinstance(found, Provenance)
        assert not found.editable, (
            f'agent_swarm is installed EDITABLE at {found.dist_info}, so uncommitted edits in this '
            f'working tree are live inside every gate on this box. Reinstall from a committed sha: '
            f'pip install --no-deps <copy of `git archive <sha>`>. direct_url.json says: '
            f'{found.direct_url_text}'
        )


class TestATimingCannotBeQuotedWithoutItsProvenance:
    """`running_provenance()` exists so a measured number cannot be printed without saying where its
    code came from. The rule it replaces -- "always quote the two together" -- is one a person has
    to remember, and this suite has already produced one figure that travelled without it."""

    def test_it_names_the_source_tree_and_its_sha_when_run_from_a_checkout(self):
        line = running_provenance()
        assert 'agent_swarm' in line
        assert 'SOURCE' in line or 'INSTALLED' in line

    def test_it_says_DIRTY_when_the_tree_has_uncommitted_changes(self):
        """The state in which a figure is unreproducible by anyone else -- and therefore the one it
        must never be quoted from silently.
        """
        line = running_provenance()
        if 'SOURCE' not in line:
            pytest.skip('this interpreter runs an installed copy, so there is no working tree to be dirty')
        dirty = subprocess.run(
            ['git', '-C', str(Path(__file__).resolve().parent.parent), 'status', '--porcelain'],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        assert ('DIRTY' in line) is bool(dirty)

    def test_the_timing_test_actually_CALLS_it(self):
        """Otherwise this module is a capability nobody uses, and the discipline is back to being a
        thing someone remembers.
        """
        source = (Path(__file__).resolve().parent / 'test_end_to_end.py').read_text(encoding='utf-8')
        assert 'running_provenance()' in source


class TestNoCredentialCanReachAReportOrALog:
    """The project invariant -- never log keys, tokens or fingerprints -- checked at the seam that
    was just extracted, because counting calls means handling the thing being counted.

    MEASURED, not assumed. With a raise forced into `_credential` immediately after the helper
    returns (so the real 858-character token was live in that frame), the token appeared ZERO times
    in the pytest report under `-q`, under `--showlocals`, and under `--showlocals --tb=long`.

    THE EMPIRICAL RESULT IS NOT THE GUARD, THOUGH. It is one pytest version under three flag
    combinations, and the dangerous path renders only when something has already gone wrong -- so it
    is never exercised by a green run and would drift unnoticed. The assertions below are structural
    instead: no credential-bearing name may be interpolated into any string in `forge.py`.
    """

    CREDENTIAL_NAMES = ('filled', '_token', 'password', 'token')

    def test_no_credential_bearing_name_is_INTERPOLATED_into_any_string(self):
        """An f-string is how a secret reaches a log, a report or an exception message, and the
        exception message is the one that bites: it renders only on the failure path, so a green
        suite says nothing about it.
        """
        source = (Path(__file__).resolve().parents[1] / 'src' / 'agent_swarm' / 'forge.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            for part in ast.walk(node):
                if isinstance(part, ast.Name) and part.id in self.CREDENTIAL_NAMES:
                    offenders.append(f'line {node.lineno}: {part.id}')
                if isinstance(part, ast.Attribute) and part.attr in self.CREDENTIAL_NAMES:
                    offenders.append(f'line {node.lineno}: .{part.attr}')
        assert not offenders, f'a credential-bearing name is formatted into a string: {offenders}'

    def test_the_helper_is_never_run_with_check_TRUE(self):
        """`subprocess.run(..., check=True)` raises `CalledProcessError`, whose repr carries stdout
        -- which for this helper IS the credential. `check=False` is load-bearing, not a style
        choice, and nothing else in the file says so.
        """
        source = (Path(__file__).resolve().parents[1] / 'src' / 'agent_swarm' / 'forge.py').read_text(encoding='utf-8')
        assert 'check=True' not in source

    def test_the_API_error_echoes_the_RESPONSE_never_the_request(self):
        """The request carries the `Authorization` header; the response body is what explains a
        refusal. Echoing the wrong one would put the token in every failed-call message.
        """
        source = (Path(__file__).resolve().parents[1] / 'src' / 'agent_swarm' / 'forge.py').read_text(encoding='utf-8')
        message_line = next(line for line in source.splitlines() if 'exc.read()' in line)
        assert 'request' not in message_line


class TestTheProvenanceLineCannotCarryACredential:
    """The printing surface widened when `running_provenance()` started printing on EVERY timing,
    and this is what that widening exposed.

    `pip install https://<token>@github.com/...` records the token in `direct_url.json`. Printing
    that file verbatim -- which is exactly what a gate log wants -- would put a credential in every
    timed run. Our current install is a `file://` path and carries none, so nothing has leaked; the
    CODE would have, which is the distinction between an incident and a defect.
    """

    def test_a_token_in_the_install_URL_is_REDACTED(self, tmp_path):
        dist_info = tmp_path / 'agent_swarm-0.1.0.dist-info'
        dist_info.mkdir()
        (dist_info / 'direct_url.json').write_text(
            '{"url": "https://oauth2:ghp_SUPERSECRET@github.com/DawnEver/agent-swarm.git", "dir_info": {}}',
            encoding='utf-8',
        )
        text = read_provenance(tmp_path).direct_url_text
        assert 'ghp_SUPERSECRET' not in text
        assert '<redacted>' in text

    def test_everything_ELSE_about_the_url_survives(self):
        """The redaction must not cost the line its comparison value -- which host, which path,
        which sha is the entire reason a gate log prints it.
        """
        redacted = redact_url_credentials('{"url": "https://oauth2:ghp_X@github.com/o/r.git"}')
        assert 'github.com/o/r.git' in redacted
        assert redacted.startswith('{"url": "https://')

    def test_a_url_with_NO_credential_is_untouched(self):
        plain = '{"url":"file:///C:/Users/linxu/Documents/PEMC/.pinned/agent-swarm-67c7aea","dir_info":{}}'
        assert redact_url_credentials(plain) == plain

    def test_it_stays_valid_JSON_so_a_reader_can_still_parse_it(self):
        text = redact_url_credentials('{"url": "https://u:p@h/x.git", "dir_info": {}}')
        assert json.loads(text)['url'] == 'https://<redacted>@h/x.git'

    def test_there_is_NO_raw_accessor_beside_the_redacted_one(self):
        """A `raw_direct_url_text` would be the one someone reached for. The unredacted file is on
        disk for anyone who genuinely needs it; nothing in this package hands it to a printer.
        """
        assert not hasattr(Provenance, 'raw_direct_url_text')
        # CODE, not prose. The docstring MENTIONS that accessor in order to explain why it does not
        # exist, and a substring check read that explanation as the offence -- which is exactly the
        # mistake the AST checks elsewhere in this suite were written to avoid, made by me, in the
        # file about not making it.
        source = (Path(__file__).resolve().parents[1] / 'src' / 'agent_swarm' / 'provenance.py').read_text(
            encoding='utf-8'
        )
        defined = {node.name for node in ast.walk(ast.parse(source)) if isinstance(node, ast.FunctionDef)} | {
            node.target.id
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        }
        assert not any('raw' in name for name in defined), f'a raw accessor exists: {sorted(defined)}'
