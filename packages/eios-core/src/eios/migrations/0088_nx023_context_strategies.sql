-- NX-023-A: versioned Context strategies (docs/05 §9). Content is published only through
-- the governed human Action nexloop.context.strategy.publish:1; built-in strategies are
-- deployment configuration (deploy/configuration/context-strategies.v1.json), never seeded here.
create table control.nexloop_context_strategies (
 tenant_id text not null,world text not null,strategy_id text not null check(strategy_id~'^[a-z][a-z0-9_]{0,63}$'),
 version integer not null check(version between 1 and 1000000),definition jsonb not null check(jsonb_typeof(definition)='object'),
 definition_digest text not null check(definition_digest~'^[0-9a-f]{64}$'),published_by text not null,intent_id text not null,
 published_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,strategy_id,version),unique(tenant_id,world,intent_id)
);
alter table control.nexloop_context_strategies owner to nexloop_owner;
alter table control.nexloop_context_strategies enable row level security;
alter table control.nexloop_context_strategies force row level security;
create policy context_strategy_tenant on control.nexloop_context_strategies to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_context_strategies from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
create function control.nexloop_context_append_only() returns trigger language plpgsql set search_path=pg_catalog as $$
begin raise exception 'context records are append-only' using errcode='22023';end $$;
alter function control.nexloop_context_append_only() owner to nexloop_owner;
revoke all on function control.nexloop_context_append_only() from public;
create trigger nexloop_context_strategy_append_only before update or delete on control.nexloop_context_strategies
 for each row execute function control.nexloop_context_append_only();

-- Structural shape check shared with the Python validator; the semantic validator is Python's.
create function control.nexloop_context_strategy_shape(d jsonb) returns boolean language sql immutable set search_path=pg_catalog as $$
 select jsonb_typeof(d)='object'
  and (select array_agg(k order by k) from jsonb_object_keys(d) k)=array['framing_reserve','include_hypotheses','include_rejected_definitions',
    'input_token_budget','output_reserve','protocol','sections','semantic','strategy_id','trim_order','version']
  and d->>'protocol'='nexloop.context-pack.v6' and jsonb_typeof(d->'input_token_budget')='number' and jsonb_typeof(d->'output_reserve')='number'
  and jsonb_typeof(d->'framing_reserve')='number' and jsonb_typeof(d->'sections')='object' and jsonb_typeof(d->'trim_order')='array'
  and jsonb_typeof(d->'semantic')='object' and jsonb_typeof(d->'include_hypotheses')='boolean' and jsonb_typeof(d->'include_rejected_definitions')='boolean'
  and (d->>'input_token_budget')::numeric between 1024 and 1000000 and (d->>'output_reserve')::numeric between 128 and 200000
  and (d->>'framing_reserve')::numeric between 0 and (d->>'input_token_budget')::numeric
$$;
alter function control.nexloop_context_strategy_shape(jsonb) owner to nexloop_owner;
revoke all on function control.nexloop_context_strategy_shape(jsonb) from public;

