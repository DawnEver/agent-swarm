"""Read the licences of everything a machine actually has installed, and render them as tables.

EXTRACTED FROM motronics' `scripts/repo/legal_notices.py`, 2026-08-12. What stayed behind is that
project's own legal content -- the copyright line, its in-tree package names, its vendored blobs, its
curated overrides and the prose around the tables. Nothing here knows what product it is describing.

WHY THE ENVIRONMENT, NOT A LOCK FILE. A lock carries names and versions but no licence text, so the
licences live in the installed metadata; and a project whose dependencies float has no resolved graph
on disk to read at all. The installed environment IS the resolved graph, and PEP 610
`direct_url.json` records where each distribution came from, including the transitive closure pulled
in by git dependencies. "Trace upward" is satisfied by construction.

DETERMINISTIC: no timestamps, everything sorted, so a generated document is a fixed point and a
`--check` mode can compare it byte-for-byte.
"""

from __future__ import annotations

import importlib.metadata as imd
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Container, Mapping, Sequence
from pathlib import Path

#: Generic raw-licence-string -> SPDX. Applied AFTER the caller's per-package overrides, to the
#: lowercased, whitespace-collapsed string. These are spellings upstreams actually ship, not
#: judgements about any one dependency set -- which is why they are here and the overrides are not.
_RAW_TO_SPDX = {
    'mit': 'MIT',
    'mit license': 'MIT',
    'mit-0': 'MIT-0',
    'mit-cmu': 'MIT-CMU',
    'apache software license': 'Apache-2.0',
    'apache 2.0': 'Apache-2.0',
    'apache-2.0': 'Apache-2.0',
    'bsd': 'BSD-3-Clause',
    'bsd license': 'BSD-3-Clause',
    'bsd-2-clause': 'BSD-2-Clause',
    'bsd-3-clause': 'BSD-3-Clause',
    'psf': 'PSF-2.0',
    'psf-2.0': 'PSF-2.0',
    'mpl-2.0': 'MPL-2.0',
    'gplv2+': 'GPL-2.0-or-later',
    'gpl-2.0-only': 'GPL-2.0-only',
    'lgpl-3.0': 'LGPL-3.0',
    'lgpl-3.0-or-later': 'LGPL-3.0-or-later',
    # Rust crates write the dual as a slash ("MIT/Apache-2.0"); the SPDX form is "OR".
    'mit/apache-2.0': 'MIT OR Apache-2.0',
}

#: SPDX-expression-looking strings pass through verbatim. Parens are stripped first ("(MIT OR
#: Apache-2.0) AND Unicode-3.0" is an expression). A string not matched here and not overridden is
#: UNKNOWN, which a caller is expected to treat as an alarm rather than a footnote.
_EXPRESSION = re.compile(r'^[A-Za-z0-9.+-]+(?: (?:AND|OR|WITH) [A-Za-z0-9.+-]+)+$')
_FULL_TEXT = re.compile(r'^(copyright|permission is|license agreement|===)', re.IGNORECASE | re.MULTILINE)

UNKNOWN = 'UNKNOWN'

#: Section order. A licence expression belongs to the family of its FIRST term (deterministic --
#: every expression seen in practice starts with a plain identifier).
FAMILIES = ('Apache 2.0', 'BSD', 'GPL / LGPL', 'MIT', 'MPL', 'PSF', 'Other')
HEADINGS = {
    'Apache 2.0': 'Software under the Apache 2.0 License',
    'BSD': 'Software under the BSD Software Licenses',
    'GPL / LGPL': 'Software under the GPL and/or LGPL Licenses',
    'MIT': 'Software under the MIT License',
    'MPL': 'Software under the MPL 2.0 (Mozilla Public License)',
    'PSF': 'Software under the PSF (Python Software Foundation) Licenses',
    'Other': 'Software under other licences',
}


def family(spdx: str) -> str:
    first = spdx.strip('()').split(' AND ')[0].split(' OR ')[0].strip('()')
    if first.startswith('LGPL'):
        return 'GPL / LGPL'
    for name in FAMILIES:
        if name == 'Other':
            return name
        if first.startswith(name.split(' ')[0]):
            return name
    return 'Other'


def normalize_license(name: str, raw: str, *, overrides: Mapping[str, str]) -> str:
    """The SPDX expression for `raw`, or `UNKNOWN` when it is not one.

    `overrides` is REQUIRED and is the caller's curation: every entry exists because one upstream's
    metadata string was too broken to trust, and which strings those are is a property of a
    particular dependency set, not of licensing.
    """
    if name in overrides:
        return overrides[name]
    text = ' '.join((raw or '').split())
    if not text or _FULL_TEXT.search(text):
        return UNKNOWN
    text = re.sub(r'^License :: OSI Approved :: ', '', text)
    lower = text.lower()
    if lower in _RAW_TO_SPDX:
        return _RAW_TO_SPDX[lower]
    if _EXPRESSION.match(text.replace('(', '').replace(')', '')):
        return text
    return UNKNOWN


