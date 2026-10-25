"""NX-057 R2 (migration remediation_wake): the exact edits it makes to three R1 functions, for the copy guard (old, new)."""
EDITS = {
 "control.nexloop_configure_remediation_rules": [
  [
   "is distinct from array['consumer_per_day','tenant_per_hour']",
   "is distinct from array['breaker_per_day','consumer_per_day','plan_wakes_per_week','tenant_per_hour']"
  ],
  [
   "  or coalesce(p_manifest->'limits'->>'consumer_per_day','')!~'^[1-9][0-9]{0,1}$' then",
   "  or coalesce(p_manifest->'limits'->>'consumer_per_day','')!~'^[1-9][0-9]{0,1}$'\n  or coalesce(p_manifest->'limits'->>'plan_wakes_per_week','')!~'^[1-9]$' or coalesce(p_manifest->'limits'->>'breaker_per_day','')!~'^[1-9][0-9]?$' then"
  ],
  [
   "  -- R1 opens escalate only; reevaluate (R2) and governed_action (R3) are refused until their slice.\n  if r->>'kind'<>'escalate' then raise exception 'remediation rules: kind_unavailable' using errcode='22023';end if;",
   "  -- R2 opens reevaluate; governed_action (R3) is refused until its slice.\n  if r->>'kind'='governed_action' then raise exception 'remediation rules: kind_unavailable' using errcode='22023';end if;"
  ]
 ],
 "authz.nexloop_remediation_plan_command": [
  [
   "x record;v_actor text:=(p_text::jsonb)->>'principal_id';",
   "x record;v_actor text:=(p_text::jsonb)->>'principal_id';v_origin uuid;v_held text[];"
  ],
  [
   "   if not found or i.state<>'failed' then v_decision:='not_failed';end if;\n  end if;\n",
   "   if not found or i.state<>'failed' then v_decision:='not_failed';end if;\n  end if;\n  -- R2 (design §9.1): an intent submitted by a remediation Run (its Run consumed a wake) only escalates, whatever the rule says.\n  if v_decision is null then v_origin:=runtime.nexloop_remediation_origin(t,p_world,i.intent_id);end if;\n"
  ],
  [
   "     or not (r->'failure_kinds' ? f.reason_code);\n",
   "     or not (r->'failure_kinds' ? f.reason_code);\n    -- R2 (design §9.3): a held rule generates nothing until the owner releases it.\n    if runtime.nexloop_remediation_rule_held(t,p_world,r->>'rule_id') then v_decision:=coalesce(v_decision,'rule_held');continue;end if;\n"
  ],
  [
   "    v_code:=case when v_consumer_recent>=",
   "    v_code:=case when v_origin is not null then 'remediation_origin' when v_consumer_recent>="
  ],
  [
   "      rule_id,rule_version,rule_digest,kind,mode,state,due_by,last_code)\n",
   "      rule_id,rule_version,rule_digest,kind,mode,state,due_by,last_code,origin_remediation_id)\n"
  ],
  [
   "      case when v_code='chain_capped' then 'escalate' else r->>'kind' end,r->>'mode','proposed',\n      clock_timestamp()+make_interval(secs=>(r->>'due_after_seconds')::integer),v_code)\n",
   "      case when v_code in ('chain_capped','remediation_origin') then 'escalate' else r->>'kind' end,r->>'mode','proposed',\n      clock_timestamp()+make_interval(secs=>(r->>'due_after_seconds')::integer),v_code,v_origin)\n"
  ],
  [
   "     -- An automatic escalation goes to the owner at once; a manual one waits for the owner's resolution.\n     if r->>'mode'='automatic' or v_code='chain_capped' then\n      perform runtime.nexloop_remediation_move(v_id,'escalated',case when v_code='chain_capped' then 'chain_capped' else 'escalated_automatic' end,\n       'service',v_actor,null);\n     end if;\n",
   "     -- An automatic escalation goes to the owner at once; a manual one waits for the owner's resolution. An automatic reevaluate\n     -- wakes the waiting plan now (R2); a capped or remediation-origin one is an escalation only.\n     if v_code in ('chain_capped','remediation_origin') or (r->>'mode'='automatic' and r->>'kind'='escalate') then\n      perform runtime.nexloop_remediation_move(v_id,'escalated',case when v_code='proposed' then 'escalated_automatic' else v_code end,\n       'service',v_actor,null);\n     elsif r->>'mode'='automatic' and r->>'kind'='reevaluate' then\n      perform runtime.nexloop_remediation_wake(t,p_world,v_id,'service',v_actor,null);\n     end if;\n"
  ],
  [
   " return jsonb_build_object('status',coalesce('closed:'||v_closed,'open'),'expired',v_expired,'decisions',counts);",
   " -- R2 (design §9.3): the breaker holds a rule past its limit in 24 hours; the owner releases it.\n v_held:=runtime.nexloop_remediation_breaker(t,p_world);\n return jsonb_build_object('status',coalesce('closed:'||v_closed,'open'),'expired',v_expired,'decisions',counts,'held',to_jsonb(v_held));"
  ]
 ],
 "runtime.nexloop_governed_resolve_remediation": [
  [
   "declare r runtime.nexloop_remediation_intents;v_to text;",
   "declare r runtime.nexloop_remediation_intents;v_to text;v_wake jsonb;"
  ],
  [
   "  or coalesce(body->>'outcome','') not in ('handled','cancelled') or",
   "  or coalesce(body->>'outcome','') not in ('handled','cancelled','wake') or"
  ],
  [
   " if r.state not in ('proposed','escalated') then raise exception 'remediation: not_open' using errcode='22023';end if;\n",
   " if r.state not in ('proposed','escalated') then raise exception 'remediation: not_open' using errcode='22023';end if;\n -- R2: a manual reevaluate is woken by the owner (the same wake, its plan cap and switches; design §6.2).\n if body->>'outcome'='wake' then\n  if r.kind<>'reevaluate' then raise exception 'remediation: not_reevaluate' using errcode='22023';end if;\n  if r.state<>'proposed' then raise exception 'remediation: not_open' using errcode='22023';end if;\n  v_wake:=runtime.nexloop_remediation_wake(p_tenant,p_world,r.remediation_id,'human',p_principal,body->>'reason');\n  if v_wake ? 'closed' then raise exception 'remediation: switch_closed' using errcode='22023';end if;\n  return v_wake;\n end if;\n"
  ]
 ]
}
