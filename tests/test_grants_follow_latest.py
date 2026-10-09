"""follow_latest_version resolution (pure): only declared service grants, only pure schema re-bindings published by review."""
import copy
import json
from pathlib import Path

import pytest
from nexloop_eios import service_grants as G

ROOT=Path(__file__).resolve().parents[1]
MANIFEST=json.loads((ROOT/'deploy/authorization/service-grants.v1.json').read_text())


def action(name,version,*,schema=1,active=True,decision='d',capability=None,change=None,types=('Consumer',)):
    refs=[{'tenant_id':'t','schema_type':'object_type','stable_name':t,'version':schema,'schema_digest':str(schema)*64} for t in types]
    definition={'tenant_id':'t','stable_name':name,'version':version,'status':'published','contract_digest':'c'*64,'object_types':refs,
        'required_scopes':['action.execute'],'governance':{'risk_level':'low','change_scope':{'object_types':copy.deepcopy(refs),'target_systems':['postgres'],'properties':[]}}}
    if change:change(definition)
    return {'resource_id':f'eios:action:{name}:{version}','stable_name':name,'version':version,'world':'real','active':active,'definition':definition,
        'capability':capability or {'capability_name':'ontology.object.edit'},'review_decision_id':decision if version>1 else None}


def derived_ids(lineage,manifest=MANIFEST):
    derived,refused=G.resolve_follow(G.validate(copy.deepcopy(manifest)),lineage)
    return [(g['principal'],g['resource_id']) for g in derived],[(r['successor'],r['reason']) for r in refused]


def test_declared_grant_follows_pure_rebinding_chain():
    lineage=[action('Consumer.edit',1),action('Consumer.edit',2,schema=2,decision='d2'),action('Consumer.edit',3,schema=3,decision='d3')]
    derived,refused=derived_ids(lineage)
    assert derived==[('claim_matcher','eios:action:Consumer.edit:2'),('claim_matcher','eios:action:Consumer.edit:3')] and refused==[]
    effective,follow=G.effective_manifest(G.validate(copy.deepcopy(MANIFEST)),lineage)
    added=[g for g in effective['grants'] if g['resource_id']=='eios:action:Consumer.edit:2']
    assert added[0]['operations']==['execute'] and 'review decision d2' in added[0]['purpose'] and G.FOLLOW not in added[0]


@pytest.mark.parametrize('successor,reason',[
    (dict(capability={'capability_name':'consumer.delete'}),'capability_changed'),
    (dict(change=lambda d:d.update(required_scopes=['action.execute','admin'])),'definition_changed'),
    (dict(change=lambda d:d['governance'].update(risk_level='high')),'governance_changed'),
    (dict(change=lambda d:d['governance']['change_scope'].update(target_systems=['postgres','mail'])),'change_scope_changed'),
    (dict(types=('Consumer','Product')),'resource_scope_changed'),
    (dict(types=('Product',)),'resource_scope_changed'),
    (dict(active=False),'successor_inactive'),
    (dict(decision=None),'not_published_by_review_decision'),
])
def test_successor_that_is_not_a_schema_rebinding_is_refused_and_stops_the_chain(successor,reason):
    lineage=[action('Consumer.edit',1),action('Consumer.edit',2,schema=2,**successor),action('Consumer.edit',3,schema=3,decision='d3')]
    derived,refused=derived_ids(lineage)
    assert derived==[] and refused==[('eios:action:Consumer.edit:2',reason)]


def test_undeclared_cross_action_and_broken_chain_are_not_followed():
    lineage=[action('Consumer.edit',1),action('Consumer.edit',3,schema=3,decision='d3'),  # gap: no :2
             action('Product.create',1,types=('Product',)),action('Product.create',2,schema=2,decision='p2',types=('Product',)),
             action('Consumer.create',2,schema=2,decision='c2')]                       # other Action, same type
    derived,refused=derived_ids(lineage)
    assert derived==[] and refused==[]
    # Base not published in this tenant: nothing derived, nothing reported.
    assert derived_ids([])==([],[])


def test_declaration_only_on_action_grants_and_policy_still_applies():
    bad=copy.deepcopy(MANIFEST)
    bad['grants']=[dict(g,follow_latest_version=True) if g['resource_type']=='object_type' else g for g in bad['grants']]
    with pytest.raises(G.ServiceGrantsRejected,match='follow_latest_version_shape'):G.validate(bad)
    bad=copy.deepcopy(MANIFEST);bad['grants'][0]['follow_latest_version']='yes'
    with pytest.raises(G.ServiceGrantsRejected,match='follow_latest_version_shape'):G.validate(bad)
    bad=copy.deepcopy(MANIFEST)
    bad['grants'].append({'principal':'claim_matcher','resource_type':'action','resource_id':'eios:action:ontology.schema.review:1','operations':['execute'],
        'purpose':'x','source_task':'x','follow_latest_version':True})
    with pytest.raises(G.ServiceGrantsRejected,match='excluded_by_policy'):G.validate(bad)
