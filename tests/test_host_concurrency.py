"""ADR-022 §4: doctor binds the deployed Agent Host active-Run limit to <= 4 before start."""
import json,os,re,subprocess,sys
from pathlib import Path
import pytest
from nexloop_eios import host_concurrency as hc

ROOT=Path(__file__).resolve().parents[1]
EXAMPLE=ROOT/'deploy/stage/agent-host.runtime-config.example.json'


@pytest.mark.parametrize('config,passed,limit',[({},True,4),({'maximum_active_runs':4},True,4),({'maximum_active_runs':1},True,1),
    ({'maximum_active_runs':5},False,5),({'maximum_active_runs':64},False,64)])
def test_limit_within_the_gate(config,passed,limit):
    result=hc.evaluate(config)
    assert result['passed'] is passed and result['details']['maximum_active_runs']==limit
    assert result['details']['explicit'] is ('maximum_active_runs' in config)


@pytest.mark.parametrize('config',[[],{'maximum_active_runs':0},{'maximum_active_runs':65},{'maximum_active_runs':'4'},{'maximum_active_runs':4.0},
    {'maximum_active_runs':True},{'run_admission_wait_ms':-1},{'run_admission_wait_ms':30001},{'run_admission_wait_ms':'500'}])
def test_invalid_configuration_is_refused(config):
    with pytest.raises(hc.HostConcurrencyInvalid):hc.evaluate(config)


def test_python_defaults_match_the_host():
    source=(ROOT/'apps/agent-host/src/run-admission-gate.ts').read_text()
    assert int(re.search(r'DEFAULT_MAXIMUM_ACTIVE_RUNS=(\d+)',source).group(1))==hc.HOST_DEFAULT_ACTIVE_RUNS
    assert int(re.search(r'DEFAULT_RUN_ADMISSION_WAIT_MS=(\d+)',source).group(1))==hc.HOST_DEFAULT_ADMISSION_WAIT_MS
    assert hc.HOST_DEFAULT_ACTIVE_RUNS<=hc.MAXIMUM_HOST_ACTIVE_RUNS==4


def test_deployment_example_sets_four():
    example=json.loads(EXAMPLE.read_text())
    assert example['maximum_active_runs']==4 and hc.evaluate(example)['passed']
    assert not re.search(r'\b(?!127\.0\.0\.1\b)\d{1,3}(?:\.\d{1,3}){3}\b',EXAMPLE.read_text())


def doctor(*arguments):
    return subprocess.run([sys.executable,'-m','nexloop_eios.doctor',*arguments],capture_output=True,text=True,timeout=60)


def private(path,value,mode=0o600):
    path.write_text(value);path.chmod(mode);return path


@pytest.mark.parametrize('body,code',[({'maximum_active_runs':4},0),({},0),({'maximum_active_runs':1,'run_admission_wait_ms':0},0),
    ({'maximum_active_runs':5},1),({'maximum_active_runs':'4'},1)])
def test_doctor_agent_host_gate(tmp_path,body,code):
    config=private(tmp_path/'runtime-config.json',json.dumps({**json.loads(EXAMPLE.read_text()),**body} if body else
        {k:v for k,v in json.loads(EXAMPLE.read_text()).items() if k not in ('maximum_active_runs','run_admission_wait_ms')}))
    result=doctor('--agent-host-only','--agent-host-config',str(config))
    report=json.loads(result.stdout)
    assert result.returncode==code and report['checks']=={'agent_host_concurrency':code==0},report


def test_doctor_agent_host_gate_refuses_unreadable_or_public_configuration(tmp_path):
    public=private(tmp_path/'public.json',json.dumps({'maximum_active_runs':4}),0o644)
    for path in (public,private(tmp_path/'broken.json','{'),tmp_path/'missing.json'):
        result=doctor('--agent-host-only','--agent-host-config',str(path))
        assert result.returncode==1 and json.loads(result.stdout)['checks']=={'agent_host_concurrency':False}
    assert doctor('--agent-host-only').returncode==2
    assert doctor().returncode==2  # the foundation doctor still needs --artifact-root
