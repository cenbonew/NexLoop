"""S-DUP (docs/implementation/S-DUP-extraction-dedupe-design.md): the exact edits the extraction_target_context and extraction_dedupe
migrations make to the 0069 feed port and the 0066 recorder, for the copy guard (old, new)."""
FEED = {
 "authz.nexloop_claim_extraction_feed": [
  [
   """ v_result jsonb;v_quiet integer;v_limit integer;v_window integer;v_count integer;j runtime.jobs%rowtype;""",
   """ v_result jsonb;v_quiet integer;v_limit integer;v_window integer;v_count integer;j runtime.jobs%rowtype;v_context integer;"""
  ],
  [
   """  v_quiet:=(c->>'quiet_seconds')::integer;v_limit:=(c->>'limit')::integer;v_window:=(c->>'window')::integer;
  if v_quiet not between 0 and 3600 or v_limit not between 1 and 100 or v_window not between 1 and 200 then""",
   """  v_quiet:=(c->>'quiet_seconds')::integer;v_limit:=(c->>'limit')::integer;v_window:=(c->>'window')::integer;
  v_context:=coalesce((c->>'context')::integer,10);
  if v_quiet not between 0 and 3600 or v_limit not between 1 and 100 or v_window not between 1 and 200 or v_context not between 0 and 50 then"""
  ],
  [
   """    'message_ids',(select jsonb_agg(w.message_id order by w.sequence) from (select m.message_id,m.sequence from runtime.nexloop_conversation_messages m
       where m.tenant_id=v_tenant and m.world=p_world and m.conversation_id=d.conversation_id and m.sequence<=d.through
       order by m.sequence desc limit v_window) w)) order by d.oldest,d.conversation_id),'[]'::jsonb) into v_result""",
   """    -- S-DUP: the targets are the window's pending Messages only; earlier Messages come as context, never extracted again.
    'message_ids',(select jsonb_agg(w.message_id order by w.sequence) from (select f2.message_id,f2.sequence from runtime.nexloop_claim_extraction_feed f2
       where f2.tenant_id=v_tenant and f2.world=p_world and f2.conversation_id=d.conversation_id and f2.task_id is null and f2.sequence<=d.through
       order by f2.sequence desc limit v_window) w),
    'context_message_ids',coalesce((select jsonb_agg(x.message_id order by x.sequence) from (select m.message_id,m.sequence from runtime.nexloop_conversation_messages m
       where m.tenant_id=v_tenant and m.world=p_world and m.conversation_id=d.conversation_id
         and m.sequence<(select min(w2.sequence) from (select f3.sequence from runtime.nexloop_claim_extraction_feed f3
           where f3.tenant_id=v_tenant and f3.world=p_world and f3.conversation_id=d.conversation_id and f3.task_id is null and f3.sequence<=d.through
           order by f3.sequence desc limit v_window) w2)
       order by m.sequence desc limit v_context) x),'[]'::jsonb)) order by d.oldest,d.conversation_id),'[]'::jsonb) into v_result"""
  ]
 ]
}
RECORDER = {
 "authz.nexloop_record_claim_extraction_v0066": [
  [
   """ v_start integer;v_end integer;
begin""",
   """ v_start integer;v_end integer;v_dup text;v_refs jsonb;
begin"""
  ],
  [
   """ for cl in select value from jsonb_array_elements(c->'claims') loop
  v_kind:=cl->>'epistemic_kind';""",
   """ -- S-DUP: a Claim another Claim of this run cites (derivation, correction) is always written as it is.
 select coalesce(jsonb_object_agg(r.x,true),'{}'::jsonb) into v_refs from (
   select jsonb_array_elements_text(value->'derived_from') x from jsonb_array_elements(c->'claims')
   union select value->>'corrects_claim_id' from jsonb_array_elements(c->'claims') where jsonb_typeof(value->'corrects_claim_id')='string') r;
 for cl in select value from jsonb_array_elements(c->'claims') loop
  v_kind:=cl->>'epistemic_kind';"""
  ],
  [
   """  insert into ontology.nexloop_claims values(v_tenant,p_world,cl->>'claim_id',""",
   """  -- S-DUP: the same statement again (same Message span, kind, polarity, modality, condition presence and normalized value, still current)
  -- is not written a second time; this run links the Claim already recorded.
  v_dup:=null;
  if src is not null and not v_refs ? (cl->>'claim_id') then
   select x.claim_id into v_dup from ontology.nexloop_claims x where x.tenant_id=v_tenant and x.world=p_world and x.source_message_id=cl->>'source_message_id'
     and x.span_start=v_start and x.span_end=v_end and x.epistemic_kind=v_kind and x.polarity=cl->>'polarity' and x.modality=cl->>'modality'
     and (x.condition_text<>'')=(coalesce(cl->>'condition','')<>'') and x.claim_id<>cl->>'claim_id'
     and ontology.nexloop_claim_value_key(x.value)=ontology.nexloop_claim_value_key(cl->'value')
     and x.superseded_at is null and x.resolution_state<>'superseded'
    order by x.recorded_at,x.claim_id limit 1;
  end if;
  if v_dup is null then
  insert into ontology.nexloop_claims values(v_tenant,p_world,cl->>'claim_id',"""
  ],
  [
   """   on conflict(tenant_id,world,claim_id) do nothing;
  insert into ontology.nexloop_extraction_run_claims values(v_tenant,p_world,c->>'input_digest',cl->>'claim_id') on conflict do nothing;
  v_claims:=v_claims||jsonb_build_object(cl->>'claim_id',""",
   """   on conflict(tenant_id,world,claim_id) do nothing;
  end if;
  insert into ontology.nexloop_extraction_run_claims values(v_tenant,p_world,c->>'input_digest',coalesce(v_dup,cl->>'claim_id')) on conflict do nothing;
  v_claims:=v_claims||jsonb_build_object(coalesce(v_dup,cl->>'claim_id'),"""
  ]
 ]
}
EDITS = {**FEED, **RECORDER}
