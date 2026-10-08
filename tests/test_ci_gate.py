"""A skipped or empty critical suite may never produce a passing local gate."""
from pathlib import Path
import subprocess
import sys
import pytest

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/check_test_report.py'

@pytest.mark.parametrize('body',['', '<testcase name="critical"><skipped/></testcase>', '<testcase name="critical"><failure/></testcase>', '<testcase name="critical"><error/></testcase>'])
def test_critical_report_rejected(tmp_path,body):
    report=tmp_path/'report.xml';report.write_text(f'<testsuite>{body}</testsuite>')
    result=subprocess.run([sys.executable,str(SCRIPT),str(report)],capture_output=True)
    assert result.returncode!=0


def test_executed_passing_report_accepted(tmp_path):
    report=tmp_path/'report.xml';report.write_text('<testsuite><testcase name="critical"/></testsuite>')
    result=subprocess.run([sys.executable,str(SCRIPT),str(report)],capture_output=True)
    assert result.returncode==0
