"""Synthetic v6 pack inputs shared by the contract example and the v6 tests."""
import copy,json
from pathlib import Path
from nexloop_eios.context_engine.budget import Item
from nexloop_eios.context_engine.strategy import load_builtins
from nexloop_eios.service_offerings import json_export_example

ROOT=Path(__file__).resolve().parents[1]
STRATEGY=next(s for s in load_builtins(ROOT/'deploy/configuration/context-strategies.v1.json') if s['strategy_id']=='ontology_hybrid')
D='decision:'+'d'*64
TENANT='3f2b8c1e-5d4a-4f6b-9a8e-1c2d3e4f5a6b';RUN='7a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d'
IDS={k:h*64 for k,h in zip(('consumer','goal','step','control','message','offering','binding'),'abcde01')}


def inputs():
    offering=json_export_example(valid_until='2026-12-31T00:00:00+00:00')
    return dict(strategy=copy.deepcopy(STRATEGY),
        bindings={'tenant_id':TENANT,'world_id':'real','run_id':RUN,'source_principal':'nexloop-service-context-assembler-principal',
            'context_id':'0c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f','namespace':'e'*64,'artifact_id':'f'*32,'command_digest':'a'*64},
        role=None,
        current_event={'kind':'consumer_message','message_id':IDS['message'],'provenance':'eios:object:'+IDS['message']},
        user_statement={'message_id':IDS['message'],'conversation_id':'c'*64,'sequence':3,
            'body':'未来两周不要发促销。付款页面一直报错，解决后我再考虑续费。','provenance':'eios:object:'+IDS['message']},
        goal={'goal_version_refs':['goal:q4-stage@2'],'control_snapshot':{'control_revision':7,'scopes':[{'kind':'consumer','ref':'consumer:'+IDS['consumer']}],
            'goals':[{'goal_id':'q4-stage','version':2}],'objects':[{'type_name':'Consumer','object_id':IDS['consumer'],'revision':4}],'budgets':['model']}},
        formal_facts=[{'type':t,'id':IDS[k],'revision':1,'provenance':'eios:object:'+IDS[k]} for t,k in (('Consumer','consumer'),('Goal','goal'),('PlanStep','step'),('EffectControl','control'))],
        current_constraints={'action':'nexloop.service.request:1','allow_effect':True,'budget_units':1,'reserved_units':0,'valid_until':'2026-12-31T00:00:00+00:00','executor_principal':'nexloop-service-executor'},
        supply={'offering_id':IDS['offering'],'offering_revision':1,'binding_id':IDS['binding'],'binding_revision':1,'provenance':'eios:object:'+IDS['offering'],'properties':offering},
        items=[
            Item('constraints','Consumer','eios:object:Consumer/'+IDS['consumer'],'4',{'type':'Consumer','properties':{'promotion_contact':'paused'}},'formal_object',D,relevance=1.0),
            Item('consumer_state','Consumer','eios:object:Consumer/'+'9'*64,'2',{'type':'Consumer','properties':{'budget_level':'两千元以内'}},'formal_object',D,relevance=0.9),
            Item('open_work','outbound','nexloop:outbound:5f0c8c8e-3b1a-4c6d-9e2f-1a2b3c4d5e6f','unknown',{'delivery_state':'unknown','note':'not confirmed as delivered to the consumer'},'execution_state',D,relevance=1.0,tags={'unconfirmed'}),
            Item('evidence','claim_evidence','claim:'+'1'*64,'nx019-extractor/2',{'quote':'解决后我再考虑续费','message_ref':'eios:object:Message/'+IDS['message'],'span':[19,28],'speaker':'consumer','epistemic_kind':'intent','resolution_state':'unresolved','modality':'conditional','condition':'解决后'},'user_statement',D,relevance=0.8,at='2026-10-09T02:00:00Z'),
            Item('evidence','conversation','eios:object:Message/'+'2'*64,'2',{'speaker':'agent','body':'我们正在排查付款问题。','sequence':2,'accepted_at':'2026-10-09T01:59:00Z'},'conversation',D,relevance=0.5,at='2026-10-09T01:59:00Z'),
            Item('semantics','property','eios:property:Consumer/budget_level','1',{'display_name':'预算区间','closed_vocabulary':['两千元以内','两千到五千元','五千元以上']},'schema',D,relevance=0.7)])


def example():
    from nexloop_eios.context_engine.pack import assemble_v6
    return assemble_v6(**inputs())[0]


if __name__=='__main__':print(json.dumps(example(),ensure_ascii=False,indent=2))
