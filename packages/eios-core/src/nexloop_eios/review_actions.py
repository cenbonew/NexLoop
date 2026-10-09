"""NX-044 / ADR-019 §3.5: governed human review decisions and the human-approved Schema publication.

ReviewDecisionPort runs for an actual Human browser session only (service and
agent credentials are refused in Python and again in SQL). For approve, the
reviewer's backend builds the next schema version and the successor Action
versions with the EIOS models, `assert_object_compatible` and
`schema_contract_digest`; the SQL definer re-derives the expected additive
change, applies the docs/08 §5 gates and publishes atomically — or records a
publication_failed decision and leaves the candidate pending_review.

Publication writes no authority fact (ADR-020 §3). ReviewReflowWorker (service
credential) then reflows recall and re-matches the re-pointed Claims; Claims
whose successor Action/property grants are not configured yet stay waiting.
"""
import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
import uuid

import psycopg
from eios.ontology.definitions import ActionDefinition
from eios.ontology.models import ObjectTypeDefinition,PropertyDefinition,PropertyGroup,PropertyValueType
from eios.ontology.semantics import assert_object_compatible,schema_contract_digest
from nexloop_eios.candidate_merge import ReviewQueueReader
from nexloop_eios.claim_matching import _Port

DECISIONS=('approve','merge_into','reject')
PROTOCOL='nexloop-review-decision-v1'
NAMESPACE=uuid.UUID('9b0c44c4-6e2b-4b1f-8f0a-2d0e5b1a0044')
VALUE_TYPES={'string':PropertyValueType.STRING,'integer':PropertyValueType.INTEGER,'decimal':PropertyValueType.NUMBER,
             'boolean':PropertyValueType.BOOLEAN,'datetime':PropertyValueType.DATETIME}
_KEY=re.compile(r'[A-Za-z0-9._~-]{16,160}')


class ReviewDecisionRejected(ValueError):
    pass


def _human(session):
    from nexloop_eios.browser_authorization import BrowserBusinessSession
    if type(session) is not BrowserBusinessSession or session.authentication.subject_kind.value!='human' or session.agent_invocation is not None:
        raise PermissionError('review decisions require an authenticated human session')
    return session


TYPE_ACTION_CREATOR='nexloop-review-publication'


def type_actions(basis,schema):
    """<Type>.create:1 / <Type>.edit:1 for a new type: the tenant's existing create/edit Capability snapshots,
    canonical low-risk / no-approval / policy-free governance (0093 checks the exact shape). Grants nothing."""
    from datetime import UTC,datetime
    from eios.ontology.definitions import (ActionChangeScope,ActionGovernanceContract,ActionIdempotencyPolicy,ActionRiskLevel,
        ActionApprovalMode,DefinitionStatus,OntologySchemaReference,OntologySchemaType)
    from eios.ontology.version_resolution import CapabilityContractSnapshot,validate_capability_binding
    capabilities=basis.get('type_action_capabilities') or {}
    tenant=basis['candidate']['tenant_id'];actions=[]
    for suffix in ('create','edit'):
        raw=capabilities.get('ontology.object.'+suffix)
        if raw is None:raise ReviewDecisionRejected('type_action_capability_unavailable:ontology.object.'+suffix)
        snapshot=CapabilityContractSnapshot.model_validate_json(json.dumps(raw))
        ref=OntologySchemaReference(tenant_id=tenant,schema_type=OntologySchemaType.OBJECT_TYPE,stable_name=schema.type_name,
            version=schema.version,schema_digest=schema_contract_digest(schema))
        definition=ActionDefinition(tenant_id=tenant,stable_name=f'{schema.type_name}.{suffix}',version=1,status=DefinitionStatus.PUBLISHED,
            created_by=TYPE_ACTION_CREATOR,created_at=datetime.now(UTC),required_scopes=snapshot.required_scopes,capability_binding=snapshot.binding(),
            object_types=(ref,),governance=ActionGovernanceContract(change_scope=ActionChangeScope(object_types=(ref,),target_systems=('postgres',)),
                risk_level=ActionRiskLevel.LOW,approval_mode=ActionApprovalMode.NONE,policy_refs=(),
                idempotency=ActionIdempotencyPolicy(key_fields=('request_id',))),receipt_schema={'type':'object'})
        validate_capability_binding(definition,snapshot)
        actions.append({'definition':definition.model_dump(mode='json'),'capability':snapshot.model_dump(mode='json')})
    return actions


