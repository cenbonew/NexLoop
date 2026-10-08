-- Authoritative configuration directory; not a second business-fact store.
create table control.nexloop_action_definitions (
 tenant_id text not null references control.nexloop_tenants(tenant_id),world text not null,
 resource_id text not null,definition jsonb not null,capability jsonb not null,active boolean not null default true,
 primary key(tenant_id,world,resource_id),
 check(definition->>'tenant_id'=tenant_id),check(definition->>'status'='published'),
 check(resource_id='eios:action:'||(definition->>'stable_name')||':'||(definition->>'version'))
);
alter table control.nexloop_action_definitions owner to nexloop_owner;
alter table control.nexloop_action_definitions enable row level security;
alter table control.nexloop_action_definitions force row level security;
create policy action_definition_tenant on control.nexloop_action_definitions to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_action_definitions from public;
create function authz.nexloop_action_definition_immutable() returns trigger language plpgsql set search_path=pg_catalog as $$
begin
 if (to_jsonb(new)-'active') is distinct from (to_jsonb(old)-'active') then
  raise exception 'published Action version is immutable' using errcode='22000';end if;
 return new;
end $$;
alter function authz.nexloop_action_definition_immutable() owner to nexloop_owner;
revoke all on function authz.nexloop_action_definition_immutable() from public;
create trigger action_definition_immutable before update on control.nexloop_action_definitions
 for each row execute function authz.nexloop_action_definition_immutable();
create trigger action_definition_authority_revision after insert or update or delete on control.nexloop_action_definitions
 for each row execute function authz.nexloop_authority_epoch_guard();

create function authz.nexloop_read_action_definition(p_digest text,p_world text,p_text text,p_signature text)
 returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb:=p_text::jsonb;k bytea;r control.nexloop_action_definitions%rowtype;
begin
 if octet_length(p_text)>262144 then raise exception 'action definition request too large' using errcode='22023';end if;
 select key_material into k from authz.nexloop_authority_signing_keys where key_id=a->>'key_id' and active;
 if not found or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-action-definition-v1:'||p_text,'UTF8'),k,'sha256'),'hex')
  or a->>'protocol' is distinct from 'nexloop-action-definition-v1'
  or (a->>'expires_at')::timestamptz>clock_timestamp()+interval '30 seconds' then
  raise exception 'action definition authorization rejected' using errcode='42501';end if;
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 select * into r from control.nexloop_action_definitions where tenant_id=a->>'tenant_id' and world=p_world and resource_id=a->>'action_resource' and active;
 if not found then raise exception 'published action unavailable' using errcode='42501';end if;
 -- Config publishers advance the locked tenant epoch in the same transaction.
 -- A plain MVCC read avoids row/epoch lock inversion while publication waits.
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 return jsonb_build_object('definition',r.definition,'capability',r.capability);
end $$;
alter function authz.nexloop_read_action_definition(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_read_action_definition(text,text,text,text) from public;
grant execute on function authz.nexloop_read_action_definition(text,text,text,text) to nexloop_api,nexloop_domain_worker,nexloop_action_worker;
