-- K4d (owner 2026-11-01, dsh intake v2 §3.3 and Q-3; contract packages/contracts/workbench-pending.schema.json): the workbench home
-- "waiting for me" read, GET /api/v1/workbench/pending (workbench read verb `pending`). Temporary number; the dispatcher renumbers.
-- Read only: no row is written by it (no audit row either: it returns counts, references and question summaries, no message content
-- and no Consumer property). What the signed-in member may act on, from the member's current role (never from a question's audience):
--   question          open human questions (K4b) whose kind lists the member's role, before due_by (an erased one, its text blanked, is not listed)
--   approval          approval requests waiting, before due_by, that another member asked for (NX-056)   owner
--   remediation       remediation intents proposed or escalated to the owner (NX-057)                    owner
--   reply_escalation  reply escalations (ADR-023 §2.7) whose message still has no bound reply             owner, operator
--   takeover_expiry   takeovers in effect that end within the next hour (owner: all; operator: own)      owner, operator
--   candidate_review  candidate definitions pending review (ADR-019)                                      owner, reviewer
-- A category the role cannot act on answers count 0. Alert acknowledgement is not a category.
-- Authorization: the 0130 signed-read verification, for this verb; the read Action is nexloop.workbench.read:1 (owner, operator) or
-- ontology.schema.review:1 (the reviewer, who holds no workbench read: it sees the candidate category only, never a 403).
-- authz.nexloop_workbench_read is renamed and wrapped (the 0246 / 0345 / 0359 / 0367 pattern; every other verb unchanged).
set local timezone = 'UTC';

create function authz.nexloop_workbench_pending(p_tenant text,p_world text,p_principal text,p_role text,p_after uuid,p_limit integer) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);now_at timestamptz:=clock_timestamp();cats jsonb;qs jsonb;after_at timestamptz;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 if p_after is not null then
  select q.created_at into after_at from runtime.nexloop_human_questions q where q.tenant_id=p_tenant and q.world=p_world and q.question_id=p_after;
  if not found then raise exception 'workbench read invalid' using errcode='22023';end if;
 end if;
 with questions as (
  select q.* from runtime.nexloop_human_questions q join control.nexloop_human_question_kinds k on k.version=q.kinds_version
  where q.tenant_id=p_tenant and q.world=p_world and q.state='open' and q.due_by>now_at and q.text<>'' and (k.definition->'kinds'->q.question_kind) ? p_role),
 items(category,n,ord,at,ref) as (
  select 'question',1,1,q.created_at,'question:'||q.question_id from questions q
  union all
  select 'approval',1,2,a.requested_at,'approval:'||a.approval_id from control.nexloop_approval_requests a
   where p_role='owner' and a.tenant_id=p_tenant and a.world=p_world and a.status='waiting_approval' and a.due_by>now_at
    and a.requester_principal is distinct from p_principal
  union all
  select 'remediation',1,3,r.created_at,'remediation:'||r.remediation_id from runtime.nexloop_remediation_intents r
   where p_role='owner' and r.tenant_id=p_tenant and r.world=p_world and r.state in ('proposed','escalated')
  union all
  select 'reply_escalation',1,4,e.escalated_at,'message:'||e.message_id from control.nexloop_reply_escalations e
   where p_role in ('owner','operator') and e.tenant_id=p_tenant and e.world=p_world
    and not exists(select 1 from runtime.nexloop_outbound_messages o where o.tenant_id=e.tenant_id and o.world=e.world and o.trigger_message_id=e.message_id)
  union all
  select 'takeover_expiry',1,5,t.started_at,'takeover:'||t.takeover_id from control.nexloop_takeovers t
   where p_role in ('owner','operator') and t.tenant_id=p_tenant and t.world=p_world and t.ended_at is null
    and t.expires_at>now_at and t.expires_at<=now_at+interval '1 hour' and (p_role='owner' or t.taken_by=p_principal)
  union all
  select 'candidate_review',1,6,c.created_at,'candidate:'||c.candidate_id from ontology.nexloop_candidate_definitions c
   where p_role in ('owner','reviewer') and c.tenant_id=p_tenant and c.world=p_world and c.status='pending_review'),
 names(category,ord) as (values ('question',1),('approval',2),('remediation',3),('reply_escalation',4),('takeover_expiry',5),('candidate_review',6))
 select jsonb_agg(jsonb_build_object('category',x.category,'count',coalesce(s.n,0),
   'oldest_waiting_seconds',case when s.oldest is null then null else greatest(0,floor(extract(epoch from now_at-s.oldest)))::bigint end,
   'oldest_ref',s.ref) order by x.ord) into cats
 from names x left join lateral (select count(*) n,min(i.at) oldest,(array_agg(i.ref order by i.at,i.ref))[1] ref from items i where i.category=x.category) s on true;
 -- A takeover's waiting time is the time since it started (its end is within the hour); oldest_ref is the one started first.
 select jsonb_agg(jsonb_build_object('question_id',q.question_id,'audience',q.audience,'question_kind',q.question_kind,'text',left(q.text,120),
   'options',q.options,'subject_ref',q.subject_ref,'withdrawable',q.withdrawable,'created_at',runtime.nexloop_commercial_ts(q.created_at),
   'due_by',runtime.nexloop_commercial_ts(q.due_by),'answer',jsonb_build_object('operation','nexloop.human_question.answer:1'))
   order by (q.audience=p_role) desc,q.created_at,q.question_id) into qs
 from (select q.* from runtime.nexloop_human_questions q join control.nexloop_human_question_kinds k on k.version=q.kinds_version
   where q.tenant_id=p_tenant and q.world=p_world and q.state='open' and q.due_by>now_at and q.text<>'' and (k.definition->'kinds'->q.question_kind) ? p_role
    and (after_at is null or (q.created_at,q.question_id)>(after_at,p_after))
   order by q.created_at,q.question_id limit p_limit) q;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return jsonb_build_object('all_clear',not exists(select 1 from jsonb_array_elements(cats) e where (e->>'count')::integer>0),
  'last_health_check_at',null,'categories',cats,'questions',coalesce(qs,'[]'::jsonb));
