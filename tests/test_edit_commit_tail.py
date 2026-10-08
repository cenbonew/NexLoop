"""Real governed EDIT / current signed authority over fixture-owned PostgreSQL."""
from datetime import UTC,datetime,timedelta
from pathlib import Path
import hmac,json
import pytest
from effect_execution_fixture import execution_plan


def shorten(monkeypatch,kind):
    import nexloop_eios.object_edits as module
    captured=[]
    original=module.canonical_payload
    if kind=='lease':
        original_govern=module.govern_published_action
        def govern(*args,**kwargs):
            command=kwargs['claim_request']
            kwargs['claim_request']=command.model_copy(update={'lease_expires_at':datetime.now(UTC)+timedelta(seconds=1.2)})
            return original_govern(*args,**kwargs)
        monkeypatch.setattr(module,'govern_published_action',govern)
    def canonical(value):
        if isinstance(value,dict) and value.get('protocol')=='nexloop-object-edit-v1':
            value=json.loads(original(value));captured.append(value['permit']['claim']); until=(datetime.now(UTC)+timedelta(seconds=1.2)).isoformat()
            if kind=='action':value['expires_at']=until
            if kind=='object':value['object_authority']['expires_at']=until
            if kind=='property':
                for proof in value['property_authorities']:proof['expires_at']=until
            if kind=='permit':value['permit']['expires_at']=until
        return original(value)
    monkeypatch.setattr(module,'canonical_payload',canonical)
    return captured

@pytest.mark.parametrize('kind',['action','object','property','permit','lease'])
def test_actual_update_crossing_proof_ttl_is_rolled_back(execution_plan,admin,monkeypatch,kind):
    p=execution_plan; before,revision=admin.execute('select properties,nexloop_revision from ontology.objects where object_id=%s',(p['goal'],)).fetchone()
    admin.execute('create sequence public.audit_fields_written')
    admin.execute('create sequence public.audit_terminal_written')
    admin.execute('grant usage on sequence public.audit_fields_written,public.audit_terminal_written to nexloop_owner')
    admin.execute("create function public.audit_terminal() returns trigger language plpgsql as $$begin if new.claim->>'state'='terminal' then perform nextval('public.audit_terminal_written');end if;return new;end$$")
    admin.execute('create trigger audit_terminal after update on runtime.nexloop_action_claims for each row execute function public.audit_terminal()')
    admin.execute("create function public.audit_edit_delay() returns trigger language plpgsql as $$begin perform nextval('public.audit_fields_written');perform pg_sleep(2);return new;end$$")
    admin.execute("create trigger audit_edit_delay after update on ontology.objects for each row execute function public.audit_edit_delay()")
    captured=shorten(monkeypatch,kind)
    caught=False
    try:
        p['goal_editor'].edit_object(action_name='Goal.edit',action_version=1,intent_id='tail-expiry',type_name='Goal',object_id=p['goal'],expected_revision=revision,properties={'state':'closed'})
    except Exception as exc:
        # Public adapter may preserve PostgreSQL InsufficientPrivilege; no arbitrary failure qualifies.
        assert getattr(exc,'sqlstate',None)=='42501',type(exc).__name__
        caught=True
    after,current=admin.execute('select properties,nexloop_revision from ontology.objects where object_id=%s',(p['goal'],)).fetchone()
    claim=admin.execute("select claim from runtime.nexloop_action_claims where intent_id='tail-expiry'").fetchone()
    assert admin.execute('select is_called from public.audit_fields_written').fetchone()[0] is True
    assert admin.execute('select is_called from public.audit_terminal_written').fetchone()[0] is True
    assert claim[0]==captured[-1], 'original active claim including lease/fence changed'
    assert caught and after==before and current==revision and claim[0]['state']=='active', 'expired proof committed object/revision/terminal claim'

def test_partial_edit_normally_preserves_properties(execution_plan,admin):
    p=execution_plan
    before,revision=admin.execute('select properties,nexloop_revision from ontology.objects where object_id=%s',(p['goal'],)).fetchone()
    p['goal_editor'].edit_object(action_name='Goal.edit',action_version=1,intent_id='normal-tail',type_name='Goal',object_id=p['goal'],expected_revision=revision,properties={'state':'closed'})
    after,current=admin.execute('select properties,nexloop_revision from ontology.objects where object_id=%s',(p['goal'],)).fetchone()
    assert after=={**before,'state':'closed'} and current==revision+1
