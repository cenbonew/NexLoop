"""Versioned business Action declarations compiled into trusted-configuration rows.

Pure builder: it reads a declaration (deploy/configuration/business-actions.v*.json),
the deployment's published object type schemas and an explicit frozen Capability
snapshot, and returns ``{'definition','capability'}`` rows for the trusted
configurator (0050). It grants nothing and never touches a database.
"""
import json
from pathlib import Path
import re

from eios.ontology.definitions import (ActionDefinition,DefinitionStatus,ActionGovernanceContract,ActionChangeScope,
    ActionRiskLevel,ActionApprovalMode,ActionIdempotencyPolicy,OntologySchemaReference,OntologySchemaType)
from eios.ontology.models import ObjectTypeDefinition
from eios.ontology.semantics import schema_contract_digest
from eios.ontology.version_resolution import CapabilityContractKind,validate_capability_binding

SCHEMA='nexloop-business-actions/1'
TOP={'schema_version','manifest_version','decision','actions'}
ACTION={'stable_name','version','object_type','capability_name','authority','risk_level','approval_mode','policy_refs',
    'idempotency_key_fields','target_systems','executor_role','purpose','source_task'}
# Deployment may publish exactly these profiles. A service-executed create or edit, or an Action whose
# executor is the human owner only (SQL refuses every non-human principal for it).
PROFILES={'ontology.object.create':'service','ontology.object.edit':'service','context.strategy.publish':'human_owner','context.audit':'human_owner'}
HUMAN_OWNER='human_owner'


class BusinessActionsRejected(ValueError):
    pass


def validate(value):
    if type(value) is not dict or set(value)!=TOP or value['schema_version']!=SCHEMA or type(value['manifest_version']) is not int or value['manifest_version']<1:
        raise BusinessActionsRejected('manifest shape')
    if type(value['actions']) is not list or not value['actions']:raise BusinessActionsRejected('actions')
    seen=set()
    for item in value['actions']:
        if type(item) is not dict or set(item)!=ACTION:raise BusinessActionsRejected('action shape')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.]{0,159}',str(item['stable_name'])) or type(item['version']) is not int or item['version']<1:
            raise BusinessActionsRejected('action identity')
        if (item['stable_name'],item['version']) in seen:raise BusinessActionsRejected('duplicate action')
        seen.add((item['stable_name'],item['version']))
        ref=item['object_type']
        if type(ref) is not dict or set(ref)!={'stable_name','version'} or type(ref['version']) is not int:raise BusinessActionsRejected('object type')
        # This path only publishes low-risk, no-approval, policy-free Actions of a known profile;
        # anything stronger needs the owner's own governance, not a deployment manifest.
        if (PROFILES.get(item['capability_name'])!=item['authority'] or item['risk_level']!='low' or item['approval_mode']!='none'
                or item['policy_refs']!=[] or item['idempotency_key_fields']!=['request_id'] or item['target_systems']!=['postgres']):
            raise BusinessActionsRejected('governance outside deployment scope')
        if (item['authority']==HUMAN_OWNER)!=(item['executor_role']==HUMAN_OWNER):
            raise BusinessActionsRejected('human-owner Actions are executed by the human owner only')
        for name in ('executor_role','purpose','source_task'):
            if type(item[name]) is not str or not item[name].strip():raise BusinessActionsRejected(name)
    return value


def load(path):
    return validate(json.loads(Path(path).read_text()))


