"""ADR-023 §2.6 over the actual NX-047 chain: a restricted Consumer's own message still gets its one bound reply.

Human HTTPS Message "以后别再给我发消息了" → (0109) contact restriction in the message's own transaction → relay → Pi
Run → governed service.request intent with its outbound record (trigger_message_id derived from the message Run) →
effect worker: the bound reply dispatches and is delivered, which settles the pending reply. The same reply outside
the configured window is refused at dispatch (zero deliveries). Synthetic data; admin probes and, for the window case,
moves the inbound message's acceptance time back (explicit time injection, documented).
"""
import json

from local_message_assembly_fixture import assembled_message,business_plan,configured  # noqa: F401
from test_outbound_messages_pg import chain,delivery_material,delivery_service,effect_worker,outbound

REFUSAL='以后别再给我发消息了'


def restriction(admin,tenant):
    return admin.execute('select active,rule_id,message_id from control.nexloop_contact_restrictions where tenant_id=%s',(tenant,)).fetchone()


def pending(admin,tenant):
    return [r[0] for r in admin.execute("select item_key from runtime.nexloop_work_feed where tenant_id=%s and feed='reply-due'",(tenant,)).fetchall()]


def test_restricted_consumer_gets_its_one_bound_reply_and_it_settles(assembled_message,admin,tmp_path):
    f=assembled_message
    with chain(f,admin,tmp_path,body=REFUSAL) as c:
        tenant=c['tenant'];consumer=c['consumer']
        assert restriction(admin,tenant)==(True,'stop-contact',consumer['id'])
        assert pending(admin,tenant)==['reply:'+consumer['id']]
        ((intent,state,_,_,_,trigger,_),)=outbound(admin,tenant)
        assert state=='persisted' and trigger==consumer['id']  # bound by the server, not by the model
        material=delivery_material(tmp_path)
        with delivery_service(c,tmp_path,material):
            assert 'fulfilled' in effect_worker(c,tmp_path,material['provider_config'],material['secret'])
        assert outbound(admin,tenant)[0][1]=='delivered'
        assert pending(admin,tenant)==[]  # the bound reply the channel accepted settled the pending reply
        assert restriction(admin,tenant)[0] is True  # a reply never lifts the restriction


def test_bound_reply_outside_the_window_is_refused(assembled_message,admin,tmp_path):
    f=assembled_message
    with chain(f,admin,tmp_path,body=REFUSAL) as c:
        tenant=c['tenant'];consumer=c['consumer']
        window=admin.execute('select control.nexloop_reply_window_seconds()').fetchone()[0]
        with admin.transaction():
            admin.execute("select set_config('eios.tenant_id',%s,true)",(tenant,))
            record=admin.execute('select record from runtime.nexloop_conversation_messages where tenant_id=%s and message_id=%s',(tenant,consumer['id'])).fetchone()[0]
            record['accepted_at']=admin.execute('select (clock_timestamp()-make_interval(secs=>%s))::text',(window+5,)).fetchone()[0]
            admin.execute('update runtime.nexloop_conversation_messages set record=%s where tenant_id=%s and message_id=%s',(json.dumps(record),tenant,consumer['id']))
        material=delivery_material(tmp_path)
        with delivery_service(c,tmp_path,material):
            output=effect_worker(c,tmp_path,material['provider_config'],material['secret'])
        assert 'fulfilled' not in output
        assert outbound(admin,tenant)[0][1] in ('persisted','failed')
        assert not any(material['root'].iterdir())  # nothing exported: zero deliveries
        assert pending(admin,tenant)==['reply:'+consumer['id']]  # still unanswered: the reply worker takes it over
