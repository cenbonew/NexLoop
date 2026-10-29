"""NX-031 D7 measured, not only declared: every background service runs as its own long-lived process on a disposable
cluster (credentials from the repository service-grants manifest, as deployed) while pg_stat_activity is sampled. EIOS pools
name every connection `nex-eios`, so each service is measured alone (peak above the idle baseline), then all ten together.
Each process must hold at most what deploy/stage/connection-budget.v1.json declares for it (its pool, plus the launcher's API
pool for the two launchers). The peaks are printed for the slice report."""
from contextlib import contextmanager
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from nexloop_eios import background_services as B
from test_background_services_pg import ROLES, argv
from test_service_grants_pg import deployment  # noqa: F401

ROOT=Path(__file__).resolve().parents[1]
BUDGET={s['name']:s for s in json.loads((ROOT/'deploy/stage/connection-budget.v1.json').read_text())['services']}
ENTRY='import sys\nfrom nexloop_eios.background_services import main_for\nsys.exit(main_for(sys.argv[1],sys.argv[2:]))'
SAMPLE="select count(*) from pg_stat_activity where application_name='nex-eios' and pid<>pg_backend_pid()"


@contextmanager
def running(d,tmp_path,services):
    original=dict(ROLES);ROLES['alert-evaluator']=('alert_evaluator','nexloop_domain_worker')
    tmp_path.mkdir(exist_ok=True);(tmp_path/'artifacts').mkdir(mode=0o700,exist_ok=True)
    children={}
    try:
        for service in services:
            args=[a for a in argv(d,tmp_path,service) if a!='--once']+['--tick-seconds','0.5']
            children[service]=subprocess.Popen([sys.executable,'-c',ENTRY,service,*args],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        for service,child in children.items():
            line=child.stdout.readline()
            assert line.strip()==B.SERVICES[service][0]+' ready',(service,line,child.poll())
        yield children
        assert all(child.poll() is None for child in children.values()),{s:c.poll() for s,c in children.items()}
    finally:
        ROLES.clear();ROLES.update(original)
        for child in children.values():child.terminate()
        for child in children.values():child.wait(timeout=30)


def peak(admin,seconds):
    top=0;deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        top=max(top,admin.execute(SAMPLE).fetchone()[0]);time.sleep(0.05)
    return top


def test_background_services_stay_within_their_declared_connections(deployment,admin,tmp_path):
    d=deployment;d['apply']()
    baseline=peak(admin,1)
    # ONE_SHOT services (world-model glossary, --once from a timer) take the operations reserve, not the application budget.
    services=[s for s in B.SERVICES if s not in B.ONE_SHOT]
    allowed={s:BUDGET[s]['pool_max']+BUDGET.get(s+'-launcher',{}).get('pool_max',0) for s in services}
    measured={}
    for service in services:
        with running(d,tmp_path/service,[service]):
            measured[service]=peak(admin,4)-baseline
    with running(d,tmp_path/'all',services):
        together=peak(admin,8)-baseline
    report={'baseline':baseline,'measured_peak':measured,'declared':allowed,'measured_together':together,'declared_total':sum(allowed.values())}
    print('NX031-D7-MEASURED',json.dumps(report,sort_keys=True))
    assert all(1<=measured[s]<=allowed[s] for s in services),report
    assert together<=sum(allowed.values()),report
