"""Published contracts never change (dispatcher 2026-10-21, NX-043 X4 wiring decision B): packages/contracts/published.json records
released contracts with their checksum; the files must match it byte for byte. A new field or value is a new contract version."""
import hashlib
import json
from pathlib import Path

C = Path(__file__).resolve().parents[1] / 'packages/contracts'
PUBLISHED = json.loads((C / 'published.json').read_text())


def test_published_contracts_are_unchanged():
    assert PUBLISHED['contracts'] and any(c['path'] == 'context-pack-v6.schema.json' for c in PUBLISHED['contracts'])
    for c in PUBLISHED['contracts']:
        assert hashlib.sha256((C / c['path']).read_bytes()).hexdigest() == c['sha256'], c['path']
