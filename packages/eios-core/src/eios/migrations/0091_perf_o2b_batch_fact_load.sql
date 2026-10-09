-- O2b (temporary number): load many authority-fact snapshots in one round trip.
-- Each element is produced by the existing authz.nexloop_load_authority_fact_snapshot
-- (same identity binding, Run-resource and key checks, same payload and record_hash);
-- this function only batches the calls. A key that the single-call function would
-- reject is reported as {"status":"error"} (the caller then re-reads it singly, which
-- raises exactly as before); a missing fact is {"status":"missing"}.
-- Published 0001..0087 are unchanged.
create function authz.nexloop_load_authority_facts(p_digest text,p_world text,p_keys jsonb) returns jsonb
language plpgsql stable security definer set search_path=pg_catalog set row_security=on as $$
declare item jsonb;v_key text[];v jsonb;v_out jsonb:='[]'::jsonb;
begin
 if jsonb_typeof(p_keys) is distinct from 'array' or jsonb_array_length(p_keys)>512 then
  raise exception 'invalid authority fact batch' using errcode='22023';end if;
 for item in select value from jsonb_array_elements(p_keys) loop
  if jsonb_typeof(item) is distinct from 'object' or jsonb_typeof(item->'kind') is distinct from 'string'
   or jsonb_typeof(item->'key') is distinct from 'array' or jsonb_array_length(item->'key') not between 1 and 3 then
   raise exception 'invalid authority fact batch' using errcode='22023';end if;
  select array_agg(x order by o) into v_key from jsonb_array_elements_text(item->'key') with ordinality as t(x,o);
  begin
   v:=authz.nexloop_load_authority_fact_snapshot(p_digest,p_world,item->>'kind',v_key);
   v_out:=v_out||jsonb_build_array(case when v is null then jsonb_build_object('status','missing')
     else jsonb_build_object('status','ok','value',v) end);
  exception when others then
   v_out:=v_out||jsonb_build_array(jsonb_build_object('status','error'));
  end;
 end loop;
 return v_out;
end $$;
alter function authz.nexloop_load_authority_facts(text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_load_authority_facts(text,text,jsonb) from public;
grant execute on function authz.nexloop_load_authority_facts(text,text,jsonb) to nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;
