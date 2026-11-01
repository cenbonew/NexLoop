"""K1 (docs/design/dsh-superagent-idea-intake.md §3.2, §3.5): scripts/check_docs.py on the repository and on synthetic trees."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import check_docs as cd  # noqa: E402

INDEX = '# x\n### 4.2 `nexloop_eios` 模块索引\n| 领域 | 主要模块 |\n|---|---|\n| a | `alpha`、`browser_*`、`pkg/` |\n\n## 5. next\n'
SECTION = '# t\n\n## 5. 否决的做法\n\n| 做法 | 否决理由（拍板日期） |\n|---|---|\n| x | y（2026-11-01） |\n'


def test_the_repository_is_clean():
    assert cd.main() == 0


def test_the_exemptions_all_have_reasons_and_the_module_list_is_empty():
    data = json.loads(cd.EXEMPTIONS.read_text())
    assert cd.exemption_findings(data) == [] and data['architecture_modules'] == {}
    assert 'docs/design/TEMPLATE.md' not in data['design_rejected_section']


def _modules(tmp_path, *names):
    root = tmp_path / 'nexloop_eios'
    root.mkdir()
    for name in names:
        if name.endswith('/'):
            (root / name).mkdir()
            (root / name / '__init__.py').write_text('')
        else:
            (root / f'{name}.py').write_text('')
    (root / '__init__.py').write_text('')
    return root


def test_an_unindexed_module_fails_and_patterns_and_packages_count(tmp_path):
    root = _modules(tmp_path, 'alpha', 'browser_login', 'pkg/', 'gamma')
    assert cd.architecture_findings({}, root, INDEX) == ['ARCHITECTURE.md §4.2 does not index module gamma']
    assert cd.architecture_findings({'gamma': 'reason'}, root, INDEX) == []


def test_a_stale_module_exemption_fails(tmp_path):
    root = _modules(tmp_path, 'alpha')
    assert cd.architecture_findings({'alpha': 'r', 'gone': 'r'}, root, INDEX) == [
        'stale architecture exemption alpha (module gone or already indexed)', 'stale architecture exemption gone (module gone or already indexed)']


def test_a_design_doc_needs_the_rejected_section_with_its_table(tmp_path):
    (tmp_path / 'new.md').write_text('# t\n\n## 1. 方案\n')
    (tmp_path / 'heading-only.md').write_text('# t\n\n## 5. 否决的做法\n\n没有表格。\n')
    (tmp_path / 'ok.md').write_text(SECTION)
    (tmp_path / 'old.md').write_text('# t\n')
    assert cd.design_findings({'docs/design/old.md': 'historical'}, tmp_path) == [
        'docs/design/heading-only.md has no "否决的做法" section with a 做法 | 否决理由 table',
        'docs/design/new.md has no "否决的做法" section with a 做法 | 否决理由 table']


def test_a_stale_design_exemption_fails(tmp_path):
    (tmp_path / 'ok.md').write_text(SECTION)
    assert cd.design_findings({'docs/design/ok.md': 'historical', 'docs/design/gone.md': 'historical'}, tmp_path) == [
        'stale design exemption docs/design/gone.md (file gone or already has the section)',
        'stale design exemption docs/design/ok.md (file gone or already has the section)']


def test_an_exemption_without_a_reason_fails():
    assert cd.exemption_findings({'architecture_modules': {'x': ' '}, 'design_rejected_section': {'docs/design/y.md': ''}}) == [
        'exemption architecture_modules/x has no reason', 'exemption design_rejected_section/docs/design/y.md has no reason']


def test_the_templates_carry_the_rejected_parts():
    assert cd.has_rejected_section((ROOT / 'docs' / 'design' / 'TEMPLATE.md').read_text())
    adr = (ROOT / 'adr' / 'TEMPLATE.md').read_text()
    assert adr.index('## 3. 理由与代价') < adr.index('否决的替代：') < adr.index('## 4. 影响')
