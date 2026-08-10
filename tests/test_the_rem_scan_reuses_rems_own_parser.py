"""`scan` reads real finding files through rem's OWN library, and refuses to guess when it cannot.

WHY NOT REIMPLEMENT THE PARSE. The finding format -- `MANUAL-<date>-<n>` / `SR-<n>` ids, the
checkbox, the bracketed severity, the module headings, the trailing date -- lives in one regex in
rem's `task-lib.mjs`. A second copy here would be a duplicated scheme, which is the defect class
this project names explicitly, and the failure mode is the quiet one: rem changes its format, this
copy keeps parsing the old one, and the drift report goes green because it found nothing.

So `scan` shells out to that library and reads JSON back. These tests run the REAL shim against REAL
files on disk -- a double would only prove that a mock returns what it was told to.

THE ENCODING CASE IS NOT HYPOTHETICAL. Measured 2026-08-10 on the first real scan: `text=True`
decodes with the locale codec, GBK on this box, and this project's findings are routinely written in
Chinese. It failed inside subprocess's reader THREAD, so `stdout` came back `None` and the traceback
named a JSON type error thirty lines from the cause.
"""

from __future__ import annotations

import shutil

import pytest

from agent_swarm.rem_bridge import BridgeError, RemTask, scan

pytestmark = pytest.mark.skipif(shutil.which('node') is None, reason="rem's scanner is a Node library")

MANUAL = """---
name: manual-2026-08-10
---

## mesh
- [ ] MANUAL-20260810-001 [HIGH] an open finding (2026-08-10)
- [x] MANUAL-20260810-002 [LOW] a closed finding (2026-08-10)
"""

#: A finding written in Chinese. THE POINT IS THE BYTES, not the language: any non-ASCII summary
#: reproduces the GBK decode failure, and most of this project's real findings are in Chinese.
NON_ASCII = """---
name: manual-2026-08-09
---

- [ ] MANUAL-20260809-003 [MEDIUM] 自研三角剖分内核路线，替换 spade (2026-08-09)
"""


def _memory(tmp_path, name: str, text: str):
    day = tmp_path / '.claude' / 'memory' / '2026' / '08' / '10'
    day.mkdir(parents=True, exist_ok=True)
    (day / name).write_text(text, encoding='utf-8')
    return tmp_path


def test_it_finds_an_open_finding(tmp_path):
    found = {t.id: t for t in scan(_memory(tmp_path, 'manual.md', MANUAL))}
    assert found['MANUAL-20260810-001'] == RemTask(
        id='MANUAL-20260810-001', summary='an open finding', checked=False, severity='HIGH'
    )


def test_it_reads_rems_CLOSED_bit(tmp_path):
    """The discriminating half of `check`: a closed finding backing an open roadmap item is one of
    the two real inconsistencies, and it is invisible if everything comes back open.
    """
    found = {t.id: t.checked for t in scan(_memory(tmp_path, 'manual.md', MANUAL))}
    assert found == {'MANUAL-20260810-001': False, 'MANUAL-20260810-002': True}


def test_a_non_ascii_finding_survives_the_round_trip(tmp_path):
    """THE MEASURED DEFECT. Decoded with the locale codec this raises in a reader thread and the
    caller sees `stdout is None` -- an unreadable backlog reported as a type error.
    """
    found = {t.id: t.summary for t in scan(_memory(tmp_path, 'manual.md', NON_ASCII))}
    assert '三角剖分' in found['MANUAL-20260809-003']


def test_an_empty_memory_tree_scans_clean(tmp_path):
    """Zero findings is a legitimate answer -- but only when the scan RAN. See the next test."""
    (tmp_path / '.claude' / 'memory').mkdir(parents=True)
    assert scan(tmp_path) == []


def test_a_missing_scanner_RAISES_rather_than_reporting_an_empty_backlog(tmp_path):
    """UNKNOWN IS NOT ZERO. `[]` from a scan that never happened reads as "nothing outstanding", and
    on a box without the plugin that would be every run, forever, silently.
    """
    with pytest.raises(BridgeError, match='task-lib'):
        scan(tmp_path, scripts=tmp_path / 'nowhere')