def build_publication(basis):
    """Next schema version + successor Actions through the EIOS models; failures become gate reasons."""
    kind=basis['kind'];p=basis['candidate']['proposed']
    try:
        if kind=='object_instance':
            # NX-050: nothing to build; SQL checks the approved identity against the published Schema.
            return {'schema':None,'actions':[]}
        if kind=='object_type':
            name=''.join(w[:1].upper()+w[1:] for w in p['name'].split('_'))
            schema=ObjectTypeDefinition(type_name=name,display_name=p['display_name'],description=p.get('description',''),only_edit_via_actions=True)
            return {'schema':schema.model_dump(mode='json'),'actions':type_actions(basis,schema)}
        old=ObjectTypeDefinition.model_validate(basis['schema'])
        if kind=='property':
            if p['value_type'] not in VALUE_TYPES or p.get('closed_vocabulary'):raise ReviewDecisionRejected('closed_or_enum_property_requires_values')
            prop=PropertyDefinition(property_name=p['name'],value_type=VALUE_TYPES[p['value_type']],display_name=p['display_name'],description=p.get('description',''))
            groups=list(old.property_groups)
            index=next((i for i,g in enumerate(groups) if g.group_name==p['property_group']),None)
            if index is None:groups.append(PropertyGroup(group_name=p['property_group'],property_names=(p['name'],)))
            else:groups[index]=groups[index].model_copy(update={'property_names':groups[index].property_names+(p['name'],)})
            new=ObjectTypeDefinition.model_validate({**old.model_dump(mode='json'),'version':old.version+1,
                'properties':[x.model_dump(mode='json') for x in old.properties+(prop,)],'property_groups':[g.model_dump(mode='json') for g in groups]})
        elif kind=='vocabulary_value':
            target=p['property_ref'].rsplit('/',1)[1];properties=[]
            for x in old.properties:
                if x.property_name==target:
                    if x.type_descriptor is None or not x.type_descriptor.enum:raise ReviewDecisionRejected('vocabulary_property_not_closed')
                    x=x.model_copy(update={'type_descriptor':x.type_descriptor.model_copy(update={'enum':x.type_descriptor.enum+(p['value'],)})})
                properties.append(x.model_dump(mode='json'))
            new=ObjectTypeDefinition.model_validate({**old.model_dump(mode='json'),'version':old.version+1,'properties':properties})
        else:raise ReviewDecisionRejected('kind_not_publishable')
        assert_object_compatible(old,new)  # EIOS: additive only (no removal, no type change, same primary key)
        digest=schema_contract_digest(new)
        actions=[]
        for row in basis['actions']:
            body=json.loads(json.dumps(row['definition']));body.pop('contract_digest',None)
            body['version']=body['version']+1
            refs=[dict(r,version=new.version,schema_digest=digest) if r['stable_name']==old.type_name else r for r in body['object_types']]
            body['object_types']=refs
            body['governance']['change_scope']['object_types']=[dict(r,version=new.version,schema_digest=digest) if r['stable_name']==old.type_name else r
                for r in body['governance']['change_scope']['object_types']]
            definition=ActionDefinition.model_validate_json(json.dumps(body))
            actions.append({'definition':definition.model_dump(mode='json'),'capability':row['capability']})
        return {'schema':new.model_dump(mode='json'),'actions':actions}
    except ReviewDecisionRejected as error:
        return {'schema':None,'actions':[],'builder_failures':[str(error)]}
    except Exception as error:
        return {'schema':None,'actions':[],'builder_failures':[type(error).__name__+':'+str(error)[:160]]}


