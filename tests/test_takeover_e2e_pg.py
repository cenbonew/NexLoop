"""NX-028 AT-044 with the actual Host/Pi (deterministic protocol; runtime guard as four processes, ADR-022 §4 / ADR-024).

Human HTTPS Message → relay → Pi Run → governed reply intent (outbound record). A person then takes the conversation over:
the queued Agent reply is refused at dispatch (taken over / stale control), the real local JSON delivery provider gets zero
requests, and nothing is materialized. The takeover start is the 0152 handler called by admin (the human governed Action
chain in front of it is covered by test_takeover_pg / test_workbench_actions_http). Synthetic data; run serially.
"""
from psycopg.types.json import Jsonb
from local_message_assembly_fixture import assembled_message,business_plan,configured  # noqa: F401
from test_outbound_messages_pg import chain,delivery_material,delivery_service,effect_worker,outbound


def test_pi_reply_queued_before_the_takeover_never_reaches_the_customer(assembled_message,admin,tmp_path):
    f=assembled_message
    with chain(f,admin,tmp_path,guard_workers=4) as c:
        tenant=c['tenant']
        assert 'succeeded' in c['runtime_output']
        ((intent,state,*_),)=outbound(admin,tenant);assert state=='persisted'
        admin.execute('select control.nexloop_governed_takeover(%s,%s,%s,%s,%s,%s)',(tenant,'real','synthetic-staff-principal','human','takeover-pi',
            Jsonb({'operation':'take_over_conversation','scope_kind':'conversation','scope_ref':c['conversation_id'],'reason':'人工接手'})))
        prediction=admin.execute('select control.nexloop_intent_dispatch_prediction(%s,%s,%s)',(tenant,'real',intent)).fetchone()[0]
        assert prediction['dispatchable'] is False and prediction['reason'] in ('taken_over','control_revision_stale'),prediction
        material=delivery_material(tmp_path)
        with delivery_service(c,tmp_path,material):
            output=effect_worker(c,tmp_path,material['provider_config'],material['secret'])
        assert 'fulfilled' not in output
        assert not any(material['root'].iterdir())  # zero deliveries
        assert outbound(admin,tenant)[0][1] in ('persisted','failed') and c['recorder'].pending()==[]
