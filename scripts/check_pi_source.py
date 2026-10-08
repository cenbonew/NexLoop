"""Verify vendored source bytes and offline, frozen generated provider data."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    provenance = json.loads((ROOT / 'vendor/pi/PROVENANCE.json').read_text())
    for record in provenance['files']:
        path = ROOT / record['path']
        if digest(path) != record['sha256']:
            raise RuntimeError(f'Pi source drift: {record["path"]}')
    pin = json.loads((ROOT / 'vendor/pi/model-catalog-pin.json').read_text())['revision']
    if f'sha256-{digest(ROOT / "vendor/pi/model-catalog.json")}' != pin:
        raise RuntimeError('frozen model catalog checksum mismatch')
    generated = json.loads((ROOT / 'vendor/pi/GENERATED.json').read_text())
    expected = {r['path'] for r in generated['files']}
    actual = {p.relative_to(ROOT).as_posix() for p in (ROOT/'vendor/pi/packages/ai/src/providers/data').rglob('*') if p.is_file()}
    if actual != expected:
        raise RuntimeError('provider generated-file set drift')
    for record in generated['files']:
        if digest(ROOT/record['path']) != record['sha256']:
            raise RuntimeError(f'provider generated-data drift: {record["path"]}')
    print(f'Pi source integrity: {len(provenance["files"])} source files; {len(expected)} frozen generated files passed')

if __name__ == '__main__':
    main()