class ReviewDecisionPort(ReviewQueueReader):
    """Human reviewer's governed decisions (and the reads the workbench already has)."""

    def __init__(self,pool,session,signer):
        super().__init__(pool,_human(session),signer)

    def awaiting(self):
        """Decided items whose Claims still wait for reflow or for trusted-configuration grants."""
        return self._call('nexloop_read_review_reflow_status',{})

    def basis(self,candidate_id):
        return self._call('nexloop_read_review_publication_basis',{'candidate_id':candidate_id})

    def derivation_impact(self,candidate_id):
        """Which service principals would automatically read/write the property once approved (0084), its group, restricted or not."""
        return self._call('nexloop_read_review_derivation_impact',{'candidate_id':candidate_id})

    def decide(self,*,candidate_id,decision,expected_revision,rationale,idempotency_key,merge_target_ref=None):
        if decision not in DECISIONS or type(expected_revision) is not int or expected_revision<1:raise ValueError('decision')
        if type(rationale) is not str or not 1<=len(rationale.strip())<=2000:raise ValueError('rationale')
        if type(idempotency_key) is not str or not _KEY.fullmatch(idempotency_key):raise ValueError('idempotency key')
        if (decision=='merge_into')!=(merge_target_ref is not None):raise ValueError('merge target')
        auth=self.session.authentication
        decision_id=str(uuid.uuid5(NAMESPACE,f'{auth.tenant_id}:{auth.subject_principal_id}:{idempotency_key}'))
        payload={'decision_id':decision_id,'candidate_id':candidate_id,'decision':decision,'expected_revision':expected_revision,
            'rationale':rationale.strip(),**({'merge_target_ref':merge_target_ref} if merge_target_ref else {})}
        if decision=='approve':
            basis=self.basis(candidate_id)
            if basis is None:raise LookupError('candidate is not pending review')
            if basis['revision']!=expected_revision:raise psycopg.errors.SerializationFailure('candidate changed; reload the review item')
            payload['publication']=build_publication(basis)
        return self._call('nexloop_review_decide',payload,PROTOCOL)


@dataclass(frozen=True)
class ReviewWorkbenchPorts:
    """HTTP ports for one inspected Human session.

    Reads go through Backend's public reviewer entry (shared lifecycle hold and
    authority_request_scope). Backend has no public decision entry yet, so a
    decision first re-authenticates through the public authenticate_browser_reviewer,
    then runs in authority_request_scope on the read-only _pool/_signer.
    Backend symbols used: authenticate_browser_reviewer, ReviewServices.review_queue,
    ReviewServices.review_candidate, _pool, _signer (no _lock).
    """
    backend:object
    inspected_session:object

    def review_queue(self,*,limit=50):
        return self.backend.authenticate_browser_reviewer(self.inspected_session).review_queue(limit=limit)

    def review_candidate(self,*,candidate_id):
        detail=self.backend.authenticate_browser_reviewer(self.inspected_session).review_candidate(candidate_id=candidate_id)
        if type(detail) is not dict:return detail
        # Shown before approve: service principals that would derive READ/EDIT on the new property.
        # Unverifiable impact is reported as such (null), never as "no impact".
        from nexloop_eios.authorization import authority_request_scope
        try:
            with authority_request_scope():impact=self._decision_port().derivation_impact(candidate_id)
        except Exception:impact=None
        return {**detail,'derivation_impact':impact}

    def _decision_port(self):
        from nexloop_eios.browser_authorization import authenticate_browser_business
        self.backend.authenticate_browser_reviewer(self.inspected_session)  # open backend + current Human authentication
        human=authenticate_browser_business(self.backend._pool,self.inspected_session,world='real')
        return ReviewDecisionPort(self.backend._pool,human,self.backend._signer)

    def review_awaiting(self):
        from nexloop_eios.authorization import authority_request_scope
        with authority_request_scope():return self._decision_port().awaiting()

    def decide(self,**arguments):
        from nexloop_eios.authorization import authority_request_scope
        with authority_request_scope():return self._decision_port().decide(**arguments)


