-- D23 option B (L6 G1 on 0431 action_input_shadow): authz.nexloop_action_input_tenant caught every error of the service identity
-- resolution and turned it into "no tenant", so a database fault, a bug or a timeout silently dropped the shadow counts. Identity
-- resolution (authz.nexloop_service_identity, 0034, and the root identity it calls) refuses only with 42501 (insufficient_privilege):
-- only that is "not a service credential here" and falls through to the browser realm (fail closed for a machine digest or an
-- unknown one, as before). Any other error is raised; the governor's shadow_validate then leaves dispatch untouched, counts
-- nothing and counts the failure itself (action_governor.SHADOW_FAILURES, with a warning naming the error class).
create or replace function authz.nexloop_action_input_tenant(p_digest text,p_world text) returns text
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare v text;
begin
 if coalesce(p_digest,'')!~'^[a-f0-9]{64}$' then return null;end if;
 begin
  v:=authz.nexloop_service_identity(p_digest,p_world)->'binding'->>'tenant_id';
 exception when insufficient_privilege then v:=null;
 end;
 if v is null then
  select r.tenant_id into v from authz.nexloop_browser_token_realms r where r.token_digest=p_digest;
 end if;
 return v;
end $$;
alter function authz.nexloop_action_input_tenant(text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_action_input_tenant(text,text) from public;