create function authz.nexloop_context_strategy_publish(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;body jsonb:=p_payload::jsonb;k bytea;stored runtime.nexloop_action_claims%rowtype;
 permit jsonb:=a->'permit';v_claim jsonb:=permit->'claim';ref jsonb:=v_claim->'binding'->'action_reference';d jsonb;ident jsonb;
 v_tenant text:=a->>'tenant_id';v_principal text:=a->>'principal_id';v_def jsonb:=body->'definition';v_current integer;v_id text;v_outcome jsonb;
begin
 if octet_length(p_text)>1048576 or octet_length(p_payload)>131072 then raise exception 'context strategy command too large' using errcode='22023';end if;
 if a->>'expires_at' is null or permit->>'expires_at' is null or v_claim->>'lease_expires_at' is null then
  raise exception 'context strategy expiry required' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-context-strategy-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'protocol' is distinct from 'nexloop-context-strategy-v1'
  or a->>'resource_id' is distinct from 'eios:action:'||(ref->>'stable_name')||':'||(ref->>'version')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds'
  or v_claim->'binding'->>'request_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'context strategy permit rejected' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 select definition into d from control.nexloop_action_definitions where tenant_id=v_tenant and world=p_world and resource_id=a->>'resource_id' and active;
 if d is null or d is distinct from a->'definition' or d->'governance'->>'approval_mode'<>'none'
  or jsonb_array_length(d->'governance'->'policy_refs')<>0 or d->'capability_binding'->>'capability_name' is distinct from 'context.strategy.publish' then
  raise exception 'context strategy Action contract unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'binding'->>'tenant_id' is distinct from v_tenant or ident->'binding'->>'subject_principal_id' is distinct from v_principal then
  raise exception 'context strategy identity binding mismatch' using errcode='42501';end if;
 -- Only a human owner publishes how Context is assembled; services and Agents never do.
 if ident->'binding'->>'subject_kind' is distinct from 'human' then raise exception 'human context strategy authority required' using errcode='42501';end if;
 if (select array_agg(x order by x) from jsonb_object_keys(body) x)<>array['definition','request_id','strategy_id','version']
  or body->>'strategy_id' is distinct from v_def->>'strategy_id' or body->'version' is distinct from v_def->'version'
  or control.nexloop_context_strategy_shape(v_def) is not true
  or (v_def->>'include_hypotheses')::boolean and p_world='real' then
  raise exception 'context strategy definition rejected' using errcode='22023';end if;
 select * into stored from runtime.nexloop_action_claims where tenant_id=v_tenant and world=p_world
  and action_name=v_claim->'key'->>'action_stable_name' and intent_id=v_claim->'key'->>'idempotency_key' for update;
 if not found or stored.principal_id is distinct from v_principal or stored.claim is distinct from v_claim
  or v_claim->>'state'<>'active' or (v_claim->>'lease_expires_at')::timestamptz<=clock_timestamp()
  or (permit->>'expires_at')::timestamptz<=clock_timestamp() or body->>'request_id' is distinct from stored.intent_id then
  raise exception 'context strategy claim fenced' using errcode='40001';end if;
 perform set_config('eios.tenant_id',v_tenant,true);
 perform pg_advisory_xact_lock(hashtextextended(v_tenant||':'||p_world||':context-strategy:'||(body->>'strategy_id'),23));
 select max(version) into v_current from control.nexloop_context_strategies where tenant_id=v_tenant and world=p_world and strategy_id=body->>'strategy_id';
 -- Versions are dense and immutable: a new definition is always the next version.
 if (body->>'version')::integer is distinct from coalesce(v_current,0)+1 then raise exception 'context strategy version conflict' using errcode='40001';end if;
 insert into control.nexloop_context_strategies(tenant_id,world,strategy_id,version,definition,definition_digest,published_by,intent_id)
  values(v_tenant,p_world,body->>'strategy_id',(body->>'version')::integer,v_def,encode(sha256(convert_to(v_def::text,'UTF8')),'hex'),v_principal,stored.intent_id);
 v_id:=encode(sha256(convert_to(jsonb_build_array(v_tenant,p_world,'context_strategy',stored.intent_id)::text,'UTF8')),'hex');
 v_outcome:=jsonb_build_object('outcome_id',v_id,'outcome_revision',1,'status','succeeded','outcome_digest',v_claim->'binding'->>'request_digest','finalized_at',clock_timestamp());
 update runtime.nexloop_action_claims set claim=stored.claim||jsonb_build_object('state','terminal','terminal_outcome',v_outcome)
  where tenant_id=stored.tenant_id and world=p_world and action_name=stored.action_name and intent_id=stored.intent_id;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 if (a->>'expires_at')::timestamptz<=clock_timestamp() or (permit->>'expires_at')::timestamptz<=clock_timestamp()
  or (v_claim->>'lease_expires_at')::timestamptz<=clock_timestamp() then raise exception 'context strategy authority expired at commit' using errcode='42501';end if;
 return jsonb_build_object('outcome_id',v_id,'strategy_ref','context-strategy:'||(body->>'strategy_id')||'@'||(body->>'version'),
  'definition_digest',encode(sha256(convert_to(v_def::text,'UTF8')),'hex'));
end $$;
alter function authz.nexloop_context_strategy_publish(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_context_strategy_publish(text,text,text,text,text) from public,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_context_strategy_publish(text,text,text,text,text) to nexloop_api;
