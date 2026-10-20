-- ADR-035 (supplements ADR-025; owner 2026-10-19 option A; temporary number 0224): workbench owners and operators read the
-- operations objects Goal, PlanStep, Commitment and ServiceOffering through the same member derivation, audited.
--   * Object READ for an existing object of the tenant and world; property READ only for each type's fixed field list (system types
--     declare no property groups), never for a field in an owner-restricted group; reviewers get nothing new.
--   * Commitment's customer content (condition, content_ref) and identifiers of who promised it (made_by, fulfillment_evidence) are
--     not derived here: they stay with the commitment view (nexloop.commitment.read plus the source Message READ).
--   * Nothing is derived for an object whose Consumer (consumer_id; made_to for a Commitment) is being erased (NX-029).
--   * Every read leaves one audit row (object kinds goal / plan_step / commitment / service_offering).
-- authz.nexloop_workbench_member_may_read (0123) is renamed and kept; the new wrapper handles the four types and passes every other
-- target on unchanged. authz.nexloop_assert_workbench_member_read is the 0123 body copied with the wider object match and kinds.
-- Nothing per object is written; no role or grant changes.

alter table runtime.nexloop_workbench_read_audit drop constraint nexloop_workbench_read_audit_object_kind_check;
alter table runtime.nexloop_workbench_read_audit add constraint nexloop_workbench_read_audit_object_kind_check
 check(object_kind in ('message','consumer','goal','plan_step','commitment','service_offering'));
alter table runtime.nexloop_workbench_read_audit drop constraint nexloop_workbench_read_audit_target_resource_check;
alter table runtime.nexloop_workbench_read_audit add constraint nexloop_workbench_read_audit_target_resource_check
 check(target_resource~'^eios:object:(Message|Consumer|Goal|PlanStep|Commitment|ServiceOffering)/[a-f0-9]{64}$');

alter function authz.nexloop_workbench_member_may_read(text,text,text,text) rename to nexloop_workbench_member_may_read_before_ops_v0224;
revoke all on function authz.nexloop_workbench_member_may_read_before_ops_v0224(text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator;
create function authz.nexloop_workbench_member_may_read(p_tenant text,p_world text,p_role text,p_target text) returns boolean
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare m text[];v_type text;v_id text;v_field text;o ontology.objects;def jsonb;grp text;restriction jsonb;v_consumer text;
begin
 m:=regexp_match(p_target,'^eios:(object|property):(Goal|PlanStep|Commitment|ServiceOffering)/([a-f0-9]{64})(?:/([A-Za-z][A-Za-z0-9_]{0,63}))?$');
 if m is null then return authz.nexloop_workbench_member_may_read_before_ops_v0224(p_tenant,p_world,p_role,p_target);end if;
 if (m[1]='object')<>(m[4] is null) or p_role is null or p_role not in ('owner','operator') then return false;end if;
 v_type:=m[2];v_id:=m[3];v_field:=m[4];
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into o from ontology.objects where tenant_id=p_tenant and world=p_world and type_name=v_type and object_id=v_id;
 if not found then return false;end if;
 -- NX-029: nothing of an object whose Consumer is being erased.
 v_consumer:=case when v_type='Commitment' then o.properties->>'made_to' else o.properties->>'consumer_id' end;
 if v_consumer ~ '^[a-f0-9]{64}$' and runtime.nexloop_consumer_erasing(p_tenant,p_world,v_consumer) then return false;end if;
 if v_field is null then return true;end if;
 if not ((v_type='Goal' and v_field in ('consumer_id','state','valid_until'))
   or (v_type='PlanStep' and v_field in ('consumer_id','goal_id','control_id','action_name','state'))
   or (v_type='Commitment' and v_field in ('made_to','promised_at','due_at','due_precision','related_goal_ref','supersedes','status','late','fulfillment_basis'))
   or (v_type='ServiceOffering' and v_field in ('service_code','title','delivery_action','content_kind','price_amount','currency','eligibility',
      'allowed_guarantees','allowed_discounts','evidence_kind','active','valid_until'))) then return false;end if;
 -- An owner-restricted group is never derived (types without groups have none to restrict).
 select definition into def from ontology.object_type_versions where tenant_id=p_tenant and type_name=v_type and version=o.schema_version;
 grp:=ontology.nexloop_property_group_of(def,v_field);
 select payload into restriction from authz.nexloop_authority_facts where tenant_id=p_tenant and fact_kind='property_group_restriction' and entity_key=array[v_type];
 return not (grp is not null and coalesce(restriction->'restricted_groups' ? grp,false));
end $$;
alter function authz.nexloop_workbench_member_may_read(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_member_may_read(text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator;

create or replace function authz.nexloop_assert_workbench_member_read(p_digest text,p_world text,p_claims jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare w jsonb;ident jsonb;v_target text:=p_claims->>'target_resource';v_object text;
begin
 w:=authz.nexloop_workbench_member(p_digest,p_world);ident:=w->'identity';
 if p_claims->>'derivation' is distinct from 'workbench-member-v1' or p_claims->>'operation' is distinct from 'read'
  or p_claims->>'resource_id' is distinct from v_target or p_claims->>'world' is distinct from p_world
  or p_claims->>'tenant_id' is distinct from w->>'tenant_id' or p_claims->>'principal_id' is distinct from w->>'principal_id'
  or p_claims->>'credential_id' is distinct from ident->'binding'->>'credential_id'
  or p_claims->>'directory_hash' is distinct from ident->>'directory_hash'
  or coalesce(p_claims->>'read_purpose','')!~'^[a-z][a-z_]{0,63}$'
  or p_claims->>'expires_at' is null or (p_claims->>'expires_at')::timestamptz<=clock_timestamp()
  or (p_claims->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'workbench member read claim invalid' using errcode='42501';end if;
 if not authz.nexloop_workbench_member_may_read(w->>'tenant_id',p_world,w->>'role',v_target) then
  raise exception 'workbench member read denied' using errcode='42501';end if;
 v_object:=substring(v_target from '^eios:(?:object|property):((?:Message|Consumer|Goal|PlanStep|Commitment|ServiceOffering)/[a-f0-9]{64})');
 perform set_config('eios.tenant_id',w->>'tenant_id',true);
 -- One audit row per object and transaction; a failed write fails the read (no exception handler here).
 insert into runtime.nexloop_workbench_read_audit(tenant_id,world,principal_id,role,session_id,object_kind,target_resource,read_purpose,read_txid)
  values(w->>'tenant_id',p_world,w->>'principal_id',w->>'role',ident->'binding'->>'session_id',
   case split_part(v_object,'/',1) when 'Message' then 'message' when 'Consumer' then 'consumer' when 'Goal' then 'goal' when 'PlanStep' then 'plan_step'
    when 'Commitment' then 'commitment' else 'service_offering' end,'eios:object:'||v_object,p_claims->>'read_purpose',txid_current())
  on conflict(tenant_id,read_txid,principal_id,target_resource) do nothing;
 return ident->'binding';
end $$;
