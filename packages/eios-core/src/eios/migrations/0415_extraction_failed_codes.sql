-- Claim-extraction failures by code (L6 follow-up to S-DUP, 2026-11-03). Temporary number; the dispatcher renumbers. Published migrations
-- are not edited.
-- A claim-extraction job that ends failed is never retried and, unlike a dead letter, raised no alert: 'denied' (authority missing or
-- revoked) and, since S-DUP, 'context_cited' (the 0412 recorder refused a Claim citing a context Message: the client filter that drops
-- them before the recorder failed) were silent. runtime.nexloop_data_health is renamed once and wrapped; the extraction block gains
--   failed_24h          claim-extraction jobs that ended failed in the last 24 hours, by result code ({} when none)
--   context_cited_24h   of those, code context_cited (always a number)
--   finished_24h        claim-extraction jobs that ended (succeeded, failed or dead-lettered) in the last 24 hours
--   no_data             1 when finished_24h is 0: nothing measured, which is not a pass; 0 otherwise
-- and the reason context_cited when context_cited_24h > 0. Alert rules v30 read context_cited_24h and no_data.

alter function runtime.nexloop_data_health(text,text) rename to nexloop_data_health_before_extraction_failed;
revoke all on function runtime.nexloop_data_health_before_extraction_failed(text,text) from public;
create function runtime.nexloop_data_health(p_tenant text,p_world text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare h jsonb:=runtime.nexloop_data_health_before_extraction_failed(p_tenant,p_world);x jsonb;v_failed jsonb;v_cited bigint;v_finished bigint;reasons jsonb;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select coalesce(jsonb_object_agg(f.code,f.n),'{}'::jsonb),coalesce(sum(f.n) filter (where f.code='context_cited'),0) into v_failed,v_cited
  from (select coalesce(j.result->>'code','unknown') code,count(*) n from runtime.jobs j
   where j.tenant_id=p_tenant and j.world=p_world and j.queue='claim-extraction' and j.status='failed' and j.updated_at>clock_timestamp()-interval '24 hours'
   group by 1) f;
 select count(*) into v_finished from runtime.jobs j
  where j.tenant_id=p_tenant and j.world=p_world and j.queue='claim-extraction' and j.status in ('succeeded','failed','dead_lettered')
   and j.updated_at>clock_timestamp()-interval '24 hours';
 x:=h->'extraction';
 reasons:=coalesce((select jsonb_agg(r) from jsonb_array_elements_text(x->'reasons') r where r<>'context_cited'),'[]'::jsonb);
 if v_cited>0 then reasons:=reasons||'["context_cited"]'::jsonb;end if;
 x:=x||jsonb_build_object('failed_24h',v_failed,'context_cited_24h',v_cited,'finished_24h',v_finished,'no_data',case when v_finished=0 then 1 else 0 end,
  'reasons',reasons,'status',case when jsonb_array_length(reasons)=0 then 'ok' else 'attention' end);
 return jsonb_set(h,'{extraction}',x);
end $$;
alter function runtime.nexloop_data_health(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_data_health(text,text) from public;
