-- NX-047 + ADR-020 §1: the governed Message READ derivation (0077) also covers Agent
-- outbound Messages. "Accepted" becomes: an inbound acceptance outbox row (unchanged
-- 0077 path) OR a materialized outbound record of a channel-accepted reply. No new
-- authority source: the same rule fact, Consumer READ, tenant/world and conversation
-- consistency are rebuilt under share locks at every use.
alter function authz.nexloop_assert_derived_message_read(text,text,jsonb) rename to nexloop_assert_derived_message_read_inbound_v0077;
revoke all on function authz.nexloop_assert_derived_message_read_inbound_v0077(text,text,jsonb) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;

create function authz.nexloop_assert_derived_message_read(p_digest text,p_world text,p_claims jsonb) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare ident jsonb;tenant text;principal text;basis jsonb:=p_claims->'derivation_basis';message text;rule jsonb;
 m ontology.objects;cm runtime.nexloop_conversation_messages;conv runtime.nexloop_conversations;o runtime.nexloop_outbound_messages;
 consumer_env jsonb;consumer jsonb;k bytea;
begin
 message:=substring(p_claims->>'resource_id' from '^eios:(?:object|property):Message/([a-f0-9]{64})(?:/(?:actor|body))?$');
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 tenant:=ident->'binding'->>'tenant_id';principal:=ident->'binding'->>'subject_principal_id';
 perform set_config('eios.tenant_id',tenant,true);
 -- Inbound (or anything that is not a materialized outbound reply): unchanged 0077 rules.
 if message is null or not exists(select 1 from runtime.nexloop_outbound_messages x where x.tenant_id=tenant and x.world=p_world and x.message_id=message) then
  return authz.nexloop_assert_derived_message_read_inbound_v0077(p_digest,p_world,p_claims);
 end if;
 perform authz.nexloop_lock_credential(p_digest,p_world,p_claims->>'resource_id');
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if p_claims->>'derivation' is distinct from 'accepted-message-v1'
  or p_claims->>'tenant_id' is distinct from tenant or p_claims->>'principal_id' is distinct from principal
  or p_claims->>'credential_id' is distinct from ident->'binding'->>'credential_id'
  or p_claims->>'world' is distinct from p_world or p_world is distinct from 'real'
  or p_claims->>'directory_hash' is distinct from ident->>'directory_hash'
  or p_claims->>'resource_id' is distinct from p_claims->>'target_resource' or p_claims->>'operation' is distinct from 'read'
  or p_claims->>'expires_at' is null or (p_claims->>'expires_at')::timestamptz<=clock_timestamp()
  or p_claims->'facts' is distinct from '[]'::jsonb
  or ident->'binding'->>'subject_kind' is distinct from 'service' or (ident->'run_context' is not null and ident->'run_context'<>'null'::jsonb)
  or jsonb_typeof(basis) is distinct from 'object' then raise exception 'derived message read invalid' using errcode='42501';end if;
 if exists(select 1 from authz.nexloop_authority_facts where tenant_id=tenant and fact_kind='grants' and entity_key in
   (array[principal,'eios:object:Message/'||message],array[principal,'eios:property:Message/'||message||'/actor'],array[principal,'eios:property:Message/'||message||'/body'])) then
  raise exception 'derived message read superseded' using errcode='42501';end if;
 select payload into rule from authz.nexloop_authority_facts where tenant_id=tenant and fact_kind='message_read_rule' and entity_key=array[principal] for share;
 if not found or encode(sha256(convert_to(rule::text,'UTF8')),'hex') is distinct from basis->>'rule_hash'
  or rule->'active' is distinct from 'true'::jsonb or (rule->>'valid_until')::timestamptz<=clock_timestamp()
  or (p_claims->>'expires_at')::timestamptz>(rule->>'valid_until')::timestamptz then raise exception 'derived message read rule unavailable' using errcode='42501';end if;
 select * into m from ontology.objects where tenant_id=tenant and world=p_world and type_name='Message' and object_id=message for share;
 if not found then raise exception 'derived message unavailable' using errcode='42501';end if;
 -- Outbound acceptance: materialized, channel-accepted, Agent sender, same conversation.
 select * into o from runtime.nexloop_outbound_messages where tenant_id=tenant and world=p_world and message_id=message for share;
 if not found or o.delivery_state not in ('provider_accepted','delivered') or m.properties->>'actor' is distinct from o.sender_principal then
  raise exception 'derived message not accepted' using errcode='42501';end if;
 select * into cm from runtime.nexloop_conversation_messages where tenant_id=tenant and world=p_world and message_id=message for share;
 if not found or cm.conversation_id is distinct from basis->>'conversation_id' or o.conversation_id is distinct from cm.conversation_id
  or cm.sequence is distinct from o.sequence then raise exception 'derived message conversation changed' using errcode='42501';end if;
 select * into conv from runtime.nexloop_conversations where tenant_id=tenant and world=p_world and conversation_id=cm.conversation_id for share;
 if not found or conv.consumer_id is distinct from basis->>'consumer_id' then raise exception 'derived message consumer changed' using errcode='42501';end if;
 consumer_env:=basis->'consumer_read';consumer:=(consumer_env->>'text')::jsonb;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=consumer->>'key_id' and active;
 if not found or consumer->>'protocol' is distinct from 'nexloop-object-read-v1'
  or consumer_env->>'signature' is distinct from encode(extensions.hmac(convert_to('nexloop-object-read-v1:'||(consumer_env->>'text'),'UTF8'),k,'sha256'),'hex')
  or consumer->>'type_name' is distinct from 'Consumer' or consumer->>'object_id' is distinct from conv.consumer_id
  or consumer->>'resource_id' is distinct from 'eios:object:Consumer/'||conv.consumer_id
  or (p_claims->>'expires_at')::timestamptz>(consumer->>'expires_at')::timestamptz then raise exception 'derived message consumer READ required' using errcode='42501';end if;
 perform authz.nexloop_assert_read_authority_before_message_read_v0072(p_digest,p_world,consumer);
 if (p_claims->>'expires_at')::timestamptz<=clock_timestamp() then raise exception 'derived message read expired' using errcode='42501';end if;
 return ident->'binding';
end $$;
alter function authz.nexloop_assert_derived_message_read(text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_derived_message_read(text,text,jsonb) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
