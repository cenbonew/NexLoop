"""Reject empty, failed or skipped critical local-CI test reports."""
from pathlib import Path
import sys
import xml.etree.ElementTree as ET


def verify(path):
    root = ET.parse(path).getroot()
    cases = list(root.iter('testcase'))
    if not cases:
        raise RuntimeError(f'{path}: no executed cases')
    for case in cases:
        if any(case.find(tag) is not None for tag in ('skipped', 'failure', 'error')):
            raise RuntimeError(f'{path}: non-passing critical case {case.get("name")}')
    print(f'{path}: {len(cases)} passed; zero critical skips')
    return len(cases)


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit('required JUnit report path missing')
    for report in sys.argv[1:]:
        verify(Path(report))
