-- NX-046 / NX-028 D2 revision (owner 2026-10-18: the owner may see the knowledge workbench). TEMPORARY NUMBER: the dispatcher renumbers
-- at merge (catalog tail + 1).
-- A read-only Action, nexloop.knowledge.read:1, opens the two reads the workbench knowledge pages use: the pending-review queue
-- (0072 authz.nexloop_read_review_queue) and one pending candidate (0074 authz.nexloop_read_review_candidate). Both now accept the
-- signed claims of either ontology.schema.review:1 (the reviewer, unchanged) or nexloop.knowledge.read:1 (granted to the owner role
-- by workbench-roles; trusted configuration only). Everything else is copied verbatim: same protocol (nexloop-review-queue-v1),
-- signature, expiry, authority assertion (authz.nexloop_assert_action_authority on the claimed Action), tenant scoping and the
-- same rows. The review decisions (NX-044 governed Actions) and the other review reads that use
-- authz.nexloop_assert_review_read (publication basis, reflow status, derivation impact) are not changed: they still require
-- ontology.schema.review, so the owner reads and never decides. No Claim value is read or projected beyond the copied reads.

create or replace function authz.nexloop_read_review_queue(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;k bytea;c jsonb:=p_payload::jsonb;t text:=a->>'tenant_id';lim integer:=coalesce((c->>'limit')::integer,50);
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or p_world is null or octet_length(p_text)>1048576 or octet_length(p_payload)>65536
  or a->>'protocol' is distinct from 'nexloop-review-queue-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is null or a->>'action_resource' not in ('eios:action:ontology.schema.review:1','eios:action:nexloop.knowledge.read:1')
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') or lim not between 1 and 200 then
  raise exception 'review queue unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-review-queue-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'review queue unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform set_config('eios.tenant_id',t,true);
 return coalesce((select jsonb_agg(item order by item->>'created_at',item->>'candidate_id') from (
  select jsonb_build_object('candidate_id',x.candidate_id,'kind',x.kind,'status',x.status,'revision',x.revision,'candidate',x.candidate,
   'merge_scores',x.merge_scores,'config_version',x.config_version,'created_at',x.created_at,'dependent_claim_count',cardinality(x.dependent_claims),
   'evidence',(select coalesce(jsonb_agg(jsonb_build_object('claim_id',cl.claim_id,'quote',cl.quote,'source_message_id',cl.source_message_id,
      'span_start',cl.span_start,'span_end',cl.span_end,'resolution_state',cl.resolution_state) order by cl.source_sequence,cl.claim_id),'[]'::jsonb)
     from ontology.nexloop_claims cl where cl.tenant_id=x.tenant_id and cl.world=x.world and 'claim:'||cl.claim_id=any(x.dependent_claims))) item
  from ontology.nexloop_candidate_definitions x where x.tenant_id=t and x.world=p_world and x.status='pending_review'
  order by x.created_at,x.candidate_id limit lim) q),'[]'::jsonb);
end $$;

create function authz.nexloop_assert_knowledge_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns text
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare a jsonb:=p_text::jsonb;k bytea;
begin
 if session_user not in ('nexloop_api','nexloop_domain_worker') or p_world is null or octet_length(p_text)>1048576 or octet_length(p_payload)>65536
  or a->>'protocol' is distinct from 'nexloop-review-queue-v1' or a->>'operation' is distinct from 'execute'
  or a->>'action_resource' is null or a->>'action_resource' not in ('eios:action:ontology.schema.review:1','eios:action:nexloop.knowledge.read:1')
  or a->>'parameters_digest' is distinct from encode(sha256(convert_to(p_payload,'UTF8')),'hex') then
  raise exception 'review read unavailable' using errcode='42501';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-review-queue-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'review read unavailable' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform set_config('eios.tenant_id',a->>'tenant_id',true);
 return a->>'tenant_id';
end $$;

create or replace function authz.nexloop_read_review_candidate(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_assert_knowledge_read(p_digest,p_world,p_text,p_signature,p_payload);c jsonb:=p_payload::jsonb;x ontology.nexloop_candidate_definitions%rowtype;
begin
 select * into x from ontology.nexloop_candidate_definitions d where d.tenant_id=t and d.world=p_world and d.candidate_id=c->>'candidate_id' and d.status='pending_review';
 if not found then return null;end if;
 return jsonb_build_object('candidate_id',x.candidate_id,'kind',x.kind,'status',x.status,'revision',x.revision,'candidate',x.candidate,
  'merge_scores',x.merge_scores,'config_version',x.config_version,'created_at',x.created_at,'dependent_claim_count',cardinality(x.dependent_claims),
  'evidence',(select coalesce(jsonb_agg(jsonb_build_object('claim_id',cl.claim_id,'quote',cl.quote,'source_message_id',cl.source_message_id,
     'span_start',cl.span_start,'span_end',cl.span_end,'predicate',cl.predicate,'value',cl.value,'resolution_state',cl.resolution_state)
     order by cl.source_sequence,cl.claim_id),'[]'::jsonb)
    from ontology.nexloop_claims cl where cl.tenant_id=x.tenant_id and cl.world=x.world and 'claim:'||cl.claim_id=any(x.dependent_claims)),
  'similar',(select coalesce(jsonb_agg(jsonb_build_object('candidate_id',s.candidate_id,'status',s.status,'proposed',s.candidate->'proposed',
     'merge_scores',s.merge_scores) order by s.created_at,s.candidate_id),'[]'::jsonb)
    from ontology.nexloop_candidate_definitions s where s.tenant_id=x.tenant_id and s.world=x.world and s.superseded_by=x.candidate_id),
  'cooldown',(select jsonb_build_object('candidate_id',r.candidate_id,'cooldown_until',r.cooldown_until) from ontology.nexloop_candidate_rejections r
    where r.tenant_id=x.tenant_id and r.world=x.world and r.kind=x.kind and r.dedupe_key=x.dedupe_key order by r.rejected_at desc limit 1));
end $$;

do $grants$
declare f text;
begin
 foreach f in array array['authz.nexloop_assert_knowledge_read(text,text,text,text,text)','authz.nexloop_read_review_queue(text,text,text,text,text)',
  'authz.nexloop_read_review_candidate(text,text,text,text,text)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator';
 end loop;
end $grants$;
grant execute on function authz.nexloop_read_review_queue(text,text,text,text,text),authz.nexloop_read_review_candidate(text,text,text,text,text) to nexloop_api,nexloop_domain_worker;
