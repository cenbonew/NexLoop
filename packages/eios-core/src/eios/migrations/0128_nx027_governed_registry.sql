-- NX-027 (temporary number 0140): the two NX-027 human Actions join the NX-028 governed entry registry (0124).
--
-- commitment.bind_commercial (operation bind_commitment_commercial): bind a commitment to one commercial reference
--   (connector, kind, external id); evidence follows only from verified records of that reference (0121).
-- cost.record (operation record_cost): a service / labour cost entered by a person, corrected by supersession (0120).
-- Both human only (subject_rule 'human'; services, Agents and Run credentials are refused by the entry before any
-- handler runs). The entry passes the whole request body, which carries 'operation' and 'request_id'; the 0120/0121
-- handlers accept their own fields only, so two thin adapters drop the entry's fields. The entry itself and the
-- published handlers are not changed.

create function control.nexloop_governed_bind_commercial(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language sql set search_path=pg_catalog,pg_temp as $$
 select control.nexloop_commercial_bind_commitment_handler(p_tenant,p_world,p_principal,p_subject,p_intent,body-'operation'-'request_id') $$;
create function control.nexloop_governed_cost_record(p_tenant text,p_world text,p_principal text,p_subject text,p_intent text,body jsonb) returns jsonb
 language sql set search_path=pg_catalog,pg_temp as $$
 select control.nexloop_cost_record_handler(p_tenant,p_world,p_principal,p_subject,p_intent,body-'operation') $$;

do $owners$
declare f text;
begin
 foreach f in array array['control.nexloop_governed_bind_commercial(text,text,text,text,text,jsonb)','control.nexloop_governed_cost_record(text,text,text,text,text,jsonb)'] loop
  execute 'alter function '||f||' owner to nexloop_owner';
  execute 'revoke all on function '||f||' from public';
 end loop;
end $owners$;

insert into control.nexloop_governed_capabilities(capability,operation,subject_rule,handler,source_task) values
 ('commitment.bind_commercial','bind_commitment_commercial','human','control.nexloop_governed_bind_commercial(text,text,text,text,text,jsonb)','NX-027'),
 ('cost.record','record_cost','human','control.nexloop_governed_cost_record(text,text,text,text,text,jsonb)','NX-027');
