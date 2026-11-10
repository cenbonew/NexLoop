-- The owner of an EffectControl reads its own control and ledger (stage 2026-11-10: customer-5's reply control d841e1f6 had no budget
-- left and the only governed way to raise it - EffectControl.edit:1, then nexloop.effect.configure_control:1 - had no read of
-- reserved_units to check against before the edit). Temporary number; the dispatcher renumbers.
--
-- control.nexloop_effect_control_owner_read(digest, world, control_id): the caller's service credential (the identity snapshot, no
-- Run context) must be the control's owner_principal and, once configured, the ledger's owner_principal. Returns the object's
-- revision, budget_units and valid_until, and the ledger's control_revision, budget_units, reserved_units and valid_until (null
-- before the first configure). Read only; numbers and ids only; never another tenant's or another owner's control (one refusal for
-- every case, 42501 'control owner unavailable'). Used by nexloop_eios.effect_control_budget before and after the edit.

create function control.nexloop_effect_control_owner_read(p_digest text,p_world text,p_control text) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v jsonb;v_tenant text;v_principal text;ctl ontology.objects;l control.nexloop_effect_control_ledger;r jsonb;
begin
 if coalesce(p_control,'')!~'^[a-f0-9]{64}$' then raise exception 'control owner unavailable' using errcode='42501';end if;
 v:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if v is null or v->'run_context' is not null and v->'run_context'<>'null'::jsonb then raise exception 'control owner unavailable' using errcode='42501';end if;
 v_tenant:=v->'binding'->>'tenant_id';v_principal:=v->'binding'->>'subject_principal_id';
 if v_tenant is null or v_principal is null then raise exception 'control owner unavailable' using errcode='42501';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 select * into ctl from ontology.objects where tenant_id=v_tenant and world=p_world and type_name='EffectControl' and object_id=p_control;
 if not found or ctl.properties->>'owner_principal' is distinct from v_principal then
  perform set_config('eios.tenant_id',coalesce(prior,''),true);raise exception 'control owner unavailable' using errcode='42501';end if;
 select * into l from control.nexloop_effect_control_ledger where tenant_id=v_tenant and world=p_world and control_id=p_control;
 if found and l.owner_principal is distinct from v_principal then
  perform set_config('eios.tenant_id',coalesce(prior,''),true);raise exception 'control owner unavailable' using errcode='42501';end if;
 r:=jsonb_build_object('control_id',ctl.object_id,'consumer_id',ctl.properties->>'consumer_id','object_revision',ctl.nexloop_revision,
  'budget_units',ctl.properties->'budget_units','valid_until',ctl.properties->>'valid_until',
  'ledger',case when l.control_id is null then null else jsonb_build_object('control_revision',l.control_revision,'budget_units',l.budget_units,
   'reserved_units',l.reserved_units,'valid_until',l.valid_until) end);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return r;
end $$;
alter function control.nexloop_effect_control_owner_read(text,text,text) owner to nexloop_owner;
revoke all on function control.nexloop_effect_control_owner_read(text,text,text) from public;
grant execute on function control.nexloop_effect_control_owner_read(text,text,text) to nexloop_api;
