-- NX-029 / AT-026 erasure coverage, batch 3 (dispatcher 2026-10-21; docs/implementation/erasure-coverage.md): the remaining real tables
-- with a consumer_id (tests/test_consumer_erasure_coverage_pg.py KNOWN_GAPS) join the Consumer erasure. The migration number is temporary;
-- it lives only in the file name and the catalog.
--
-- When a Consumer's erasure completes (as batch 2: after its message phase), in that world, under one pseudonym of this step
-- (the conversations and plans are batch 2's, 0305):
--   delete   control.nexloop_consumer_owners                   the principal-to-Consumer link itself (only insert/update are guarded)
--            control.nexloop_relationship_context_recipes      a recipe of assessments the erasure already deleted (0138)
--            runtime.nexloop_evo_patch_sources                 the link of an evolution patch to its source Consumer (the patch stays)
--            runtime.nexloop_message_relay_leases              short relay leases (the erasure waited for its effects)
--            runtime.nexloop_commitments                       the commitment registry rows (its guard admits the erasure delete;
--                                                              the Formal commitment object is the erasure's commitments phase's)
--   update   authz.nexloop_role_run_bindings                   consumer_id
--            control.nexloop_consumer_identity_link_receipts   identity_ref (one pseudonym per row: the key), consumer, subject and
--                                                              account ids, request_payload {"erased":true}
--            control.nexloop_data_exports                      consumer_id, reason '' (the export file has its own expiry)
--            control.nexloop_effect_contexts                   consumer_id, expected_versions {"erased":true} (it names the
--                                                              Consumer resource; identity guard v2 admits the erasure scope first)
--            control.nexloop_effect_control_ledger             consumer_id
--            ontology.nexloop_extraction_runs                  consumer and principal ids, rejected [] (rejected extractions may
--                                                              quote the Consumer)
--            runtime.nexloop_commitment_exceptions             consumer_id, detail {"erased":true}
--            runtime.nexloop_native_web_events                 consumer and principal ids (the transports' foreign key now cascades
--                                                              the principal update)
--            runtime.nexloop_relay_handoffs                    consumer_id
--            runtime.nexloop_scope_denials                     consumer_id, scope and request_scope {"erased":true} (immutable
--                                                              guard v2 admits the erasure scope first)
-- Facts stay (a Run was bound, an export was made, a context, a ledger, a denial, a plan existed; times and codes), never the identity.
-- A Consumer retention hold keeps all of it (completed_with_holds never fires this). Guards: the existing ones admit only the erasure
-- scope (nexloop_nx022_append_only and nexloop_commitment_registry_guard already do; two v2 guards here).

alter table control.nexloop_data_exports drop constraint nexloop_data_exports_consumer_id_check,
 add constraint nexloop_data_exports_consumer_id_check check(consumer_id~'^[a-f0-9]{64}$' or consumer_id~'^pseudonym:[0-9a-f]{32}$'),
 drop constraint nexloop_data_exports_reason_check,
 add constraint nexloop_data_exports_reason_check check(char_length(reason)<=500 and (char_length(reason)>=1 or consumer_id~'^pseudonym:'));
alter table control.nexloop_effect_contexts drop constraint nexloop_effect_contexts_expected_versions_check,
 add constraint nexloop_effect_contexts_expected_versions_check check((jsonb_typeof(expected_versions)='array' and jsonb_array_length(expected_versions)>0)
  or (expected_versions='{"erased":true}'::jsonb and consumer_id~'^pseudonym:[0-9a-f]{32}$'));
alter table runtime.nexloop_native_web_transports drop constraint nexloop_native_web_transports_tenant_id_world_provider_nam_fkey,
 add constraint nexloop_native_web_transports_tenant_id_world_provider_nam_fkey foreign key(tenant_id,world,provider_namespace,principal_id,provider_event_id)
  references runtime.nexloop_native_web_events(tenant_id,world,provider_namespace,principal_id,provider_event_id) on update cascade;

-- Guards v2: the erasure scope first, then exactly the original rule.
create function authz.nexloop_effect_context_identity_immutable_v2() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if runtime.nexloop_erasure_permits(current_user::text,'control.nexloop_effect_contexts','UPDATE',to_jsonb(old),to_jsonb(new)) then return new;end if;
 if (new.context_id,new.tenant_id,new.world,new.consumer_id,new.goal_identity,new.plan_step_identity,new.slot_identity,new.executor_principal)
  is distinct from (old.context_id,old.tenant_id,old.world,old.consumer_id,old.goal_identity,old.plan_step_identity,old.slot_identity,old.executor_principal) then
  raise exception 'effect context identity immutable' using errcode='22023';end if;
 return new;
end $$;
alter function authz.nexloop_effect_context_identity_immutable_v2() owner to nexloop_owner;
revoke all on function authz.nexloop_effect_context_identity_immutable_v2() from public;
drop trigger effect_context_identity_immutable on control.nexloop_effect_contexts;
create trigger effect_context_identity_immutable before update on control.nexloop_effect_contexts for each row execute function authz.nexloop_effect_context_identity_immutable_v2();

create function authz.nexloop_scope_denial_immutable_v2() returns trigger language plpgsql set search_path=pg_catalog,pg_temp as $$
begin
 if tg_op='UPDATE' and runtime.nexloop_erasure_permits(current_user::text,'runtime.nexloop_scope_denials','UPDATE',to_jsonb(old),to_jsonb(new)) then return new;end if;
 raise exception 'scope denial is immutable' using errcode='22000';
end $$;
alter function authz.nexloop_scope_denial_immutable_v2() owner to nexloop_owner;
revoke all on function authz.nexloop_scope_denial_immutable_v2() from public;
drop trigger scope_denial_immutable on runtime.nexloop_scope_denials;
create trigger scope_denial_immutable before delete or update on runtime.nexloop_scope_denials for each row execute function authz.nexloop_scope_denial_immutable_v2();

create function control.nexloop_consumer_reference_erasure() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v_prior text:=current_setting('eios.tenant_id',true);v_p text:='pseudonym:'||encode(extensions.gen_random_bytes(16),'hex');
 v_ref text:='consumer-erasure:'||new.request_id;n jsonb:='{}'::jsonb;c integer;t text;w text:=new.world;k text:=new.consumer_id;tn text:=new.tenant_id;
begin
 if new.state<>'completed' or old.state='completed' then return null;end if;
 perform set_config('eios.tenant_id',tn,true);
 if runtime.nexloop_retention_held(tn,w,'consumer',k) then perform set_config('eios.tenant_id',coalesce(v_prior,''),true);return null;end if;
 -- Deletions.
 delete from control.nexloop_consumer_owners where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('consumer_owners',c);
 delete from control.nexloop_relationship_context_recipes where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('relationship_context_recipes',c);
 delete from runtime.nexloop_evo_patch_sources where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('evo_patch_sources',c);
 delete from runtime.nexloop_message_relay_leases where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('message_relay_leases',c);
 perform runtime.nexloop_erasure_open('runtime.nexloop_commitments',null,true,v_ref);
 delete from runtime.nexloop_commitments where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('commitments',c);
 -- Pseudonyms (no guard on these tables).
 update authz.nexloop_role_run_bindings set consumer_id=v_p where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('role_run_bindings',c);
 update control.nexloop_consumer_identity_link_receipts set identity_ref='pseudonym:'||encode(extensions.gen_random_bytes(16),'hex'),
   consumer_id=v_p,subject_id=v_p,account_id=v_p,request_payload='{"erased":true}'::jsonb where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('identity_link_receipts',c);
 update control.nexloop_data_exports set consumer_id=v_p,reason='' where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('data_exports',c);
 update control.nexloop_effect_control_ledger set consumer_id=v_p where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('effect_control_ledger',c);
 update ontology.nexloop_extraction_runs set consumer_id=v_p,principal_id=v_p,rejected='[]'::jsonb where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('extraction_runs',c);
 update runtime.nexloop_native_web_events set consumer_id=v_p,principal_id=v_p where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('native_web_events',c);
 -- Pseudonyms through the erasure scope (guarded tables).
 perform runtime.nexloop_erasure_open('control.nexloop_effect_contexts',array['consumer_id','expected_versions'],false,v_ref);
 update control.nexloop_effect_contexts set consumer_id=v_p,expected_versions='{"erased":true}'::jsonb where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('effect_contexts',c);
 perform runtime.nexloop_erasure_open('runtime.nexloop_commitment_exceptions',array['consumer_id','detail'],false,v_ref);
 update runtime.nexloop_commitment_exceptions set consumer_id=v_p,detail='{"erased":true}'::jsonb where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('commitment_exceptions',c);
 perform runtime.nexloop_erasure_open('runtime.nexloop_relay_handoffs',array['consumer_id'],false,v_ref);
 update runtime.nexloop_relay_handoffs set consumer_id=v_p where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('relay_handoffs',c);
 perform runtime.nexloop_erasure_open('runtime.nexloop_scope_denials',array['consumer_id','scope','request_scope'],false,v_ref);
 update runtime.nexloop_scope_denials set consumer_id=v_p,scope='{"erased":true}'::jsonb,request_scope='{"erased":true}'::jsonb where tenant_id=tn and world=w and consumer_id=k;
 get diagnostics c=row_count;n:=n||jsonb_build_object('scope_denials',c);
 -- Only the scopes opened here are closed: the erasure step may hold others open in the same transaction.
 delete from control.nexloop_erasure_scope where txid=txid_current() and table_name in ('runtime.nexloop_commitments','control.nexloop_effect_contexts',
  'runtime.nexloop_commitment_exceptions','runtime.nexloop_relay_handoffs','runtime.nexloop_scope_denials');
 perform runtime.nexloop_erasure_tombstone(tn,w,'consumer_erasure',v_ref,'consumer_references','consumer_references',k,'pseudonymized',n,'nexloop_owner');
 perform set_config('eios.tenant_id',coalesce(v_prior,''),true);
 return null;
end $$;
alter function control.nexloop_consumer_reference_erasure() owner to nexloop_owner;
revoke all on function control.nexloop_consumer_reference_erasure() from public;
create trigger zz_nx029_2_consumer_reference_erasure after update of state on control.nexloop_consumer_erasures for each row execute function control.nexloop_consumer_reference_erasure();
