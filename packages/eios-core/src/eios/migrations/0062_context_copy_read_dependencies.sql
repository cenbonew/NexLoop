-- Current reader and producer permissions on copied Message fields.
alter function authz.nexloop_read_local_artifact(text,text,text,text) rename to nexloop_read_local_artifact_v0060;
revoke all on function authz.nexloop_read_local_artifact_v0060(text,text,text,text) from public,nexloop_api,nexloop_domain_worker;
create function authz.nexloop_context_artifact_read_dependency(p_digest text,p_world text,p_permit text,p_payload text)
returns text language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare r jsonb;b runtime.nexloop_context_artifact_bindings;
begin
 r:=authz.nexloop_read_local_artifact_v0060(p_digest,p_world,p_permit,p_payload);
 select * into b from runtime.nexloop_context_artifact_bindings where tenant_id=r->>'tenant_id' and world=p_world and artifact_id=r->>'artifact_id';
 if not found then
  if r->>'media_type'='application/vnd.nexloop.context+json' then raise exception 'context artifact unavailable' using errcode='42501';end if;
  return null;
 end if;
 if (b.pack_text::jsonb)->>'schema_version' is null or (b.pack_text::jsonb)->>'schema_version' not in ('nexloop.context-pack.v1','nexloop.context-pack.v2')
  or (b.pack_text::jsonb)->'user_statement'->>'message_id' is distinct from b.message_id then raise exception 'context artifact unavailable' using errcode='42501';end if;
 return b.message_id;
