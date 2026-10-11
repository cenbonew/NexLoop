-- NX-031 slice 3 (temporary number 0150): a claim-match item whose Conversation has no Claim left to match completes.
-- Extraction marks claim-match (0092) and, for commitment Claims, commitment-register in the same transaction. When the
-- commitment keeper resolves those Claims first, the matcher's purpose READ on the Conversation (0102: only while some
-- Claim is still to be matched) ends, so processing the item was denied on every attempt and it dead-lettered. The
-- matcher now asks first, for the one item it currently leases, whether anything is left to match: a boolean only (no
-- Claim, Message or Conversation content), under the same purpose rule and purpose Action grant the derived READ needs.
create function authz.nexloop_claim_match_settled(p_digest text,p_world text,p_item_key text,p_fence bigint) returns boolean
language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare ident jsonb;tenant text;principal text;rule jsonb;held jsonb;v_conversation text;
begin
 if session_user<>'nexloop_domain_worker' or p_world is distinct from 'real' or p_item_key is null or p_fence is null then
  raise exception 'claim match state unavailable' using errcode='42501';end if;
 ident:=authz.nexloop_service_identity_snapshot(p_digest,p_world);
 if ident->'binding'->>'subject_kind' is distinct from 'service' or (ident->'run_context' is not null and ident->'run_context'<>'null'::jsonb) then
  raise exception 'claim match state unavailable' using errcode='42501';end if;
 tenant:=ident->'binding'->>'tenant_id';principal:=ident->'binding'->>'subject_principal_id';
 perform set_config('eios.tenant_id',tenant,true);
 -- The same authority the purpose READ derives from: an active claim_matching rule and a current grant on its Action.
 select payload into rule from authz.nexloop_authority_facts where tenant_id=tenant and fact_kind='message_purpose_rule'
  and entity_key=array[principal,'claim_matching'];
 if not found or rule->'active' is distinct from 'true'::jsonb or (rule->>'valid_until')::timestamptz<=clock_timestamp() then
  raise exception 'claim match state unavailable' using errcode='42501';end if;
 select payload into held from authz.nexloop_authority_facts where tenant_id=tenant and fact_kind='grants'
  and entity_key=array[principal,authz.nexloop_message_purpose_action('claim_matching')];
 if not found or jsonb_typeof(held->'grants') is distinct from 'array' or jsonb_array_length(held->'grants')=0 then
  raise exception 'claim match state unavailable' using errcode='42501';end if;
 -- Only the item the caller currently leases (its fence): no probing of other Conversations.
 select f.payload->>'conversation_id' into v_conversation from runtime.nexloop_work_feed f where f.tenant_id=tenant and f.world=p_world
  and f.feed='claim-match' and f.item_key=p_item_key and f.status='pending' and f.lease_seq=p_fence and f.lease_until>clock_timestamp();
 if not found or v_conversation is null then raise exception 'claim match state unavailable' using errcode='42501';end if;
 return not exists(select 1 from ontology.nexloop_claims c where c.tenant_id=tenant and c.world=p_world and c.conversation_id=v_conversation
  and authz.nexloop_claim_match_pending(c.tenant_id,c.world,c.claim_id,c.resolution_state));
end $$;
alter function authz.nexloop_claim_match_settled(text,text,text,bigint) owner to nexloop_owner;
revoke all on function authz.nexloop_claim_match_settled(text,text,text,bigint) from public;
grant execute on function authz.nexloop_claim_match_settled(text,text,text,bigint) to nexloop_domain_worker;