def compile_actions(manifest,*,tenant,created_by,created_at,object_types,capability=None,capabilities=None,select=None):
    """object_types: the trusted manifest's published object type schema dicts.

    capabilities maps capability name → explicit frozen snapshot (``capability`` is the
    ontology.object.create shorthand). ``select`` limits compilation to named Actions.
    """
    validate(manifest)
    snapshots=dict(capabilities or {})
    if capability is not None:snapshots.setdefault('ontology.object.create',capability)
    for name,snapshot in snapshots.items():
        if name not in PROFILES or snapshot.capability_name!=name or snapshot.kind is not CapabilityContractKind.ATOMIC or not snapshot.idempotent:
            raise BusinessActionsRejected('capability')
        if name in ('ontology.object.create','ontology.object.edit') and not snapshot.has_side_effects:raise BusinessActionsRejected('capability')
    schemas={(row['type_name'],row['version']):ObjectTypeDefinition.model_validate(row) for row in object_types}
    rows=[]
    for item in manifest['actions']:
        if select is not None and item['stable_name'] not in select:continue
        snapshot=snapshots.get(item['capability_name'])
        if snapshot is None:raise BusinessActionsRejected('capability snapshot missing for '+item['stable_name'])
        schema=schemas.get((item['object_type']['stable_name'],item['object_type']['version']))
        if schema is None:raise BusinessActionsRejected('object type not published')
        ref=OntologySchemaReference(tenant_id=tenant,schema_type=OntologySchemaType.OBJECT_TYPE,stable_name=schema.type_name,
            version=schema.version,schema_digest=schema_contract_digest(schema))
        definition=ActionDefinition(tenant_id=tenant,stable_name=item['stable_name'],version=item['version'],status=DefinitionStatus.PUBLISHED,
            created_by=created_by,created_at=created_at,required_scopes=snapshot.required_scopes,capability_binding=snapshot.binding(),
            object_types=(ref,),governance=ActionGovernanceContract(change_scope=ActionChangeScope(object_types=(ref,),target_systems=tuple(item['target_systems'])),
                risk_level=ActionRiskLevel.LOW,approval_mode=ActionApprovalMode.NONE,policy_refs=(),
                idempotency=ActionIdempotencyPolicy(key_fields=tuple(item['idempotency_key_fields']))),receipt_schema={'type':'object'})
        validate_capability_binding(definition,snapshot)
        rows.append({'definition':definition.model_dump(mode='json'),'capability':snapshot.model_dump(mode='json')})
    if select is not None and {r['definition']['stable_name'] for r in rows}!=set(select):raise BusinessActionsRejected('selected Action not declared')
    return rows


def doctor(manifest,lineage):
    """Pure read-only check of the declaration against what the tenant already publishes (Action definitions are immutable).

    ``lineage`` is control.nexloop_service_grant_action_lineage (configurator). A declared Action already published
    under another Capability is a conflict the deployment must resolve (exclude it with ``select`` after checking that
    the existing one carries the declared profile); one published with the same Capability needs no publication.
    """
    validate(manifest)
    existing={(row['stable_name'],row['version']):row for row in lineage if row.get('world')=='real'}
    findings=[];publish=[]
    for item in manifest['actions']:
        row=existing.get((item['stable_name'],item['version']))
        resource=f"eios:action:{item['stable_name']}:{item['version']}"
        if row is None:publish.append(item['stable_name']);continue
        bound=row['definition'].get('capability_binding',{}).get('capability_name')
        if bound!=item['capability_name'] or row['capability'].get('capability_name')!=item['capability_name']:
            findings.append({'state':'conflict_different_capability','resource_id':resource,'declared':item['capability_name'],'published':bound,
                'remedy':'exclude it with select and decide (owner) which Action carries the profile; published definitions are immutable'})
        else:
            findings.append({'state':'already_published','resource_id':resource,'capability':bound,'active':row.get('active'),
                'remedy':'exclude it with select; the published Action already provides the profile'})
    return {'publish':publish,'findings':findings,'ok':not any(f['state']=='conflict_different_capability' for f in findings)}


def main(argv=None):
    import argparse,sys
    p=argparse.ArgumentParser(description='Business Action declaration doctor (read-only)')
    p.add_argument('command',choices=['doctor']);p.add_argument('--manifest',type=Path,required=True)
    p.add_argument('--tenant',required=True);p.add_argument('--database-url-file',type=Path,required=True)
    a=p.parse_args(argv)
    try:
        from nexloop_eios.service_grants import action_lineage
        report=doctor(load(a.manifest),action_lineage(a.database_url_file,a.tenant))
    except Exception as error:
        print(json.dumps({'ok':False,'error':type(error).__name__}),file=sys.stdout);return 2
    print(json.dumps(report,ensure_ascii=False,indent=2));return 0 if report['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
