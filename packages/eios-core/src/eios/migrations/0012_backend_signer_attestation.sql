-- Read-only startup proof; no credential/authority/business mutation.
create function authz.nexloop_verify_backend_signer(p_key_id text,p_challenge text,p_signature text)
 returns boolean language plpgsql stable security definer
 set search_path=pg_catalog set row_security=on as $$
declare v_key bytea;
begin
 if p_challenge is null or p_challenge !~ '^[a-f0-9]{64}$'
    or p_signature is null or p_signature !~ '^[a-f0-9]{64}$' then
  return false;
 end if;
 select key_material into v_key from authz.nexloop_authority_signing_keys
 where key_id=p_key_id and active;
 if v_key is null then return false;end if;
 return p_signature=encode(extensions.hmac(
   convert_to('nexloop-backend-signer-v1:'||p_challenge,'UTF8'),v_key,'sha256'),'hex');
end $$;
alter function authz.nexloop_verify_backend_signer(text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_verify_backend_signer(text,text,text) from public;
grant execute on function authz.nexloop_verify_backend_signer(text,text,text)
 to nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler;