def workbench_ports(backend,inspected_session):
    """Wiring for http_api; backend.py is not modified."""
    return ReviewWorkbenchPorts(backend,inspected_session)


AUTHORITY_ERRORS=('ActionAuthorizationDenied','AuthorizationUnavailable','PermissionError','InsufficientPrivilege','AuthorizationFactDenied')


class ReviewReflowWorker:
    """Service side: reflow recall and re-match Claims of merged / published decisions."""

    def __init__(self,pool,session,signer,*,gluer):
        self.port=_Port(pool,session,signer);self.gluer=gluer

    def run_pending(self):
        results={}
        for item in self.port.call('nexloop_review_reflow',{'verb':'pending'}):
            if item['outcome']=='published' and item.get('kind')=='object_type':
                # New type: reindex its definition, then hand the dependent Claims back to the matcher through
                # the claim-match feed (full four-layer chain; nothing is applied here). Idempotent per run.
                owner=next(r.rsplit(':',1)[1] for r in item['publication']['published_refs'] if r.count(':')==2 and r.startswith('eios:object_type:'))
                self.gluer.indexer.index_object_type(owner)
                results[item['decision_id']]=self.port.call('nexloop_review_reflow',{'verb':'release','decision_id':item['decision_id']})
                continue
            if item['outcome']=='published' and item.get('kind')=='object_instance':
                results[item['decision_id']]=self._instance(item);continue
            try:
                if item['outcome']=='merged':outcome=self.gluer.reflow_merged(item['candidate_id'])
                elif item['outcome']=='published':outcome=self.gluer.reflow_published(item['candidate_id'])
                else:continue
                rematched=outcome.rematched or {}
            except Exception as error:
                # Typically the successor Action / new property is not granted yet: keep waiting, retry later.
                reason='awaiting_grants' if type(error).__name__ in AUTHORITY_ERRORS else 'reflow_error'
                report={'applied':0,'claims':{},'reason':reason,'error':type(error).__name__}
                self.port.call('nexloop_review_reflow',{'verb':'record','decision_id':item['decision_id'],'status':'waiting','report':report})
                results[item['decision_id']]={'status':'waiting',**report};continue
            if item['outcome']=='published' and item.get('kind') in ('property','vocabulary_value'):
                # Entity Claims of the publication go back to the full matcher (instance layer included, NX-050).
                released=self.port.call('nexloop_review_reflow',{'verb':'release','decision_id':item['decision_id']})['released']
                rematched={k:v for k,v in rematched.items() if k not in released}
            applied=sum(1 for r in rematched.values() if r.get('applied')=='applied')
            # A Claim not applied yet (e.g. successor Action / new property grant not configured) keeps waiting.
            status='done' if applied==len(rematched) else 'waiting'
            reason=None if status=='done' else ('awaiting_grants' if any(r.get('applied')=='awaiting_grant' for r in rematched.values()) else 'claims_not_applied')
            report={'applied':applied,'claims':rematched,'published_refs':(item.get('publication') or {}).get('published_refs',[]),**({'reason':reason} if reason else {})}
            self.port.call('nexloop_review_reflow',{'verb':'record','decision_id':item['decision_id'],'status':status,'report':report})
            results[item['decision_id']]={'status':status,**report}
        return results

    def _instance(self,item):
        """NX-050: create the approved instance through the governed <Type>.create (idempotent per decision), bind it,
        then release the dependent Claims to the matcher. Missing create authority keeps the decision waiting."""
        from nexloop_eios.object_actions import GovernedObjectCreator
        if item.get('instance_object_id'):
            # Already created and bound: only (re)release the dependent Claims.
            return {**self.port.call('nexloop_review_reflow',{'verb':'release','decision_id':item['decision_id']}),'object_id':item['instance_object_id']}
        # The type's current latest create Action, re-checked in SQL against the approved identity; otherwise the
        # candidate is returned to review with the reasons (never left waiting).
        plan=self.port.call('nexloop_review_reflow',{'verb':'instance_plan','decision_id':item['decision_id']})
        if plan.get('returned_to_review'):return plan
        name,version=plan['create_action'].removeprefix('eios:action:').rsplit(':',1)
        try:
            created=GovernedObjectCreator(self.port.pool,self.port.session,self.port.signer).create(action_name=name,action_version=int(version),
                intent_id=str(uuid.uuid5(NAMESPACE,'instance:'+item['decision_id'])),type_name=plan['type_name'],properties=dict(plan['properties']))
        except Exception as error:
            reason='awaiting_grants' if type(error).__name__ in AUTHORITY_ERRORS else 'reflow_error'
            report={'applied':0,'claims':{},'reason':reason,'error':type(error).__name__}
            self.port.call('nexloop_review_reflow',{'verb':'record','decision_id':item['decision_id'],'status':'waiting','report':report})
            return {'status':'waiting',**report}
        self.port.call('nexloop_review_reflow',{'verb':'instance_created','decision_id':item['decision_id'],'object_id':created['object_id']})
        return {**self.port.call('nexloop_review_reflow',{'verb':'release','decision_id':item['decision_id']}),'object_id':created['object_id']}