def _raw_license(md) -> str:
    """The licence a distribution declares: field first, then the classifier.

    Many packages leave the ``License`` field empty and declare only
    ``Classifier: License :: OSI Approved :: ...`` -- measured, and the field alone produced UNKNOWNs.
    """
    raw = md.get('License') or md.get('License-Expression') or ''
    if raw:
        return raw
    for classifier in md.get_all('Classifier') or []:
        if classifier.startswith('License ::'):
            return classifier
    return ''


def _metadata_url(md) -> str:
    home = md.get('Home-page')
    if home:
        return home
    urls = md.get_all('Project-URL') or []
    for url in urls:
        if url.startswith('Homepage,'):
            return url.split(',', 1)[1].strip()
    for url in urls:
        if ',' in url:
            return url.split(',', 1)[1].strip()
    return ''


def python_packages(*, in_tree: Container[str], overrides: Mapping[str, str]) -> list[dict]:
    """Every installed distribution outside `in_tree`, licence normalized.

    `in_tree` is REQUIRED: which distributions are the consumer's OWN code is the one thing this
    cannot measure, and a default of "none" would list a product as a third party in its own notice.
    """
    rows = []
    for dist in imd.distributions():
        md = dist.metadata
        name = md.get('Name', '?')
        if name in in_tree:
            continue
        rows.append(
            {
                'name': name,
                'version': md.get('Version', '?'),
                'license': normalize_license(name, _raw_license(md), overrides=overrides),
                'url': _metadata_url(md) or '—',
            }
        )
    return rows


def rust_crates(manifest: Path, *, overrides: Mapping[str, str]) -> list[dict]:
    """Third-party crates from `cargo metadata` (workspace members are the consumer's own code)."""
    cargo = shutil.which('cargo')
    if cargo is None:
        sys.exit('cargo not on PATH -- a Rust toolchain is required to read crate licences; refusing to guess')
    out = subprocess.run(
        [cargo, 'metadata', '--format-version', '1', '--manifest-path', str(manifest)],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(out.stdout)
    workspace = frozenset(data['workspace_members'])
    return [
        {
            'name': pkg['name'],
            'version': pkg['version'],
            'license': normalize_license(pkg['name'], pkg.get('license') or '', overrides=overrides),
            'url': f'https://crates.io/crates/{pkg["name"]}',
        }
        for pkg in data['packages']
        if pkg['id'] not in workspace
    ]


#: A direct git reference as a manifest spells it: "name @ git+<url>".
_DIRECT_GIT = re.compile(r'^([A-Za-z0-9_.-]+) @ git\+(\S+)$')


def git_sources(declared: Sequence[str]) -> dict[str, str]:
    """Distribution name -> repo URL, from the ENVIRONMENT first and the caller's `declared` specs
    second. Fragments and pinned revs are stripped: they are provenance, not the repository URL.

    THE ENVIRONMENT IS THE PRIMARY SOURCE and a lock file is not a source at all. A lock LAGS the
    manifest, and where it is gitignored it is absent from every worktree -- so a check reading it
    was red in every lane, with a remedy that failed the same way. PEP 610 has the installer write
    `direct_url.json` beside each distribution, so the environment records where each package
    ACTUALLY came from, including transitive git dependencies, which a manifest alone cannot see.
    """
    out: dict[str, str] = {}
    for dist in imd.distributions():
        try:
            raw = dist.read_text('direct_url.json')
        except OSError:
            # An unreadable distribution is not a git one by default -- it is unknown. Left out of
            # the map it simply reports no repo link; it can never invent one.
            continue
        if not raw:
            continue
        try:
            direct = json.loads(raw)
        except ValueError:
            continue
        name = dist.metadata['Name']
        if (direct.get('vcs_info') or {}).get('vcs') == 'git' and name:
            out[name] = direct.get('url', '')
    for spec in declared:
        match = _DIRECT_GIT.match(spec)
        if match:
            out.setdefault(match.group(1), match.group(2))
    clean = {name: url.split('?')[0].split('#')[0] for name, url in out.items()}
    return {name: re.sub(r'@[0-9a-f]{7,40}$', '', url) for name, url in clean.items()}


def table(rows: Sequence[dict]) -> str:
    """One markdown table, sorted so the output is a fixed point."""
    lines = ['| Package | Version | License | URL |', '|---|---|---|---|']
    lines.extend(
        f'| {r["name"]} | {r["version"]} | {r["license"]} | {r["url"]} |'
        for r in sorted(rows, key=lambda r: (r['name'].lower(), r['version']))
    )
    return '\n'.join(lines)


def grouped(rows: Sequence[dict]) -> str:
    """The rows as one section per licence family, in `FAMILIES` order, empty families omitted."""
    parts: list[str] = []
    for name in FAMILIES:
        members = [r for r in rows if family(r['license']) == name]
        if not members:
            continue
        parts.extend((f'## {HEADINGS[name]}', '', table(members), ''))
    return '\n'.join(parts).rstrip()
