"""Observe real offline builders; no mock build results or real credentials."""
import importlib.util
from pathlib import Path
import secrets
import shutil
import subprocess
import pytest

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('builder,function,expected',[
    ('prepare_community_build','prepare',{'uv','pnpm'}),
    ('prepare_host_build','prepare_host',{'node','pnpm'}),
])
def test_real_build_children_exclude_provider_environment(monkeypatch,builder,function,expected):
    spec=importlib.util.spec_from_file_location(builder+'_environment_test',ROOT/'scripts'/(builder+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    variables=['MODEL_API_KEY','MODEL_CREDENTIALS_FILE','EMBEDDING_API_KEY','EMBEDDING_MODEL',
               'EMBEDDING_MODE','EMBEDDING_LOCATION','EMBEDDING_DIMENSION','EMBEDDING_CREDENTIALS_FILE','COMPOSE_FILE']
    marker='synthetic-build-boundary-'+secrets.token_hex(20)
    for name in variables:monkeypatch.setenv(name,marker)
    original_run=subprocess.run;observed=[]
    def observing_actual_run(args,**kwargs):
        # Only inspect caller-supplied environment, then execute the real tool.
        environment=kwargs.get('env')
        assert isinstance(environment,dict) and not any(name.startswith(('MODEL_','EMBEDDING_','COMPOSE_')) for name in environment)
        observed.append(tuple(args))
        return original_run(args,**kwargs)
    monkeypatch.setattr(module.subprocess,'run',observing_actual_run)
    output=ROOT/'.ci-results'/('provider-build-boundary-'+secrets.token_hex(12))
    try:
        report=getattr(module,function)(output)
        assert output.is_dir() and report
        assert {Path(args[0]).name for args in observed}==expected
        if builder=='prepare_community_build':
            assert [args[:2] for args in observed if args[0]=='uv']==[('uv','build'),('uv','export')]
            assert (output/'nexloop_eios_core-0.1.0-py3-none-any.whl').is_file()
        else:assert (output/'runtime.tar').is_file()
        assert all(marker.encode() not in path.read_bytes() for path in output.rglob('*') if path.is_file())
    finally:
        # This exact random test directory is owned and disposable.
        if output.exists():shutil.rmtree(output)
