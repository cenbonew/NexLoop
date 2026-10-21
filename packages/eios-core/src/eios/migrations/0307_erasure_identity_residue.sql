-- NX-029 / AT-026 erasure coverage, batch 4 (L6 review of batch 3, dispatcher 2026-10-21): what the whole-database search found after a
-- completed Consumer erasure - the Consumer's id or principal left in columns no consumer_id check sees. The migration number is
-- temporary; it lives only in the file name and the catalog.
--
-- On completion (not with a retention hold), under one pseudonym of this step. The completion triggers of batches 2-4 are named
-- zz_nx029_1_identity < zz_nx029_2_reference < zz_nx029_3_text: after every other completion trigger (nx038 semantic access and
-- evolution, nx039, contact preferences read the original ids first), and this one before the conversations change (it reads the
-- Consumer's principals there).
--   runtime.nexloop_conversation_messages  record.actor: the Consumer's principal (L6 1)       text-replaced (the record keeps its shape)
--   runtime.jobs                           normalized_input, plan_snapshot, result, error: run_command.consumer_ref
--                                          consumer:<id> (L6 2) and the principal; actor_id     text-replaced
--   runtime.invocations                    actor_id, result, error: the principal of a customer's invocation  text-replaced
--   runtime.nexloop_action_claims          claim: the governed Consumer.create claim names the Consumer      text-replaced
--   ontology.nexloop_claims                consumer_id (quote and value were erased; the id was left)        pseudonym
--   runtime.nexloop_effect_intents         consumer_id (frozen_request was erased; the id was left)          pseudonym
--   runtime.nexloop_effect_dispatch_controls snapshot: the consumer scope ref                                {"erased":true} (scope)
--   runtime.nexloop_work_feed              items naming the Consumer in their payload (reply-due and the like) are deleted, except
--                                          the recall index deletion of the Consumer itself (the recall worker deletes that item when
--                                          it has removed the index entry)
--   ontology.objects CommercialRecord      consumer_ref: the record is derived again from its verified events (the customer link is
--                                          gone, so the derived record is unlinked; the commercial guard only accepts derived state)
--   control.nexloop_control_scopes         a consumer-scope pause naming the Consumer                         deleted (scope)
--   control.nexloop_control_events         consumer-scope events naming them, except the erasure's own suppression
--                                          event (kept with the suppression, 0132)                             scope_ref pseudonym (scope)
--   control.nexloop_retention_holds        a released Consumer hold's scope_ref                                pseudonym
--   control.nexloop_metric_observations    subject_ref: the Consumer counted by a cohort metric                pseudonym (scope); the
--                                          erased Consumer leaves the cohort count (its cohort member was pseudonymised apart, 0227)
-- Text replacement only writes the pseudonym where the id or principal stood as a whole JSON string ("x" or "consumer:x") or a whole
-- actor value (L6 review 2.1: never inside another id); nothing else in the value changes. Consumer pauses in both forms (x, consumer:x).
--
-- After a retention hold (L6 review 2.3): an erasure that met a Consumer hold ends completed_with_holds and none of its phases (nor
-- batches 1-4) ran. When the last Consumer hold on that Consumer is released, trigger nx029_erasure_resume_after_hold puts the erasure
-- back to purging (first phase); the retention keeper runs its phases again and, on completion, batches 1-4 run as for any erasure. A
-- hold that only expires (valid_until) is not an update: the owner releases it to resume (docs/implementation/erasure-coverage.md).

alter table control.nexloop_metric_observations drop constraint nexloop_metric_observations_subject_ref_check,
 add constraint nexloop_metric_observations_subject_ref_check check(subject_ref~'^[a-f0-9]{64}$' or subject_ref~'^pseudonym:[0-9a-f]{32}$');

create function control.nexloop_consumer_identity_erasure() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v_p text:='pseudonym:'||encode(extensions.gen_random_bytes(16),'hex');
 v_ref text:='consumer-erasure:'||new.request_id;n jsonb:='{}'::jsonb;c integer;tn text:=new.tenant_id;w text:=new.world;k text:=new.consumer_id;
 v_ids text[];x text;r record;v jsonb;
begin
 if new.state<>'completed' or old.state='completed' then return null;end if;
 perform set_config('eios.tenant_id',tn,true);
 if runtime.nexloop_retention_held(tn,w,'consumer',k) then perform set_config('eios.tenant_id',coalesce(v_prior,''),true);return null;end if;
 -- The Consumer's id and principals (read before batch 2 pseudonymises the conversations).
 v_ids:=array(select distinct p from (select principal_id p from runtime.nexloop_conversations where tenant_id=tn and world=w and consumer_id=k
   union select principal_id from control.nexloop_consumer_owners where tenant_id=tn and world=w and consumer_id=k) s where p is not null and p!~'^pseudonym:')||k;
 -- Text replacement in the documents that name them.
 foreach x in array v_ids loop
  update runtime.nexloop_conversation_messages m set record=replace(record::text,'"'||x||'"','"'||v_p||'"')::jsonb
   where m.tenant_id=tn and m.world=w and m.record::text like '%"'||x||'"%'
    and m.conversation_id in (select conversation_id from runtime.nexloop_conversations where tenant_id=tn and world=w and consumer_id=k);
  get diagnostics c=row_count;n:=n||jsonb_build_object('message_records',coalesce((n->>'message_records')::integer,0)+c);
  -- L6 review 2.1: bounded replacement - the id or principal only as a whole JSON string ("x", "consumer:x") or a whole actor value,
  -- never as a substring of another id; candidates narrowed first; the invocations through this world's jobs.
  update runtime.jobs j set normalized_input=replace(replace(normalized_input::text,'"'||x||'"','"'||v_p||'"'),'"consumer:'||x||'"','"consumer:'||v_p||'"')::jsonb,
    plan_snapshot=replace(replace(plan_snapshot::text,'"'||x||'"','"'||v_p||'"'),'"consumer:'||x||'"','"consumer:'||v_p||'"')::jsonb,
    result=replace(replace(result::text,'"'||x||'"','"'||v_p||'"'),'"consumer:'||x||'"','"consumer:'||v_p||'"')::jsonb,
    error=replace(replace(error::text,'"'||x||'"','"'||v_p||'"'),'"consumer:'||x||'"','"consumer:'||v_p||'"')::jsonb,
    actor_id=case when actor_id=x then v_p else actor_id end
   where j.tenant_id=tn and j.world=w and (j.actor_id=x or j.normalized_input->'run_command'->>'consumer_ref'='consumer:'||x
    or strpos(concat_ws(' ',j.normalized_input::text,j.plan_snapshot::text,j.result::text,j.error::text),'"'||x||'"')>0
    or strpos(concat_ws(' ',j.normalized_input::text,j.plan_snapshot::text,j.result::text,j.error::text),'"consumer:'||x||'"')>0);
  get diagnostics c=row_count;n:=n||jsonb_build_object('jobs',coalesce((n->>'jobs')::integer,0)+c);
  update runtime.invocations i set actor_id=case when actor_id=x then v_p else actor_id end,
    result=replace(replace(result::text,'"'||x||'"','"'||v_p||'"'),'"consumer:'||x||'"','"consumer:'||v_p||'"')::jsonb,
    error=replace(replace(error::text,'"'||x||'"','"'||v_p||'"'),'"consumer:'||x||'"','"consumer:'||v_p||'"')::jsonb
   where i.tenant_id=tn and exists(select 1 from runtime.jobs j where j.tenant_id=tn and j.world=w and j.invocation_id=i.invocation_id)
    and (i.actor_id=x or strpos(concat_ws(' ',i.result::text,i.error::text),'"'||x||'"')>0 or strpos(concat_ws(' ',i.result::text,i.error::text),'"consumer:'||x||'"')>0);
  get diagnostics c=row_count;n:=n||jsonb_build_object('invocations',coalesce((n->>'invocations')::integer,0)+c);
  update runtime.nexloop_action_claims set claim=replace(replace(claim::text,'"'||x||'"','"'||v_p||'"'),'"consumer:'||x||'"','"consumer:'||v_p||'"')::jsonb
   where tenant_id=tn and world=w and (strpos(claim::text,'"'||x||'"')>0 or strpos(claim::text,'"consumer:'||x||'"')>0);
  get diagnostics c=row_count;n:=n||jsonb_build_object('action_claims',coalesce((n->>'action_claims')::integer,0)+c);
 end loop;
 update ontology.nexloop_claims set consumer_id=v_p where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('claims',c);
 update runtime.nexloop_effect_intents set consumer_id=v_p where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('effect_intents',c);
 perform runtime.nexloop_erasure_open('runtime.nexloop_effect_dispatch_controls',array['snapshot'],false,v_ref);
 update runtime.nexloop_effect_dispatch_controls set snapshot='{"erased":true}'::jsonb where tenant_id=tn and world=w and snapshot::text like '%"'||k||'"%';
 get diagnostics c=row_count;n:=n||jsonb_build_object('dispatch_controls',c);
 delete from control.nexloop_erasure_scope where txid=txid_current() and table_name='runtime.nexloop_effect_dispatch_controls';
 delete from runtime.nexloop_work_feed where tenant_id=tn and world=w and payload::text like '%"'||k||'"%'
  and not (feed='recall-instance' and payload->>'op'='delete' and payload->>'object_id'=k);
 get diagnostics c=row_count;n:=n||jsonb_build_object('work_feed',c);
 perform runtime.nexloop_erasure_open('control.nexloop_control_scopes',null,true,v_ref);
 delete from control.nexloop_control_scopes where tenant_id=tn and world=w and scope_kind='consumer' and scope_ref in (k,'consumer:'||k);  -- both forms (L6 2.2)
 get diagnostics c=row_count;n:=n||jsonb_build_object('control_scopes',c);
 perform runtime.nexloop_erasure_open('control.nexloop_control_events',array['scope_ref'],false,v_ref);
 update control.nexloop_control_events set scope_ref=v_p where tenant_id=tn and world=w and scope_kind='consumer' and scope_ref in (k,'consumer:'||k)
  and not (event_kind='contact_restricted' and detail->>'rule_id'='consumer_erasure');
 get diagnostics c=row_count;n:=n||jsonb_build_object('control_events',c);
 perform runtime.nexloop_erasure_open('control.nexloop_metric_observations',array['subject_ref'],false,v_ref);
 update control.nexloop_metric_observations set subject_ref=v_p where tenant_id=tn and world=w and subject_ref=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('metric_observations',c);
 delete from control.nexloop_erasure_scope where txid=txid_current() and table_name in ('control.nexloop_control_scopes','control.nexloop_control_events',
  'control.nexloop_metric_observations');
 -- A released Consumer hold names them (an active one would have kept the erasure from completing): history, under the pseudonym.
 update control.nexloop_retention_holds set scope_ref=v_p where tenant_id=tn and world=w and scope_kind='consumer' and scope_ref=k and released_at is not null;
 get diagnostics c=row_count;n:=n||jsonb_build_object('released_holds',c);
 -- CommercialRecords: derived again from their verified events (the customer link is gone).
 c:=0;
 for r in select o.object_id,cr.record_key from ontology.objects o join runtime.nexloop_commercial_records cr on cr.tenant_id=o.tenant_id and cr.world=o.world
   and cr.object_id=o.object_id where o.tenant_id=tn and o.world=w and o.type_name='CommercialRecord' and o.properties->>'consumer_ref'=k loop
  v:=runtime.nexloop_commercial_target(tn,w,r.record_key);
  if v is not null then update ontology.objects set properties=v->'properties' where tenant_id=tn and world=w and type_name='CommercialRecord' and object_id=r.object_id;c:=c+1;end if;
 end loop;
 n:=n||jsonb_build_object('commercial_records',c);
 perform runtime.nexloop_erasure_tombstone(tn,w,'consumer_erasure',v_ref,'consumer_identity','consumer_identity',k,'pseudonymized',n,'nexloop_owner');
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return null;
end $$;
alter function control.nexloop_consumer_identity_erasure() owner to nexloop_owner;
revoke all on function control.nexloop_consumer_identity_erasure() from public;
create trigger zz_nx029_1_consumer_identity_erasure after update of state on control.nexloop_consumer_erasures for each row execute function control.nexloop_consumer_identity_erasure();

-- L6 review 2.3: the erasure resumes when the last Consumer hold is released.
create function control.nexloop_erasure_resume_after_hold() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);
begin
 if new.scope_kind<>'consumer' or old.released_at is not null or new.released_at is null then return null;end if;
 perform set_config('eios.tenant_id',new.tenant_id,true);
 if not runtime.nexloop_retention_held(new.tenant_id,new.world,'consumer',new.scope_ref) then
  update control.nexloop_consumer_erasures set state='purging',phase='messages',completed_at=null,updated_at=clock_timestamp()
   where tenant_id=new.tenant_id and world=new.world and consumer_id=new.scope_ref and state='completed_with_holds';
 end if;
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return null;
end $$;
alter function control.nexloop_erasure_resume_after_hold() owner to nexloop_owner;
revoke all on function control.nexloop_erasure_resume_after_hold() from public;
create trigger nx029_erasure_resume_after_hold after update of released_at on control.nexloop_retention_holds for each row execute function control.nexloop_erasure_resume_after_hold();
