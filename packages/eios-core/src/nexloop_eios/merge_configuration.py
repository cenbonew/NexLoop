"""NX-045/046 glue configuration manifest: validate, compare and publish through trusted configuration.

python -m nexloop_eios.merge_configuration --manifest deploy/ontology/merge-config.v1.json --check
python -m nexloop_eios.merge_configuration --manifest ... --tenant <tenant> --doctor --database-url-file <configurator-dsn-file>
python -m nexloop_eios.merge_configuration --manifest ... --tenant <tenant> --apply  --database-url-file <configurator-dsn-file>

Writes go only through control.nexloop_publish_merge_configuration_manifest as
nexloop_configurator (never superuser/BYPASSRLS/owner member). Versions are
immutable; re-applying the same file is a no-op.
"""
import argparse
import json
import math
import re
import sys
from pathlib import Path

from psycopg.types.json import Jsonb
from nexloop_eios.candidate_merge import FEATURE_VERSION,FEATURES
from nexloop_eios.trusted_configuration import configurator_connection

KEYS={'schema_version','config_version','decision','feature_version','embedding_profile','weights','merge_threshold','dedupe_threshold',
      'reject_cooldown_seconds','whitelist','calibration','notes'}
CALIBRATION={'dataset','dataset_version','dataset_ref','evidence_ref','evidence_class','inputs','method','pairs','positives','true_positive',
             'false_positive','precision','recall','margin'}


class MergeConfigurationRejected(ValueError):
    pass


def _unit(value):return type(value) in (int,float) and not isinstance(value,bool) and math.isfinite(value) and 0<=value<=1


def validate(manifest):
    m=manifest
    def need(condition,reason):
        if not condition:raise MergeConfigurationRejected(reason)
    need(isinstance(m,dict) and set(m)==KEYS,'manifest keys')
    need(m['schema_version']=='nexloop-merge-config/1','schema_version')
    need(type(m['config_version']) is str and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,79}',m['config_version']),'config_version')
    need(m['feature_version']==FEATURE_VERSION,'feature_version must match the deployed glue features')
    p=m['embedding_profile']
    need(isinstance(p,dict) and set(p)=={'model','dimension'} and type(p['model']) is str and re.fullmatch(r'[!-~]{1,160}',p['model'])
        and type(p['dimension']) is int and 1<=p['dimension']<=16000,'embedding_profile')
    w=m['weights']
    need(isinstance(w,dict) and set(w)==set(FEATURES) and all(_unit(v) for v in w.values()) and abs(sum(w.values())-1)<0.0005,'weights')
    need(_unit(m['merge_threshold']) and _unit(m['dedupe_threshold']),'thresholds')
    # Calibration invariant: a business whitelist hit alone reaches the merge threshold.
    need(w['rule_whitelist']+1e-9>=m['merge_threshold'],'whitelist weight below merge threshold')
    need(type(m['reject_cooldown_seconds']) is int and 0<=m['reject_cooldown_seconds']<=31536000,'reject_cooldown_seconds')
    need(isinstance(m['whitelist'],list) and all(isinstance(e,dict) and set(e)<={'texts','target_ref'} and isinstance(e.get('texts'),list) and e['texts']
        and all(type(t) is str and t.strip() for t in e['texts']) for e in m['whitelist']),'whitelist')
    c=m['calibration']
    need(isinstance(c,dict) and set(c)==CALIBRATION,'calibration keys')
    need(c['evidence_class'] in ('real','test') and type(c['pairs']) is int and c['pairs']>0 and c['true_positive']+c['false_positive']<=c['pairs'],'calibration counts')
    need(c['false_positive']==0 and c['precision']==1.0,'a published configuration must have had no false merge in calibration')
    need(isinstance(m['notes'],list) and all(type(n) is str for n in m['notes']) and type(m['decision']) is str and m['decision'],'notes/decision')
    return m


def load(path):
    return validate(json.loads(Path(path).read_text()))


def doctor(manifest,tenant,*,database_url_file):
    with configurator_connection(database_url_file) as db:
        state=db.execute('select control.nexloop_read_merge_configuration(%s)',(tenant,)).fetchone()[0]
    active=state['active'];profile=f"{manifest['embedding_profile']['model']}@{manifest['embedding_profile']['dimension']}"
    findings=[]
    if state['recall_profile']!=profile:findings.append({'kind':'embedding_profile_mismatch','active_recall_profile':state['recall_profile'],'manifest':profile})
    if active is None:findings.append({'kind':'no_active_configuration'})
    elif active['config_version']!=manifest['config_version']:findings.append({'kind':'other_version_active','active':active['config_version']})
    else:
        for key in ('weights','merge_threshold','dedupe_threshold','reject_cooldown_seconds'):
            have=active[key];want=manifest[key]
            same=({k:float(v) for k,v in have.items()}=={k:float(v) for k,v in want.items()}) if key=='weights' else float(have)==float(want)
            if not same:findings.append({'kind':'content_drift','field':key})
    return {'config_version':manifest['config_version'],'in_sync':not findings,'findings':findings,'active':active and active['config_version']}


def apply(manifest,tenant,*,database_url_file):
    with configurator_connection(database_url_file) as db,db.transaction():
        return db.execute('select control.nexloop_publish_merge_configuration_manifest(%s,%s)',(tenant,Jsonb(manifest))).fetchone()[0]


def main(argv=None):
    p=argparse.ArgumentParser(description='NexLoop glue (merge) configuration manifest')
    p.add_argument('--manifest',type=Path,required=True);p.add_argument('--tenant');p.add_argument('--database-url-file',type=Path)
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check',action='store_true');mode.add_argument('--doctor',action='store_true');mode.add_argument('--apply',action='store_true')
    a=p.parse_args(argv)
    try:
        manifest=load(a.manifest)
        if a.check:result={'valid':True,'config_version':manifest['config_version']}
        else:
            if not a.tenant or not a.database_url_file:raise MergeConfigurationRejected('--tenant and --database-url-file required')
            result=doctor(manifest,a.tenant,database_url_file=a.database_url_file) if a.doctor else apply(manifest,a.tenant,database_url_file=a.database_url_file)
    except Exception as error:
        print(json.dumps({'ok':False,'error':type(error).__name__,'reason':str(error) if isinstance(error,MergeConfigurationRejected) else 'refused'}));return 1
    print(json.dumps(result,ensure_ascii=False));return 0 if result.get('in_sync',True) else 1


if __name__=='__main__':sys.exit(main())
