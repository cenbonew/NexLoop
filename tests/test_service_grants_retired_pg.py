"""NX-023 / NX-048: a principal retired by a later manifest loses every standing grant on apply.

The v5 manifest (fb61852) declared context_assembler with EXECUTE nexloop.context.assemble:1;
v6 retires it because that authority is now issued with each Run (0104). Applying v6 over
an already applied v5 revokes the grant through trusted configuration and doctor is back in
sync; the retired principal can no longer decide the Action. Synthetic data only.
"""
import copy,json,secrets,subprocess
from pathlib import Path

import pytest
from eios.authz.operations import Operation
from eios.authz.resources import ResourceType
from nexloop_eios import service_grants as G
from nexloop_eios.authorization import authenticate_service
from test_service_grants_pg import MANIFEST,TENANT,decide,deployment  # noqa: F401

ROOT=Path(__file__).resolve().parents[1]
ASSEMBLE='eios:action:nexloop.context.assemble:1'


def v5():
    text=subprocess.run(['git','-C',str(ROOT),'show','fb61852:deploy/authorization/service-grants.v1.json'],check=True,capture_output=True,text=True).stdout
    return G.validate(json.loads(text))


def test_retired_assembler_is_revoked_on_upgrade_and_doctor_in_sync(deployment,admin):
    d=deployment;old=v5()
    assembler=next(p for p in old['principals'] if p['role']=='context_assembler')
    assert old['manifest_version']==5 and any(g['resource_id']==ASSEMBLE for g in old['grants'])
    # The v5 deployment also had the assembler credential (secret supplied by the operator).
    d['tokens']['context_assembler']=secrets.token_urlsafe(48);d['paths']['secrets'].write_text(json.dumps(d['tokens']))
    d['apply'](old)
    pool=d['pools']['nexloop_api']
    session=authenticate_service(pool,d['tokens']['context_assembler'],world='real')
    assert decide(pool,session,ASSEMBLE)
    # Upgrade: v6 retires the principal; before apply, doctor reports the leftover grant as managed drift.
    report=G.doctor(d['manifest'],TENANT,database_url_file=d['paths']['dsn'])
    assert report['in_sync'] is False
    assert {'principal_id':assembler['principal_id'],'resource_id':ASSEMBLE,'operation':'execute','manifest_principal':True} in report['extra_grants']
    applied=d['apply']()
    assert {'principal_id':assembler['principal_id'],'resource_id':ASSEMBLE} in applied['grants_revoked']
    assert G.doctor(d['manifest'],TENANT,database_url_file=d['paths']['dsn'])['in_sync'] is True
    session=authenticate_service(pool,d['tokens']['context_assembler'],world='real')
    assert not decide(pool,session,ASSEMBLE)
    assert d['apply']()['changed'] is False


def test_retired_principal_validation():
    base=G.load(MANIFEST)
    for change,reason in ((lambda b:b['retired_principals'][0].update(role='claim_matcher'),'retired_principal_active'),
                          (lambda b:b['retired_principals'][0].update(principal_id=b['principals'][0]['principal_id']),'retired_principal_active'),
                          (lambda b:b['retired_principals'][0].update(retired_in=99),'retired_principal_shape'),
                          (lambda b:b['retired_principals'][0].update(extra=1),'retired_principal_shape'),
                          (lambda b:b['retired_principals'].append(dict(b['retired_principals'][0])),'retired_principal_active')):
        body=copy.deepcopy(base);change(body)
        with pytest.raises(G.ServiceGrantsRejected,match=reason):G.validate(body)
