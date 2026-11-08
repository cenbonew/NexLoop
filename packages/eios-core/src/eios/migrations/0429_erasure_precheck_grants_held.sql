-- The owner's erasure precheck counts WebChat grants still held (stage 2026-11-09: customer-4's Consumer was erased on 10-30 but its
-- principal still held nexloop.conversation.read and four other non-empty grant sets; the 0220 erasure deletes the Consumer's data
-- and its owner link, never the 0050 authority facts, which only webchat_consumers.py `revoke: true` empties). Temporary number; the
-- dispatcher renumbers. authz.nexloop_workbench_observe_read is renamed once and wrapped: for the verb erasure_precheck the wrapped
-- body's answer gains held.grants_held, every other verb is the wrapped body unchanged.
-- grants_held = the number of `grants` authority facts with a non-empty grant set whose principal is the Consumer's:
--   * its current owner principals (control.nexloop_consumer_owners; before the erasure), or
--   * a principal whose browser identity references (control.nexloop_principal_identity_refs, 0154) match, by the tenant's salted
--     HMAC, a contact key the Consumer's erasure request recorded (control.nexloop_erased_contact_keys.request_id; after the erasure
--     the owner link is gone and this is the only principal-free trace). A revoked contact key still counts: revoking contact does
--     not revoke WebChat access.
-- Numbers only; no principal or key leaves the function. The owner tool's dry-run post-check fails while it is above zero.
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) rename to nexloop_workbench_observe_read_before_grants_held;
revoke all on function authz.nexloop_workbench_observe_read_before_grants_held(text,text,text,text,text) from public,nexloop_api;
create function authz.nexloop_workbench_observe_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare r jsonb;c jsonb;v_tenant text;v_consumer text;v_request text;k bytea;n bigint;
begin
 r:=authz.nexloop_workbench_observe_read_before_grants_held(p_digest,p_world,p_text,p_signature,p_payload);
 c:=p_payload::jsonb;
 if c->>'verb' is distinct from 'erasure_precheck' then return r;end if;
 -- The wrapped body verified the owner, the signature and the payload, and set eios.tenant_id.
 v_tenant:=current_setting('eios.tenant_id',true);v_consumer:=r->>'consumer_id';v_request:=r->'erasure'->>'request_id';
 select salt into k from control.nexloop_erasure_salts where tenant_id=v_tenant;
 select count(*) into n from authz.nexloop_authority_facts f
  where f.tenant_id=v_tenant and f.fact_kind='grants' and jsonb_array_length(coalesce(f.payload->'grants','[]'::jsonb))>0
   and (exists(select 1 from control.nexloop_consumer_owners o where o.tenant_id=v_tenant and o.world=p_world and o.consumer_id=v_consumer
                and o.principal_id=f.entity_key[1])
        or (v_request is not null and k is not null and exists(
             select 1 from unnest(control.nexloop_principal_identity_refs(v_tenant,p_world,f.entity_key[1])) ref
              join control.nexloop_erased_contact_keys x on x.tenant_id=v_tenant and x.world=p_world and x.request_id=v_request
               and x.key_digest=encode(extensions.hmac(convert_to(ref,'UTF8'),k,'sha256'),'hex'))));
 return jsonb_set(r,'{held,grants_held}',to_jsonb(n));
end $$;
alter function authz.nexloop_workbench_observe_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_observe_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_observe_read(text,text,text,text,text) to nexloop_api;