def uncovered_actions(manifest,tenant,*,database_url_file):
    """Read-only: Action versions published by review decisions that the service grant manifest does not cover."""
    from nexloop_eios.trusted_configuration import configurator_connection
    with configurator_connection(database_url_file) as db:
        published=db.execute('select control.nexloop_review_published_actions(%s)',(tenant,)).fetchone()[0]
    from nexloop_eios.service_grants import FOLLOW,action_lineage,resolve_follow
    granted={}
    for g in manifest['grants']:granted.setdefault(g['resource_id'],[]).append({'principal':g['principal'],'operations':g['operations']})
    derived,refused=resolve_follow(manifest,action_lineage(database_url_file,tenant)) if any(g.get(FOLLOW) for g in manifest['grants']) else ([],[])
    auto={}
    for g in derived:auto.setdefault(g['resource_id'],[]).append({'principal':g['principal'],'operations':g['operations']})
    rows=[];automatic=[]
    for item in published:
        if item['resource_id'] in granted:continue
        if item['resource_id'] in auto:
            # Declared follow_latest_version: the next service_grants --apply grants it (not yet applied by this read).
            automatic.append({**item,'status':'will_be_auto_covered','grants':auto[item['resource_id']]});continue
        refusal=[r for r in refused if r['successor']==item['resource_id']]
        rows.append({**item,'predecessor_grants':granted.get(item['predecessor'],[])+auto.get(item['predecessor'],[]),
            **({'follow_latest_refused':refusal} if refusal else {}),
            'suggestion':'add to deploy/authorization/service-grants (new manifest version) if the same principals should use it; human grants need owner approval'})
    return {'tenant':tenant,'manifest_version':manifest.get('manifest_version'),'published_action_versions':len(published),
        'will_be_auto_covered':automatic,'uncovered':rows,'covered':not rows}


TYPE_GROUPS=('channel_tech','demographics','lifestyle','needs_intent','other','pain_point','preference','purchase_behavior','sentiment_attitude','spending_power')


