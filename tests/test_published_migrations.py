"""Published migrations never change (dispatcher 2026-10-21, after the AT-048 performance fix edited 0286 in place): migrations/published.json
records every migration already applied on stage with its checksum; the catalog and the files must match it byte for byte, and a fix goes
into a new migration. The dispatcher updates the record at each release."""
import hashlib
import json
from pathlib import Path

M = Path(__file__).resolve().parents[1] / 'packages/eios-core/src/eios/migrations'
PUBLISHED = json.loads((M / 'published.json').read_text())
CATALOG = {m['version']: m for m in json.loads((M / 'catalog.json').read_text())['migrations']}


def test_the_record_is_contiguous_through_the_published_version():
    versions = [m['version'] for m in PUBLISHED['migrations']]
    assert versions == sorted(versions) and versions[-1] == PUBLISHED['published_through']
    assert versions == [v for v in sorted(CATALOG) if v <= PUBLISHED['published_through']]


def test_published_catalog_entries_and_files_are_unchanged():
    for m in PUBLISHED['migrations']:
        entry = CATALOG[m['version']]
        assert (entry['name'], entry['sha256']) == (m['name'], m['sha256']), m['version']
        assert hashlib.sha256((M / m['name']).read_bytes()).hexdigest() == m['sha256'], m['name']
