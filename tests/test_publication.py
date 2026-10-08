"""Index/worktree divergence cannot hide a private staged blob."""
from pathlib import Path
import subprocess
import sys
import pytest

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/check_publication.py'

@pytest.fixture
def repository(tmp_path):
    root=tmp_path/'nexloop-publication-probe';root.mkdir()
    subprocess.run(['git','init','-q',str(root)],check=True)
    (root/'.gitignore').write_text('.env\ndocs/tmp/\n')
    return root


def run(root,*args):
    return subprocess.run([sys.executable,str(SCRIPT),*args],cwd=root,capture_output=True,text=True)


def test_sanitized_worktree_does_not_hide_staged_credential_assignment(repository):
    file=repository/'README.md'
    file.write_text('MODEL_API_KEY='+'synthetic-invalid-marker\n')
    subprocess.run(['git','add','README.md'],cwd=repository,check=True)
    file.write_text('public and sanitized\n')
    for args in [(),('--staged',)]:
        result=run(repository,*args)
        assert result.returncode!=0 and '(index): model credential' in result.stdout
        assert 'synthetic-invalid-marker' not in result.stdout


def test_staged_check_does_not_substitute_unstaged_bad_worktree(repository):
    file=repository/'README.md';file.write_text('public\n')
    subprocess.run(['git','add','README.md'],cwd=repository,check=True)
    file.write_text('MODEL_API_KEY='+'synthetic-invalid-marker\n')
    assert run(repository,'--staged').returncode==0
    assert run(repository).returncode!=0


def test_forced_private_path_is_rejected_even_if_worktree_file_missing(repository):
    file=repository/'.env';file.write_text('synthetic private annex\n')
    subprocess.run(['git','add','-f','.env'],cwd=repository,check=True)
    file.unlink()
    result=run(repository,'--staged')
    assert result.returncode!=0 and '(index): private path' in result.stdout


def test_empty_index_stays_empty(repository):
    result=run(repository,'--staged')
    assert result.returncode==0 and '0 index blobs' in result.stdout
