"""K4b (migration human_questions): the exact edits it makes to two earlier functions, for the copy guard (old, new)."""
EDITS = {
 "authz.nexloop_record_plan_outcome": [
  [
   "(select array_agg(k order by k) from jsonb_object_keys(o) k) is distinct from array['evidence_refs','intent_ref','kind','plan_update','reasons','reassess_at','schema_version']",
   "(select array_agg(k order by k) from jsonb_object_keys(o-'question') k) is distinct from array['evidence_refs','intent_ref','kind','plan_update','reasons','reassess_at','schema_version']"
  ],
  [
   "or o->>'schema_version' is distinct from '1.0' or",
   "or coalesce(o->>'schema_version','') not in ('1.0','1.1') or (o ? 'question' and not runtime.nexloop_run_question_valid(o)) or"
  ],
  [
   "  values(t,p_world,act.run_id,p.plan_id,p.version,o->>'kind',o,digest,(o->>'intent_ref')::uuid,v_new);\n",
   "  values(t,p_world,act.run_id,p.plan_id,p.version,o->>'kind',o,digest,(o->>'intent_ref')::uuid,v_new);\n if o ? 'question' then perform runtime.nexloop_human_question_open(t,p_world,act.run_id,p.plan_id,p.version,o->'question');end if;\n"
  ]
 ],
 "authz.nexloop_retention_command": [
  [
   " elsif c->>'verb'='status' then\n",
   " elsif c->>'verb'='questions_expire' then\n  -- K4b (D2): open human questions past due_by expire and wake their plan once; nothing is asked again.\n  return runtime.nexloop_human_questions_expire(t,p_world,(s->'sweep'->>'batch')::integer);\n elsif c->>'verb'='status' then\n"
  ]
 ]
}
