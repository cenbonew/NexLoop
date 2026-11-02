-- Claim → Consumer property, phase one (plan docs/implementation/claim-to-consumer-property-plan.md §1.3 a and §9.3, L6 condition 3;
-- temporary number, the dispatcher numbers it at batch time): an operator release of Claims for rematching by the reason of their
-- latest match, so a configuration change (edit_actions) reaches the Claims the matcher already left in needs_resolution. Today the
-- reason is type_not_writable (78 Claims on stage, 11-02).
-- authz.nexloop_claim_rematch(digest, world, text, signature, payload): the signed nexloop.claim.match EXECUTE of the 0070 authority
-- (granted to the claim_matcher service only; a browser token or another credential is refused there). Tenant from the credential,
-- world the credential's own; the payload is exactly {verb dry_run|release, reason type_not_writable, matcher_version nx020-matcher/N}
-- and names no tenant or world. World real only.
-- Eligible: resolution_state needs_resolution and the latest match of the Claim (under that matcher version or one of its rematch
-- generations) has that reason; never a Claim of an erasing or erased Consumer (runtime.nexloop_consumer_erasing), a Consumer
-- whose persona is not real (simulated_agent or synthetic), a superseded Claim or one a correction points to.
-- release: each eligible Claim goes back to unresolved, its rematch generation +1 with cause 'rematch:<reason>' (the matcher then
-- matches it under nx020-matcher/N;rematch:<generation>, never replaying the old outcome), and its Conversation is touched on the
-- claim-match feed. dry_run writes nothing. The answer is counts only: {eligible, released, conversations} - no Claim id or text.
-- Reads of ontology.nexloop_claims: resolution_state, superseded_at, corrects_claim_id, consumer_id and conversation_id only.
create function authz.nexloop_claim_rematch(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=authz.nexloop_assert_claim_match_authority(p_digest,p_world,p_text,p_signature,p_payload);c jsonb:=p_payload::jsonb;
 t text:=a->>'tenant_id';v_version text;v_reason text;eligible text[];n_conv integer;conv text;
begin
 if jsonb_typeof(c) is distinct from 'object' or exists(select 1 from jsonb_object_keys(c) k where k not in ('verb','reason','matcher_version'))
  or c->>'verb' is null or c->>'verb' not in ('dry_run','release') or c->>'reason' is null or c->>'reason' not in ('type_not_writable')
  or coalesce(c->>'matcher_version','')!~'^nx020-matcher/[1-9][0-9]{0,3}$' then
  raise exception 'claim rematch invalid' using errcode='22023';end if;
 if p_world is distinct from 'real' or a->>'world' is distinct from p_world then raise exception 'claim rematch unavailable' using errcode='42501';end if;
 v_version:=c->>'matcher_version';v_reason:=c->>'reason';
 select coalesce(array_agg(cl.claim_id order by cl.claim_id),'{}') into eligible
  from ontology.nexloop_claims cl
  join lateral (select m.reason from ontology.nexloop_claim_matches m where m.tenant_id=cl.tenant_id and m.world=cl.world and m.claim_id=cl.claim_id
    and (m.matcher_version=v_version or m.matcher_version like v_version||';rematch:%') order by m.recorded_at desc,m.matcher_version desc limit 1) latest on true
  where cl.tenant_id=t and cl.world=p_world and cl.resolution_state='needs_resolution' and latest.reason=v_reason
   and cl.superseded_at is null
   and not exists(select 1 from ontology.nexloop_claims x where x.tenant_id=cl.tenant_id and x.world=cl.world and x.corrects_claim_id=cl.claim_id)
   and not runtime.nexloop_consumer_erasing(t,p_world,cl.consumer_id)
   and runtime.nexloop_consumer_persona(t,p_world,cl.consumer_id)='real';
 select count(distinct cl.conversation_id) into n_conv from ontology.nexloop_claims cl where cl.tenant_id=t and cl.world=p_world and cl.claim_id=any(eligible);
 if c->>'verb'='release' and cardinality(eligible)>0 then
  update ontology.nexloop_claims set resolution_state='unresolved' where tenant_id=t and world=p_world and claim_id=any(eligible);
  insert into ontology.nexloop_claim_rematch as r(tenant_id,world,claim_id,generation,cause,authority_fingerprint)
   select t,p_world,e,1,'rematch:'||v_reason,'rematch:'||v_version from unnest(eligible) e
  on conflict(tenant_id,world,claim_id) do update set generation=r.generation+1,cause=excluded.cause,authority_fingerprint=excluded.authority_fingerprint,released_at=clock_timestamp();
  for conv in select distinct cl.conversation_id from ontology.nexloop_claims cl where cl.tenant_id=t and cl.world=p_world and cl.claim_id=any(eligible) loop
   perform authz.nexloop_work_feed_touch(t,p_world,'claim-match',conv,jsonb_build_object('conversation_id',conv));
  end loop;
 end if;
 return jsonb_build_object('eligible',cardinality(eligible),'released',case when c->>'verb'='release' then cardinality(eligible) else 0 end,'conversations',n_conv);
end $$;
alter function authz.nexloop_claim_rematch(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_claim_rematch(text,text,text,text,text) from public;
grant execute on function authz.nexloop_claim_rematch(text,text,text,text,text) to nexloop_domain_worker;
