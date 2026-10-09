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
from dataclasses import dataclass
import json
import re
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


def build_publication(basis):
    """Next schema version + successor Actions through the EIOS models; failures become gate reasons."""
    kind=basis['kind'];p=basis['candidate']['proposed']
    try:
        if kind=='object_type':
            name=''.join(w[:1].upper()+w[1:] for w in p['name'].split('_'))
            schema=ObjectTypeDefinition(type_name=name,display_name=p['display_name'],description=p.get('description',''),only_edit_via_actions=True)
            return {'schema':schema.model_dump(mode='json'),'actions':[]}
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

    def basis(self,candidate_id):
        return self._call('nexloop_read_review_publication_basis',{'candidate_id':candidate_id})

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
    """HTTP ports for one inspected Human session; every call re-authenticates it."""
    pool:object
    signer:object
    lock:object
    inspected_session:object

    def _port(self):
        from nexloop_eios.browser_authorization import authenticate_browser_business
        return ReviewDecisionPort(self.pool,authenticate_browser_business(self.pool,self.inspected_session,world='real'),self.signer)

    def _run(self,method,**arguments):
        with self.lock:
            return getattr(self._port(),method)(**arguments)

    def review_queue(self,*,limit=50):return self._run('pending',limit=limit)
    def review_candidate(self,*,candidate_id):return self._run('candidate',candidate_id=candidate_id)
    def decide(self,**arguments):return self._run('decide',**arguments)


def workbench_ports(backend,inspected_session):
    """Wiring for http_api (backend.py is not modified): its pool, signer and request lock."""
    backend._assert_open()
    return ReviewWorkbenchPorts(backend._pool,backend._signer,backend._lock,inspected_session)


class ReviewReflowWorker:
    """Service side: reflow recall and re-match Claims of merged / published decisions."""

    def __init__(self,pool,session,signer,*,gluer):
        self.port=_Port(pool,session,signer);self.gluer=gluer

    def run_pending(self):
        results={}
        for item in self.port.call('nexloop_review_reflow',{'verb':'pending'}):
            try:
                if item['outcome']=='merged':outcome=self.gluer.reflow_merged(item['candidate_id'])
                elif item['outcome']=='published':outcome=self.gluer.reflow_published(item['candidate_id'])
                else:continue
                rematched=outcome.rematched or {}
            except Exception as error:
                # Typically the successor Action / new property is not granted yet: keep waiting, retry later.
                report={'applied':0,'claims':{},'error':type(error).__name__}
                self.port.call('nexloop_review_reflow',{'verb':'record','decision_id':item['decision_id'],'status':'waiting','report':report})
                results[item['decision_id']]={'status':'waiting',**report};continue
            applied=sum(1 for r in rematched.values() if r.get('applied')=='applied')
            # A Claim not applied yet (e.g. successor Action / new property grant not configured) keeps waiting.
            status='done' if applied==len(rematched) else 'waiting'
            report={'applied':applied,'claims':rematched,'published_refs':(item.get('publication') or {}).get('published_refs',[])}
            self.port.call('nexloop_review_reflow',{'verb':'record','decision_id':item['decision_id'],'status':status,'report':report})
            results[item['decision_id']]={'status':status,**report}
        return results
