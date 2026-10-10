-- NX-051 (AT-014): a correction's target must come earlier in the EFFECTIVE order, not only the receipt order.
-- Effective order = a signed channel's sequence when every Message of the extraction window carries one (single
-- namespace, unique, not skewed; dispatcher rulings 3/4: client-stated order never orders anything); else the
-- receipt sequence. The same rule as conversation_extraction.effective_rank. The 0069 recorder is kept unchanged
-- and no longer directly callable; this wrapper repeats its correction check with that order, then calls the
-- 0066 recorder (every evidence check) and links corrections exactly as 0069 did.
alter function authz.nexloop_record_claim_extraction(text,text,text,text,text) rename to nexloop_record_claim_extraction_v0069;
revoke all on function authz.nexloop_record_claim_extraction_v0069(text,text,text,text,text) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity;

create function authz.nexloop_record_claim_extraction(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare c jsonb:=p_payload::jsonb;cl jsonb;v_seen jsonb:='{}'::jsonb;v_result jsonb;v_target jsonb;v_rank jsonb:='{}'::jsonb;
 prior text:=current_setting('eios.tenant_id',true);v_tenant text:=(p_text::jsonb)->>'tenant_id';v_trusted boolean;
begin
 if jsonb_typeof(c->'claims') is distinct from 'array' or jsonb_typeof(c->'messages') is distinct from 'array' then
  raise exception 'claim extraction unavailable' using errcode='42501';end if;
 -- Order evidence only; authority and every window/evidence check stay in the 0066 recorder below (same transaction).
 perform set_config('eios.tenant_id',coalesce(v_tenant,''),true);
 select count(*)>0 and count(*)=count(f.message_id) and bool_and(f.trust='signed' and not f.skewed and f.provider_sequence is not null)
   and count(distinct f.provider_namespace)=1 and count(distinct f.provider_sequence)=count(*)
  into v_trusted
  from jsonb_array_elements(c->'messages') w
  left join runtime.nexloop_message_provider_facts f on f.tenant_id=v_tenant and f.world=p_world and f.message_id=w.value->>'message_id';
 select coalesce(jsonb_object_agg(w.value->>'sequence',case when v_trusted then f.provider_sequence else (w.value->>'sequence')::bigint end),'{}'::jsonb) into v_rank
  from jsonb_array_elements(c->'messages') w
  left join runtime.nexloop_message_provider_facts f on f.tenant_id=v_tenant and f.world=p_world and f.message_id=w.value->>'message_id';
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 -- A correction may only point at an explicit Claim recorded earlier in the same run, from a Message that is the
 -- same or earlier in the effective order.
 for cl in select value from jsonb_array_elements(c->'claims') loop
  if jsonb_typeof(cl->'corrects_claim_id')='string' then
   v_target:=v_seen->(cl->>'corrects_claim_id');
   if cl->>'epistemic_kind' is distinct from 'correction' or v_target is null or v_target->>'kind'='hypothesis'
    or cl->>'source_sequence' is null or v_target->>'sequence' is null
    or not v_rank ? (v_target->>'sequence') or not v_rank ? (cl->>'source_sequence')
    or (v_rank->>(v_target->>'sequence'))::bigint>(v_rank->>(cl->>'source_sequence'))::bigint then
    raise exception 'claim correction target invalid' using errcode='42501';end if;
  elsif cl ? 'corrects_claim_id' and jsonb_typeof(cl->'corrects_claim_id') is distinct from 'null' then
   raise exception 'claim correction target invalid' using errcode='42501';end if;
  v_seen:=v_seen||jsonb_build_object(cl->>'claim_id',jsonb_build_object('kind',cl->>'epistemic_kind','sequence',cl->'source_sequence'));
 end loop;
 v_result:=authz.nexloop_record_claim_extraction_v0066(p_digest,p_world,p_text,p_signature,p_payload);
 if v_result->'replay'='true'::jsonb then return v_result;end if;
 -- Same transaction; RLS tenant context was set by the verified authority above.
 for cl in select value from jsonb_array_elements(c->'claims') where jsonb_typeof(value->'corrects_claim_id')='string' loop
  update ontology.nexloop_claims set corrects_claim_id=cl->>'corrects_claim_id'
   where tenant_id=current_setting('eios.tenant_id',true) and world=p_world and claim_id=cl->>'claim_id' and corrects_claim_id is null;
 end loop;
 return v_result;
end $$;
alter function authz.nexloop_record_claim_extraction(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_record_claim_extraction(text,text,text,text,text) from public,nexloop_action_worker,nexloop_scheduler,nexloop_identity;
grant execute on function authz.nexloop_record_claim_extraction(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;
