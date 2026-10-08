-- DRAFT append: preserve published 0063 checksum; pure deadline set checks
-- occur after the last potentially blocking authority/Artifact/effect helper.
create function authz.nexloop_role_ttl_proof(p jsonb) returns timestamptz
language plpgsql security definer set search_path=pg_catalog as $$
declare q jsonb;deadline timestamptz:=(p->>'expires_at')::timestamptz;
begin
 if p->>'expires_at' is null or (p->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'role proof deadline expired' using errcode='42501';end if;
 if p ? 'property_authorities' then
  if jsonb_typeof(p->'property_authorities') is distinct from 'array' then raise exception 'role property proof unavailable' using errcode='42501';end if;
  for q in select value from jsonb_array_elements(p->'property_authorities') loop
   if q->>'expires_at' is null or (q->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'role property proof deadline expired' using errcode='42501';end if;
   deadline:=least(deadline,(q->>'expires_at')::timestamptz);
  end loop;
 end if;
 if deadline<=clock_timestamp() then raise exception 'role proof deadline expired' using errcode='42501';end if;return deadline;
end $$;
create function authz.nexloop_role_ttl_reads(p jsonb) returns timestamptz
language plpgsql security definer set search_path=pg_catalog as $$
declare e jsonb;deadline timestamptz;
begin
 if jsonb_typeof(p) is distinct from 'object' then raise exception 'role READ set unavailable' using errcode='42501';end if;
 for e in select value from jsonb_each(p) loop deadline:=least(deadline,authz.nexloop_role_ttl_proof((e->>'text')::jsonb));end loop;
 if deadline is null or deadline<=clock_timestamp() then raise exception 'role READ set deadline expired' using errcode='42501';end if;return deadline;
end $$;
create function authz.nexloop_role_business_deadline(p_run uuid) returns timestamptz
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare deadline timestamptz;value timestamptz;
begin
 -- Only typed refs validated and locked by the preceding governed helpers.
 -- Plain MVCC SELECT adds no new lock wait and grants no READ/EXEC permission.
 select expires_at into deadline from authz.nexloop_role_run_bindings where run_id=p_run;
 if not found or deadline is null then raise exception 'role lifecycle unavailable' using errcode='42501';end if;
 for value in select x.expiry from (
  select ar.retention_until expiry from runtime.nexloop_role_context_artifacts b join runtime.nexloop_local_artifacts ar on ar.tenant_id=b.tenant_id and ar.world=b.world and ar.artifact_id=b.artifact_id where b.run_id=p_run
  union all select ctx.valid_until from control.nexloop_effect_run_contexts rc join control.nexloop_effect_contexts ctx on ctx.context_id=rc.context_id where rc.run_id=p_run
  union all select ctl.valid_until from control.nexloop_effect_run_contexts rc join control.nexloop_effect_plan_bindings pb on pb.context_id=rc.context_id join control.nexloop_effect_control_ledger ctl on ctl.control_id=pb.control_id where rc.run_id=p_run
  union all select (g.properties->>'valid_until')::timestamptz from control.nexloop_effect_run_contexts rc join control.nexloop_effect_plan_bindings pb on pb.context_id=rc.context_id join ontology.objects g on g.object_id=pb.goal_id and g.tenant_id=pb.tenant_id and g.world=pb.world where rc.run_id=p_run
 ) x loop
  if value is null then raise exception 'role business deadline unavailable' using errcode='42501';end if;deadline:=least(deadline,value);
 end loop;
 if deadline<=clock_timestamp() then raise exception 'role business deadline expired' using errcode='42501';end if;return deadline;
end $$;
alter function authz.nexloop_role_business_deadline(uuid) owner to nexloop_owner;
revoke all on function authz.nexloop_role_business_deadline(uuid) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_role_ttl_claims(c jsonb) returns timestamptz
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare e jsonb;n text;p jsonb;deadline timestamptz;minimum timestamptz;
begin
 if c ? 'resource_id' then minimum:=authz.nexloop_role_ttl_proof(c);end if;
 for n in select unnest(array['run_proofs','artifact_proofs']) loop
  if c ? n then for e in select value from jsonb_array_elements(c->n) loop minimum:=least(minimum,authz.nexloop_role_ttl_proof(e));end loop;end if;
 end loop;
 if c ? 'formal_reads' then minimum:=least(minimum,authz.nexloop_role_ttl_reads(c->'formal_reads'));end if;
 for n in select unnest(array['role_envelope','context_role_envelope','catalog_envelope','context_catalog_envelope']) loop
  if c->n is not null and c->n is distinct from 'null'::jsonb then
   p:=(c->n->>'payload')::jsonb;minimum:=least(minimum,authz.nexloop_role_ttl_reads(p->'reads'));
   if n in ('role_envelope','context_role_envelope') then
    deadline:=authz.nexloop_role_business_deadline((p->>'run_id')::uuid);
    if deadline is null or deadline<=clock_timestamp() then raise exception 'role lifecycle deadline expired' using errcode='42501';end if;minimum:=least(minimum,deadline);
   elsif n in ('catalog_envelope','context_catalog_envelope') then
    select (o.properties->>'valid_until')::timestamptz into deadline from ontology.objects o where o.object_id=p->>'offering_id' and o.world=c->>'world' and o.type_name='ServiceOffering';
    if deadline is null or deadline<=clock_timestamp() then raise exception 'role catalog deadline expired' using errcode='42501';end if;minimum:=least(minimum,deadline);
   end if;
  end if;
 end loop;
 for n in select unnest(array['context_artifact_proof','origin_proof']) loop
  if c ? n and c->n is distinct from 'null'::jsonb then minimum:=least(minimum,authz.nexloop_role_ttl_proof(c->n));end if;
 end loop;
 if minimum is not null and minimum<=clock_timestamp() then raise exception 'role full proof set deadline expired' using errcode='42501';end if;return minimum;
end $$;
alter function authz.nexloop_role_ttl_proof(jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_role_ttl_proof(jsonb) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
alter function authz.nexloop_role_ttl_reads(jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_role_ttl_reads(jsonb) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
alter function authz.nexloop_role_ttl_claims(jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_role_ttl_claims(jsonb) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;

alter function authz.nexloop_role_run_validate(text,text,text,text,text) rename to nexloop_role_run_validate_before_role_ttl_v0063;
revoke all on function authz.nexloop_role_run_validate_before_role_ttl_v0063(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_role_run_validate(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare result jsonb;deadline timestamptz;
begin
 result:=authz.nexloop_role_run_validate_before_role_ttl_v0063(p_digest,p_world,p_text,p_signature,p_payload);
 perform authz.nexloop_role_ttl_reads(p_payload::jsonb->'reads');
 deadline:=coalesce((result->>'expires_at')::timestamptz,(result->'binding'->>'expires_at')::timestamptz);
 if deadline is null or deadline<=clock_timestamp() then raise exception 'role lifecycle deadline expired' using errcode='42501';end if;
 return result;
end $$;
alter function authz.nexloop_role_run_validate(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_role_run_validate(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;

alter function authz.nexloop_role_run_current(text,text,text,text,text) rename to nexloop_role_run_current_before_role_ttl_v0063;
revoke all on function authz.nexloop_role_run_current_before_role_ttl_v0063(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_role_run_current(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare result jsonb;deadline timestamptz;
begin
 result:=authz.nexloop_role_run_current_before_role_ttl_v0063(p_digest,p_world,p_text,p_signature,p_payload);
 perform authz.nexloop_role_ttl_reads(p_payload::jsonb->'reads');
 deadline:=coalesce((result->>'expires_at')::timestamptz,(result->'binding'->>'expires_at')::timestamptz);
 if deadline is null or deadline<=clock_timestamp() then raise exception 'role lifecycle deadline expired' using errcode='42501';end if;
 return result;
end $$;
alter function authz.nexloop_role_run_current(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_role_run_current(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;

alter function authz.nexloop_role_run_bind(text,text,text,text,text) rename to nexloop_role_run_bind_before_role_ttl_v0063;
revoke all on function authz.nexloop_role_run_bind_before_role_ttl_v0063(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_role_run_bind(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare result jsonb;deadline timestamptz;
begin
 result:=authz.nexloop_role_run_bind_before_role_ttl_v0063(p_digest,p_world,p_text,p_signature,p_payload);
 perform authz.nexloop_role_ttl_reads(p_payload::jsonb->'reads');
 deadline:=coalesce((result->>'expires_at')::timestamptz,(result->'binding'->>'expires_at')::timestamptz);
 if deadline is null or deadline<=clock_timestamp() then raise exception 'role lifecycle deadline expired' using errcode='42501';end if;
 return result;
end $$;
alter function authz.nexloop_role_run_bind(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_role_run_bind(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
grant execute on function authz.nexloop_role_run_bind(text,text,text,text,text) to nexloop_api;

alter function authz.nexloop_runtime_activation_command(text,text,text,text,text) rename to nexloop_runtime_activation_command_before_role_ttl_v0063;
revoke all on function authz.nexloop_runtime_activation_command_before_role_ttl_v0063(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_runtime_activation_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare result jsonb;deadline timestamptz;
begin
 result:=authz.nexloop_runtime_activation_command_before_role_ttl_v0063(p_digest,p_world,p_text,p_signature,p_payload);
 perform authz.nexloop_role_ttl_claims(p_text::jsonb || (case when p_payload::jsonb ? 'run_proofs' then jsonb_build_object('run_proofs',p_payload::jsonb->'run_proofs') else '{}'::jsonb end) || (case when p_payload::jsonb ? 'origin_proof' then jsonb_build_object('origin_proof',p_payload::jsonb->'origin_proof') else '{}'::jsonb end));
 return result;
end $$;
alter function authz.nexloop_runtime_activation_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_runtime_activation_command(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
grant execute on function authz.nexloop_runtime_activation_command(text,text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_scheduler;

alter function authz.nexloop_effect_intent_command(text,text,text,text,text) rename to nexloop_effect_intent_command_before_role_ttl_v0063;
revoke all on function authz.nexloop_effect_intent_command_before_role_ttl_v0063(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_effect_intent_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare result jsonb;deadline timestamptz;
begin
 result:=authz.nexloop_effect_intent_command_before_role_ttl_v0063(p_digest,p_world,p_text,p_signature,p_payload);
 perform authz.nexloop_role_ttl_claims(p_text::jsonb || (case when p_payload::jsonb ? 'run_proofs' then jsonb_build_object('run_proofs',p_payload::jsonb->'run_proofs') else '{}'::jsonb end) || (case when p_payload::jsonb ? 'origin_proof' then jsonb_build_object('origin_proof',p_payload::jsonb->'origin_proof') else '{}'::jsonb end));
 return result;
end $$;
alter function authz.nexloop_effect_intent_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_intent_command(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
grant execute on function authz.nexloop_effect_intent_command(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;

alter function authz.nexloop_effect_execution_command(text,text,text,text,text) rename to nexloop_effect_execution_command_before_role_ttl_v0063;
revoke all on function authz.nexloop_effect_execution_command_before_role_ttl_v0063(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_effect_execution_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare result jsonb;deadline timestamptz;
begin
 result:=authz.nexloop_effect_execution_command_before_role_ttl_v0063(p_digest,p_world,p_text,p_signature,p_payload);
 perform authz.nexloop_role_ttl_claims(p_text::jsonb || (case when p_payload::jsonb ? 'run_proofs' then jsonb_build_object('run_proofs',p_payload::jsonb->'run_proofs') else '{}'::jsonb end) || (case when p_payload::jsonb ? 'origin_proof' then jsonb_build_object('origin_proof',p_payload::jsonb->'origin_proof') else '{}'::jsonb end));
 return result;
end $$;
alter function authz.nexloop_effect_execution_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_effect_execution_command(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
grant execute on function authz.nexloop_effect_execution_command(text,text,text,text,text) to nexloop_action_worker;

alter function authz.nexloop_role_context_command(text,text,text,text,text) rename to nexloop_role_context_command_before_role_ttl_v0063;
revoke all on function authz.nexloop_role_context_command_before_role_ttl_v0063(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_role_context_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare result jsonb;deadline timestamptz;
begin
 result:=authz.nexloop_role_context_command_before_role_ttl_v0063(p_digest,p_world,p_text,p_signature,p_payload);
 perform authz.nexloop_role_ttl_claims(p_text::jsonb || (case when p_payload::jsonb ? 'run_proofs' then jsonb_build_object('run_proofs',p_payload::jsonb->'run_proofs') else '{}'::jsonb end) || (case when p_payload::jsonb ? 'origin_proof' then jsonb_build_object('origin_proof',p_payload::jsonb->'origin_proof') else '{}'::jsonb end));
 if p_payload::jsonb->>'verb' in('snapshot','bind') then
  if result->'current_constraints'->>'valid_until' is null or (result->'current_constraints'->>'valid_until')::timestamptz<=clock_timestamp()
   or result->'supply'->'properties'->>'valid_until' is null or (result->'supply'->'properties'->>'valid_until')::timestamptz<=clock_timestamp() then raise exception 'role Context lifecycle deadline expired' using errcode='42501';end if;
 end if;
 return result;
end $$;
alter function authz.nexloop_role_context_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_role_context_command(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
grant execute on function authz.nexloop_role_context_command(text,text,text,text,text) to nexloop_api;

alter function authz.nexloop_read_local_artifact(text,text,text,text) rename to nexloop_read_local_artifact_before_role_ttl_v0063;
revoke all on function authz.nexloop_read_local_artifact_before_role_ttl_v0063(text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_read_local_artifact(p_digest text,p_world text,p_permit text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare result jsonb;d jsonb:=p_payload::jsonb->'context_dependency';claims jsonb;
begin
 result:=authz.nexloop_read_local_artifact_before_role_ttl_v0063(p_digest,p_world,p_permit,p_payload);
 if d->>'kind'='role' then
  perform authz.nexloop_role_ttl_reads(d->'reads');
  select ap.claims into claims from authz.nexloop_artifact_permits ap where ap.tenant_id=result->>'tenant_id' and ap.permit_id=p_permit;
  perform authz.nexloop_role_ttl_proof(claims);
  if result->>'retention_until' is null or (result->>'retention_until')::timestamptz<=clock_timestamp() then raise exception 'role Artifact retention deadline expired' using errcode='42501';end if;
 end if;
 return result;
end $$;
alter function authz.nexloop_read_local_artifact(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_read_local_artifact(text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
grant execute on function authz.nexloop_read_local_artifact(text,text,text,text) to nexloop_api,nexloop_domain_worker;
