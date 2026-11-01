#!/usr/bin/env python3
"""K1 (docs/design/dsh-superagent-idea-intake.md §3.2 document side, §3.5): local document checks.

1. docs/ARCHITECTURE.md §4.2 (the nexloop_eios module index) names every module and package under
   packages/eios-core/src/nexloop_eios/ (backticked names; `x_*` patterns allowed), unless exempted with a reason.
2. Every docs/design/*.md has a "否决的做法" section (heading) with a two-column table 做法 | 否决理由, unless it is a
   historical file in the exemption list. docs/design/TEMPLATE.md carries the section itself.

Exemptions: scripts/docs-check-exemptions.json. Each needs a non-empty reason; a stale entry (the module is gone or indexed,
the file is gone or has the section) fails, so the list only shrinks. Exit 0 when clean, 1 with one line per finding.
"""
import fnmatch
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / 'packages' / 'eios-core' / 'src' / 'nexloop_eios'
ARCHITECTURE = ROOT / 'docs' / 'ARCHITECTURE.md'
DESIGN = ROOT / 'docs' / 'design'
EXEMPTIONS = ROOT / 'scripts' / 'docs-check-exemptions.json'
REJECTED_HEADING = re.compile(r'^#{2,4} +(?:\S+ +)?否决的做法\s*$', re.M)
REJECTED_TABLE = re.compile(r'^\| *做法 *\| *否决理由[^|]*\|\s*$\n^\|[-: |]+\|\s*$', re.M)


def modules(root=MODULES):
    names = {p.stem for p in root.glob('*.py') if p.stem != '__init__'}
    return names | {p.name for p in root.iterdir() if p.is_dir() and (p / '__init__.py').exists()}


def indexed_patterns(text):
    start = text.index('### 4.2')
    end = text.index('\n## ', start)
    return {token.rstrip('/') for token in re.findall(r'`([^`]+)`', text[start:end])}


def architecture_findings(exempt, root=MODULES, text=None):
    patterns = indexed_patterns(ARCHITECTURE.read_text() if text is None else text)
    present = modules(root)
    covered = {m for m in present if any(fnmatch.fnmatchcase(m, p) for p in patterns)}
    out = [f'ARCHITECTURE.md §4.2 does not index module {m}' for m in sorted(present - covered - set(exempt))]
    out += [f'stale architecture exemption {m} (module gone or already indexed)' for m in sorted(exempt) if m not in present or m in covered]
    return out


def has_rejected_section(text):
    heading = REJECTED_HEADING.search(text)
    return bool(heading and REJECTED_TABLE.search(text, heading.end()))


def design_findings(exempt, root=DESIGN):
    out = []
    files = {f'docs/design/{p.name}': p for p in sorted(root.glob('*.md'))}
    for name, path in files.items():
        if name not in exempt and not has_rejected_section(path.read_text()):
            out.append(f'{name} has no "否决的做法" section with a 做法 | 否决理由 table')
    out += [f'stale design exemption {n} (file gone or already has the section)' for n in sorted(exempt)
            if n not in files or has_rejected_section(files[n].read_text())]
    return out


def exemption_findings(data):
    return [f'exemption {group}/{name} has no reason' for group in ('architecture_modules', 'design_rejected_section')
            for name, reason in data.get(group, {}).items() if not isinstance(reason, str) or not reason.strip()]


def main():
    data = json.loads(EXEMPTIONS.read_text())
    findings = exemption_findings(data) + architecture_findings(data.get('architecture_modules', {})) \
        + design_findings(data.get('design_rejected_section', {}))
    for line in findings:
        print(line)
    print(f'docs check: {len(findings)} finding(s)')
    return 1 if findings else 0


if __name__ == '__main__':
    sys.exit(main())