def review_type_coverage(manifest,published,*,owner_restrictions=None,matcher_role='claim_matcher'):
    """Pure: for every type a human review published (its <Type>.create:1 and <Type>.edit:1 came with one decision),
    the minimal trusted configuration the background services need, reusing follow_latest_version (Action successors)
    and the 0084 type derivation (per-object/property READ/EDIT) instead of static per-object grants."""
    by_decision={}
    for item in published:
        m=re.fullmatch(r'eios:action:([A-Z][A-Za-z0-9_]{0,63})\.(create|edit):1',item['resource_id'])
        if m:by_decision.setdefault((item['decision_id'],m.group(1)),set()).add(m.group(2))
    types=sorted({t for (_,t),kinds in by_decision.items() if kinds=={'create','edit'}})
    grants={(g['principal'],g['resource_id']):g for g in manifest['grants']}
    rules={(r['principal'],r['type_name']):r for r in manifest.get('property_access_rules',[])}
    reads={r['principal']:r for r in manifest.get('message_read_rules',[])}
    restricted={r['type_name'] for r in (owner_restrictions or {}).get('restrictions',[])}
    report={'matcher_role':matcher_role,'types':[],'covered':True,'findings':[]}
    if matcher_role not in reads:report['findings'].append({'state':'message_read_rule_missing','principal':matcher_role})
    for t in types:
        missing=[];suggested={'grants':[],'property_access_rules':[],'owner_restrictions':[],'match_config':{
            'edit_actions':{t:[t+'.edit',1]},'create_actions':{t:[t+'.create',1]}}}
        need=[{'principal':matcher_role,'resource_type':'object_type','resource_id':f'eios:object_type:{t}','operations':['read'],
               'purpose':f'Recall and match Claims on the review-approved type {t}.','source_task':'NX-050'}]
        need+=[{'principal':matcher_role,'resource_type':'action','resource_id':f'eios:action:{t}.{k}:1','operations':['execute'],
                'purpose':f'{k} {t} instances / values (reflow creation of approved instances, matched writes); successors follow review publications.',
                'source_task':'NX-050','follow_latest_version':True} for k in ('create','edit')]
        for g in need:
            have=grants.get((g['principal'],g['resource_id']))
            if have is None or (g.get('follow_latest_version') and not have.get('follow_latest_version')):
                missing.append('grant:'+g['resource_id']);suggested['grants'].append(g)
        rule=rules.get((matcher_role,t))
        if rule is None or not rule.get('include_review_published') or set(rule.get('operations',[]))!={'edit','read'}:
            missing.append('property_access_rule:'+t)
            suggested['property_access_rules'].append({'principal':matcher_role,'type_name':t,'operations':['edit','read'],'property_groups':list(TYPE_GROUPS),
                'include_review_published':True,'basis_schema_version':1,'valid_until':'<owner-decided>',
                'purpose':f'Per-object READ/EDIT on {t} derived from the type rule (0084); every property arrives through human review.','source_task':'NX-050'})
        if owner_restrictions is not None and t not in restricted:
            missing.append('owner_restriction:'+t)
            suggested['owner_restrictions'].append({'type_name':t,'restricted_groups':'<owner decides; [] restricts nothing>'})
        report['types'].append({'type_name':t,'missing':missing,'suggested':suggested})
        report['covered']=report['covered'] and not missing
    report['covered']=report['covered'] and not report['findings']
    return report


def type_coverage(manifest,tenant,*,database_url_file,owner_restrictions=None):
    from nexloop_eios.trusted_configuration import configurator_connection
    with configurator_connection(database_url_file) as db:
        published=db.execute('select control.nexloop_review_published_actions(%s)',(tenant,)).fetchone()[0]
    return {'tenant':tenant,'manifest_version':manifest.get('manifest_version'),**review_type_coverage(manifest,published,owner_restrictions=owner_restrictions)}


def main(argv=None):
    p=argparse.ArgumentParser(description='NexLoop review publication doctor (read-only)')
    p.add_argument('command',choices=['uncovered-actions','type-coverage'])
    p.add_argument('--manifest',type=Path,required=True);p.add_argument('--tenant',required=True);p.add_argument('--database-url-file',type=Path,required=True)
    p.add_argument('--owner-restrictions',type=Path)
    a=p.parse_args(argv)
    try:
        manifest=json.loads(a.manifest.read_text())
        if a.command=='type-coverage':
            owner=json.loads(a.owner_restrictions.read_text()) if a.owner_restrictions else None
            result=type_coverage(manifest,a.tenant,database_url_file=a.database_url_file,owner_restrictions=owner)
        else:result=uncovered_actions(manifest,a.tenant,database_url_file=a.database_url_file)
    except Exception as error:
        print(json.dumps({'ok':False,'error':type(error).__name__}));return 2
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['covered'] else 1


if __name__=='__main__':sys.exit(main())