end $$;
alter function authz.nexloop_workbench_pending(text,text,text,text,uuid,integer) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_pending(text,text,text,text,uuid,integer) from public;

alter function authz.nexloop_workbench_read(text,text,text,text,text) rename to nexloop_workbench_read_before_pending;
revoke all on function authz.nexloop_workbench_read_before_pending(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_workbench_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;c jsonb:=p_payload::jsonb;k bytea;w jsonb;cfg control.nexloop_workbench_configurations;v_tenant text;v_action text;v_role text;
begin
 if c->>'verb' is distinct from 'pending' then
  return authz.nexloop_workbench_read_before_pending(p_digest,p_world,p_text,p_signature,p_payload);end if;
 -- The 0130 verification, for this verb (as the remediations wrapper), with either read Action.
 v_action:=a->>'action_resource';
 if session_user is distinct from 'nexloop_api' or p_world is distinct from 'real'
  or octet_length(p_text)>1048576 or octet_length(p_payload)>8192
  or a->>'protocol' is distinct from 'nexloop-workbench-read-v1' or a->>'operation' is distinct from 'execute'
  or v_action is null or v_action not in ('eios:action:nexloop.workbench.read:1','eios:action:ontology.schema.review:1') or a->>'resource_id' is distinct from v_action
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'workbench read unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-workbench-read-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp() or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'workbench read unavailable' using errcode='42501';end if;
 w:=authz.nexloop_workbench_member(p_digest,p_world);v_tenant:=w->>'tenant_id';cfg:=control.nexloop_workbench_current(v_tenant);v_role:=w->>'role';
 if a->>'tenant_id' is distinct from v_tenant or a->>'principal_id' is distinct from w->>'principal_id' or not (cfg.roles->v_role ? v_action)
  or (v_action='eios:action:ontology.schema.review:1' and v_role<>'reviewer') then
  raise exception 'workbench read forbidden' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 if exists(select 1 from jsonb_object_keys(c) x where x not in ('verb','after','limit'))
  or (c ? 'after' and coalesce(c->>'after','')!~'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
  or (c ? 'limit' and (coalesce(c->>'limit','')!~'^[1-9][0-9]?$' or (c->>'limit')::integer>20)) then
  raise exception 'workbench read invalid' using errcode='22023';end if;
 return authz.nexloop_workbench_pending(v_tenant,p_world,w->>'principal_id',v_role,(c->>'after')::uuid,coalesce((c->>'limit')::integer,20));
end $$;
alter function authz.nexloop_workbench_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_read(text,text,text,text,text) to nexloop_api;
