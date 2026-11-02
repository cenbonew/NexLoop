"""Claim → Consumer property phase one (migration claim_auto_write): the exact edits it makes to the 0074 match recorder, for the
copy guard (old, new)."""
EDITS = {
 "authz.nexloop_record_claim_match": [
  [
   """     effective_at,supersedes_claim,proposal)
    values(t,p_world,p->>'proposal_id',p->>'business_intent_ref',cl.claim_id,v_outcome,'proposed',p->>'op',p->>'type_name',p->>'target_ref',p->>'property_name',
     (p->>'effective_at')::timestamptz,p->>'supersedes_claim',p->'proposal')""",
   """     effective_at,supersedes_claim,proposal,reason)
    values(t,p_world,p->>'proposal_id',p->>'business_intent_ref',cl.claim_id,v_outcome,'proposed',p->>'op',p->>'type_name',p->>'target_ref',p->>'property_name',
     (p->>'effective_at')::timestamptz,p->>'supersedes_claim',p->'proposal',
     case when ontology.nexloop_auto_write_allowed(t,p_world,p->>'type_name',ontology.nexloop_proposal_properties(p->>'op',p->>'property_name',p->'proposal'))
      then '' else 'auto_write_not_allowed' end)"""
  ],
  [
   """   raise exception 'proposal apply arguments required' using errcode='22023';end if;""",
   """   raise exception 'proposal apply arguments required' using errcode='22023';end if;
  -- Claim auto-write whitelist: a proposal outside it is never applied by the matcher (it stays for review).
  if c->>'to'='applying' and not ontology.nexloop_auto_write_allowed(t,p_world,pr.type_name,ontology.nexloop_proposal_properties(pr.op,pr.property_name,pr.proposal)) then
   raise exception 'auto write not allowed' using errcode='42501';end if;"""
  ]
 ]
}
