"""10/18 stage (refusal-2, restricted, fallback dc7ff6f3): the fallback Context was refused with context source content mismatch.
A source read for the pack can change before SQL re-verifies it at bind. The Run's Artifact is fixed, so the pack is not bound
again; the sources are re-read once and the escalation names the changed subsections (codes, never a value), or
context_source_content_mismatch_stable when the pack disagrees with unchanged sources. Actual PG, actual fallback launcher, restricted consumer."""
import dataclasses
import json

import pytest

from local_message_assembly_fixture import assembled_message,business_plan,configured  # noqa: F401
from test_outbound_messages_pg import chain
from test_reply_fallback_pg import guarantor,make_due,provision,silent

pytestmark=pytest.mark.reply_fallback


def _tamper(items):
    """The turn or Claim read for the pack, as if it changed before bind (here: the pack's copy differs from the row)."""
    for n,item in enumerate(items):
        if item.section=='evidence' or item.section=='consumer_state':
            return [*items[:n],dataclasses.replace(item,content={**item.content,'stale':True} if isinstance(item.content,dict) else item.content),*items[n+1:]]
    raise AssertionError('no source item to change')


def _restricted(f,admin,tmp_path,monkeypatch,tamper_calls):
    from nexloop_eios.context_engine import sources
    original=sources.collect;calls=[]
    def collect(*args,**kwargs):
        found=original(*args,**kwargs);calls.append(1)
        if len(calls) in tamper_calls:found.items=_tamper(found.items)
        return found
    monkeypatch.setattr(sources,'collect',collect)
    return provision(f,admin),calls


def test_a_source_changed_before_bind_is_named_in_the_escalation(assembled_message,admin,tmp_path,monkeypatch):  # noqa: F811
    f=assembled_message;token,calls=_restricted(f,admin,tmp_path,monkeypatch,{1})
    with chain(f,admin,tmp_path,body='以后别再给我发消息了，不过退款到账了吗？',host_update=silent) as c:
        c['recipe']=f['recipe']
        assert admin.execute('select active from control.nexloop_contact_restrictions where tenant_id=%s',(c['tenant'],)).fetchone()==(True,)
        with guarantor(c,admin,tmp_path,token) as g:
            make_due(admin)
            assert g['worker']().run_once()['escalated']==1
            assert len(calls)==2  # the pack, the re-read; never a second bind
            (escalation,)=g['read']().escalations()
            assert escalation['reason']=='fallback_failed' and escalation['detail']['restricted'] is True
            assert escalation['detail']['check'].startswith('context_source_changed:') and 'stale' not in json.dumps(escalation)


def test_a_pack_that_disagrees_with_unchanged_sources_is_refused_once_and_named(assembled_message,admin,tmp_path,monkeypatch):  # noqa: F811
    f=assembled_message;token,calls=_restricted(f,admin,tmp_path,monkeypatch,{1,2,3})
    with chain(f,admin,tmp_path,body='以后别再给我发消息了，不过退款到账了吗？',host_update=silent) as c:
        c['recipe']=f['recipe']
        with guarantor(c,admin,tmp_path,token) as g:
            make_due(admin)
            assert g['worker']().run_once()['escalated']==1
            assert len(calls)==2  # no second bind
            (escalation,)=g['read']().escalations()
            assert 'context_source_content_mismatch_stable' in json.dumps(escalation)
