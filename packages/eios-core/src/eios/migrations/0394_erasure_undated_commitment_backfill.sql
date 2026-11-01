-- I36-e (owner 2026-11-01; I36-d analysis; temporary number 0394): the one remaining open commitment of an erased Consumer on stage.
-- 0388 cancels an erased Consumer's undated commitments in two ways: the completion trigger (§c, erasures completing after 0388) and
-- the stock block (§d), which joins runtime.nexloop_commitments and so reaches only commitments whose registry row is still there
-- (erased before 0306). An erasure that completed after 0306 but before 0388 deleted the registry rows and was never reached: its
-- commitment keeps made_to 'erasure:<request id>', no due_at, an open status, and no keeper can move it (stage precheck 2026-11-01:
-- exactly one, erasure completed 2026-10-27).
-- This block is 0388 §d without the registry row condition: Commitment objects whose made_to is an erasure request marker, status
-- conditional / open / in_progress, undated or already cancel-requested; the same cancel request (reason consumer_erasure, ref and
-- principal the marker) when none exists yet, and the same runtime.nexloop_commitment_cancel_apply (it reads only the object and the
-- events). Idempotent: a second run writes no second cancel request and no second status event. Nothing else changes.
do $stock$
declare x record;
begin
 for x in select o.tenant_id,o.world,o.object_id,o.properties->>'made_to' made_to,o.properties->>'due_at' due_at from ontology.objects o
  where o.type_name='Commitment' and o.properties->>'made_to' ~ '^erasure:[A-Za-z0-9._:-]{1,190}$'
   and o.properties->>'status' in ('conditional','open','in_progress')
  order by o.tenant_id,o.world,o.object_id loop
  perform set_config('eios.tenant_id',x.tenant_id,true);
  continue when x.due_at is not null and not runtime.nexloop_commitment_cancel_requested(x.tenant_id,x.world,x.object_id);
  if not runtime.nexloop_commitment_cancel_requested(x.tenant_id,x.world,x.object_id) then
   insert into runtime.nexloop_commitment_events(tenant_id,world,subject_ref,kind,ref,detail,principal_id)
    values(x.tenant_id,x.world,'commitment:'||x.object_id,'cancel_requested',x.made_to,jsonb_build_object('reason','consumer_erasure'),x.made_to);
  end if;
  perform runtime.nexloop_commitment_cancel_apply(x.tenant_id,x.world,x.object_id,'consumer_erasure');
 end loop;
 perform set_config('eios.tenant_id','',true);
end $stock$;