end $$;
alter function authz.nexloop_context_artifact_read_dependency(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_context_artifact_read_dependency(text,text,text,text) from public;
grant execute on function authz.nexloop_context_artifact_read_dependency(text,text,text,text) to nexloop_api,nexloop_domain_worker;
create function authz.nexloop_read_local_artifact(p_digest text,p_world text,p_permit text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare r jsonb;b runtime.nexloop_context_artifact_bindings;d jsonb:=p_payload::jsonb->'context_dependency';a jsonb;proof jsonb;v jsonb;artifact_proof jsonb;accepted runtime.nexloop_message_outbox;
begin
 r:=authz.nexloop_read_local_artifact_v0060(p_digest,p_world,p_permit,p_payload);
 select * into b from runtime.nexloop_context_artifact_bindings where tenant_id=r->>'tenant_id' and world=p_world and artifact_id=r->>'artifact_id' for share;
 if not found then
  if r->>'media_type'='application/vnd.nexloop.context+json' then raise exception 'context artifact unavailable' using errcode='42501';end if;
  return r;
 end if;
 if d->>'text' is null or d->>'signature' is null then raise exception 'context artifact unavailable' using errcode='42501';end if;
 if (b.pack_text::jsonb)->>'schema_version' is null or (b.pack_text::jsonb)->>'schema_version' not in ('nexloop.context-pack.v1','nexloop.context-pack.v2')
  or (b.pack_text::jsonb)->'user_statement'->>'message_id' is distinct from b.message_id then raise exception 'context artifact unavailable' using errcode='42501';end if;
 a:=(d->>'text')::jsonb;
 if a->>'type_name' is distinct from 'Message' or a->>'object_id' is distinct from b.message_id
  or a->'fields' is distinct from '["actor","body"]'::jsonb or a->>'tenant_id' is distinct from b.tenant_id
  or a->>'operation' is distinct from 'read' or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp()
  or jsonb_typeof(a->'property_authorities') is distinct from 'array' or jsonb_array_length(a->'property_authorities')<>2 then raise exception 'context artifact unavailable' using errcode='42501';end if;
 v:=authz.nexloop_read_object(p_digest,p_world,d->>'text',d->>'signature');
 select * into accepted from runtime.nexloop_message_outbox where tenant_id=b.tenant_id and world=p_world and message_id=b.message_id for share;
 if not found or v->'properties'->>'body' is distinct from (b.pack_text::jsonb)->'user_statement'->>'body'
  or v->'properties'->>'body' is distinct from accepted.record->>'body'
  or v->'properties'->>'actor' is distinct from accepted.record->>'actor' then raise exception 'context artifact unavailable' using errcode='42501';end if;
 -- Current reader is authoritative; never borrow b.source_digest authority.
 perform authz.nexloop_assert_read_authority(p_digest,p_world,a);
 for proof in select value from jsonb_array_elements(a->'property_authorities') loop
  if proof->>'operation' is distinct from 'read' or proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'context artifact unavailable' using errcode='42501';end if;
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
 end loop;
 select claims into artifact_proof from authz.nexloop_artifact_permits where tenant_id=b.tenant_id and permit_id=p_permit;
 if artifact_proof is null or artifact_proof->>'expires_at' is null or (artifact_proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'context artifact unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_artifact_authority(p_digest,p_world,artifact_proof);
 if (a->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'context artifact unavailable' using errcode='42501';end if;
 return r;
end $$;
alter function authz.nexloop_read_local_artifact(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_read_local_artifact(text,text,text,text) from public;
grant execute on function authz.nexloop_read_local_artifact(text,text,text,text) to nexloop_api,nexloop_domain_worker;

-- Producer snapshot must not return copied body before current Source's own READ;
-- bind repeats the same protected proof and tail in its governed transaction.
alter function authz.nexloop_context_artifact_command(text,text,text,text,text) rename to nexloop_context_artifact_command_v0060;
revoke all on function authz.nexloop_context_artifact_command_v0060(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_scheduler,nexloop_action_worker,nexloop_identity;
create function authz.nexloop_check_context_message_read(p_digest text,p_world text,p_envelope jsonb,p_message text)
returns void language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb;proof jsonb;v jsonb;
begin
 if p_envelope->>'text' is null or p_envelope->>'signature' is null or p_message is null then raise exception 'context artifact unavailable' using errcode='42501';end if;
 a:=(p_envelope->>'text')::jsonb;
 if a->>'type_name' is distinct from 'Message' or a->>'object_id' is distinct from p_message or a->'fields' is distinct from '["actor","body"]'::jsonb
  or a->>'operation' is distinct from 'read' or a->>'expires_at' is null or (a->>'expires_at')::timestamptz<=clock_timestamp()
  or jsonb_typeof(a->'property_authorities') is distinct from 'array' or jsonb_array_length(a->'property_authorities')<>2 then raise exception 'context artifact unavailable' using errcode='42501';end if;
 v:=authz.nexloop_read_object(p_digest,p_world,p_envelope->>'text',p_envelope->>'signature');
 perform authz.nexloop_assert_read_authority(p_digest,p_world,a);
 for proof in select value from jsonb_array_elements(a->'property_authorities') loop
  if proof->>'operation' is distinct from 'read' or proof->>'expires_at' is null or (proof->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'context artifact unavailable' using errcode='42501';end if;
  perform authz.nexloop_assert_read_authority(p_digest,p_world,proof);
 end loop;
 if (a->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'context artifact unavailable' using errcode='42501';end if;
end $$;
alter function authz.nexloop_check_context_message_read(text,text,jsonb,text) owner to nexloop_owner;
revoke all on function authz.nexloop_check_context_message_read(text,text,jsonb,text) from public,nexloop_api,nexloop_domain_worker;
create function authz.nexloop_context_artifact_command(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;p jsonb:=p_payload::jsonb;v jsonb;
begin
 perform authz.nexloop_check_context_message_read(p_digest,p_world,a->'message_read_envelope',p->>'message_id');
 v:=authz.nexloop_context_artifact_command_v0060(p_digest,p_world,p_text,p_signature,p_payload);
 perform authz.nexloop_check_context_message_read(p_digest,p_world,a->'message_read_envelope',p->>'message_id');
 return v;
end $$;
alter function authz.nexloop_context_artifact_command(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_context_artifact_command(text,text,text,text,text) from public;
grant execute on function authz.nexloop_context_artifact_command(text,text,text,text,text) to nexloop_api;
