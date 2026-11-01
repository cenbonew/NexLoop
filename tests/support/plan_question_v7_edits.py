"""K4h K1 (migration plan_question_context_v7): the exact edit it makes to the 0397 outcome port, for the copy guard (old, new)."""
EDITS = {
 "authz.nexloop_record_plan_outcome": [
  [
   "(o ? 'question' and not runtime.nexloop_run_question_valid(o))",
   "(o ? 'question' and (not runtime.nexloop_run_question_valid(o) or not runtime.nexloop_run_context_v7(t,p_world,act.run_id)))"
  ]
 ]
}
