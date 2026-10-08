-- Governed genuine Human local Browser identity association.
alter function authz.nexloop_assert_edit_authority(text,text,jsonb) rename to nexloop_nonbrowser_assert_edit_authority_identity;
revoke all on function authz.nexloop_nonbrowser_assert_edit_authority_identity(text,text,jsonb) from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker;
create function authz.nexloop_assert_edit_authority(p_digest text,p_world text,p_claims jsonb) returns jsonb
language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare identity jsonb;binding jsonb;entry jsonb;keys text[];snap jsonb;kind text;counted integer:=0;
 locator authz.nexloop_browser_token_realms;
begin
 select br.* into locator from authz.nexloop_browser_token_realms br where br.token_digest=p_digest;
 if not found then return authz.nexloop_nonbrowser_assert_edit_authority_identity(p_digest,p_world,p_claims);end if;
 identity:=authz.nexloop_browser_identity_snapshot(p_digest,p_world);binding:=identity->'binding';
 -- Lock live canonical rows through the eventual business commit. No cached
 -- HTTP inspection or model supplied principal/realm is authority.
 perform 1 from control.nexloop_browser_sessions bs where bs.tenant_id=locator.tenant_id and bs.session_id=locator.session_id for share;
 perform 1 from control.nexloop_browser_applications ba where ba.tenant_id=locator.tenant_id and ba.application_id=identity->>'browser_application_id' for share;
 perform 1 from control.nexloop_browser_subjects su where su.subject_id=binding->>'subject_id' for share;
 perform 1 from control.nexloop_browser_accounts ac where ac.tenant_id=locator.tenant_id and ac.local_account_id=binding->>'credential_id' for share;
 perform 1 from control.nexloop_browser_memberships bm where bm.tenant_id=locator.tenant_id and bm.principal_id=binding->>'subject_principal_id' for share;
 perform 1 from control.nexloop_browser_business_applications bc where bc.tenant_id=locator.tenant_id and bc.application_id=identity->>'browser_application_id' for share;
 identity:=authz.nexloop_browser_identity_snapshot(p_digest,p_world);binding:=identity->'binding';
 if p_claims->>'tenant_id' is distinct from binding->>'tenant_id' or p_claims->>'credential_id' is distinct from binding->>'credential_id'
  or p_claims->>'principal_id' is distinct from binding->>'subject_principal_id' or p_claims->>'world' is distinct from p_world
  or p_claims->>'directory_hash' is distinct from identity->>'directory_hash'
  or p_claims->>'resource_id' is distinct from p_claims->>'target_resource' or p_claims->>'operation' is distinct from 'edit'
  or p_claims->>'expires_at' is null or (p_claims->>'expires_at')::timestamptz<=clock_timestamp()
  or jsonb_typeof(p_claims->'facts') is distinct from 'array' or jsonb_array_length(p_claims->'facts')<>12 then
  raise exception 'browser edit authority stale' using errcode='42501';end if;
 for entry in select value from jsonb_array_elements(p_claims->'facts') order by value->>'kind',(value->'key')::text loop
  kind:=entry->>'kind';select array_agg(value) into keys from jsonb_array_elements_text(entry->'key');
  if kind in ('actor','application','subject_authority','resource_graph','grants','scope','controls','policies','revision') then
   perform 1 from authz.nexloop_authority_facts af where af.tenant_id=locator.tenant_id and af.fact_kind=kind and af.entity_key=keys for share;
   if not found then raise exception 'browser edit authority missing' using errcode='42501';end if;
  elsif kind not in ('subject','membership','browser_authentication') then raise exception 'browser edit authority invalid' using errcode='42501';end if;
  snap:=authz.nexloop_browser_authority_fact_snapshot(p_digest,p_world,kind,keys);
  if snap is null or snap->>'record_hash' is distinct from entry->>'record_hash' then raise exception 'browser edit authority changed' using errcode='42501';end if;
  counted:=counted+1;
 end loop;
 if (select count(distinct e->>'kind') from jsonb_array_elements(p_claims->'facts') e)<>12 then raise exception 'browser edit fact coverage invalid' using errcode='42501';end if;
 perform 1 from control.nexloop_tenants t where t.tenant_id=locator.tenant_id for share;
 identity:=authz.nexloop_browser_identity_snapshot(p_digest,p_world);
 if p_claims->>'directory_hash' is distinct from identity->>'directory_hash' or (p_claims->>'expires_at')::timestamptz<=clock_timestamp() then
  raise exception 'browser edit authority expired' using errcode='42501';end if;
 return identity->'binding';
end $$;
alter function authz.nexloop_assert_edit_authority(text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_assert_edit_authority(text,text,jsonb) from public;

-- candidate, test-owned bootstrap only. Technical current Action provenance receipts,
-- never an alternate Subject/Account/Consumer authority store.
create table control.nexloop_consumer_identity_link_receipts (
 tenant_id text not null,world text not null,identity_ref text not null,
 consumer_id text not null,subject_id text not null,account_id text not null,
 request_payload jsonb not null,recorded_txid bigint not null,
 primary key(tenant_id,world,identity_ref)
);
alter table control.nexloop_consumer_identity_link_receipts owner to nexloop_owner;
alter table control.nexloop_consumer_identity_link_receipts enable row level security;
alter table control.nexloop_consumer_identity_link_receipts force row level security;
create policy consumer_identity_receipt_tenant on control.nexloop_consumer_identity_link_receipts to nexloop_owner
 using(tenant_id=current_setting('eios.tenant_id',true)) with check(tenant_id=current_setting('eios.tenant_id',true));
revoke all on control.nexloop_consumer_identity_link_receipts from public,nexloop_api,nexloop_identity,nexloop_domain_worker,nexloop_action_worker;

create function authz.nexloop_prepare_browser_identity_link(p_digest text,p_world text,p_text text,p_signature text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb;identity jsonb;binding jsonb;key_material bytea;owner control.nexloop_consumer_owners;
 object ontology.objects;ref text;prior control.nexloop_consumer_identity_link_receipts;refs jsonb;result jsonb;
begin
 if session_user<>'nexloop_api' then raise exception 'identity link unavailable' using errcode='42501';end if;
 a:=p_text::jsonb;select sk.key_material into key_material from authz.nexloop_authority_signing_keys sk where sk.key_id=a->>'key_id' and active;
 if key_material is null or p_signature is distinct from encode(extensions.hmac(convert_to('nexloop-identity-link-prepare-v1:'||p_text,'UTF8'),key_material,'sha256'),'hex')
  or a->>'action_resource' is distinct from 'eios:action:nexloop.consumer.link_authenticated_identity:1' then
  raise exception 'identity link unavailable' using errcode='42501';end if;
 identity:=authz.nexloop_browser_identity_snapshot(p_digest,p_world);binding:=identity->'binding';
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform set_config('eios.tenant_id',binding->>'tenant_id',true);
 select * into owner from control.nexloop_consumer_owners where tenant_id=binding->>'tenant_id' and world=p_world and principal_id=binding->>'subject_principal_id' for share;
 if not found or not exists(select 1 from ontology.objects o where o.tenant_id=owner.tenant_id and o.world=p_world and o.type_name='ConsumerOwnership'
  and o.object_id=owner.ownership_id and o.properties=jsonb_build_object('consumer_id',owner.consumer_id,'principal_id',owner.principal_id)) then
  raise exception 'identity ownership unavailable' using errcode='42501';end if;
 select * into object from ontology.objects o where o.tenant_id=owner.tenant_id and o.world=p_world and o.type_name='Consumer' and o.object_id=owner.consumer_id for share;
 if not found then raise exception 'identity ownership unavailable' using errcode='42501';end if;
 ref:='nexloop:browser-local:'||encode(sha256(convert_to(owner.tenant_id||':'||p_world||':'||(binding->>'subject_id')||':'||(binding->>'credential_id'),'UTF8')),'hex');
 select * into prior from control.nexloop_consumer_identity_link_receipts where tenant_id=owner.tenant_id and world=p_world and identity_ref=ref;
 if found then
  if prior.consumer_id<>owner.consumer_id then raise exception 'identity link conflict' using errcode='23505';end if;
  return prior.request_payload;
 end if;
 refs:=coalesce(object.properties->'external_id_refs','[]'::jsonb);
 if jsonb_typeof(refs)<>'array' then raise exception 'identity profile unavailable' using errcode='23514';end if;
 select jsonb_agg(value order by value) into refs from (select distinct value from jsonb_array_elements(refs||jsonb_build_array(ref))) x;
 result:=jsonb_build_object('request_id','identity-link-'||encode(sha256(convert_to(owner.tenant_id||':'||p_world||':'||owner.consumer_id||':'||ref,'UTF8')),'hex'),
  'type_name','Consumer','object_id',owner.consumer_id,'expected_revision',object.nexloop_revision,'properties',jsonb_build_object('external_id_refs',refs));
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 return result;
end $$;
alter function authz.nexloop_prepare_browser_identity_link(text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_prepare_browser_identity_link(text,text,text,text) from public;
grant execute on function authz.nexloop_prepare_browser_identity_link(text,text,text,text) to nexloop_api;

create function authz.nexloop_link_browser_identity_action(p_digest text,p_world text,p_text text,p_signature text,p_payload text)
returns jsonb language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare a jsonb;payload jsonb;identity jsonb;binding jsonb;owner control.nexloop_consumer_owners;ref text;
 object ontology.objects;existing jsonb;expected jsonb;outcome jsonb;
begin
 if session_user<>'nexloop_api' then raise exception 'identity link unavailable' using errcode='42501';end if;
 a:=p_text::jsonb;payload:=p_payload::jsonb;
 if a->>'action_resource' is distinct from 'eios:action:nexloop.consumer.link_authenticated_identity:1' then raise exception 'identity link unavailable' using errcode='42501';end if;
 identity:=authz.nexloop_browser_identity_snapshot(p_digest,p_world);binding:=identity->'binding';
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform set_config('eios.tenant_id',binding->>'tenant_id',true);
 select * into owner from control.nexloop_consumer_owners where tenant_id=binding->>'tenant_id' and world=p_world and principal_id=binding->>'subject_principal_id' for share;
 if not found or owner.consumer_id is distinct from payload->>'object_id' or not exists(select 1 from ontology.objects o where o.tenant_id=owner.tenant_id and o.world=p_world and o.type_name='ConsumerOwnership'
  and o.object_id=owner.ownership_id and o.properties=jsonb_build_object('consumer_id',owner.consumer_id,'principal_id',owner.principal_id)) then raise exception 'identity ownership unavailable' using errcode='42501';end if;
 select * into object from ontology.objects o where o.tenant_id=owner.tenant_id and o.world=p_world and o.type_name='Consumer' and o.object_id=owner.consumer_id for update;
 if not found then raise exception 'identity ownership unavailable' using errcode='42501';end if;
 ref:='nexloop:browser-local:'||encode(sha256(convert_to(owner.tenant_id||':'||p_world||':'||(binding->>'subject_id')||':'||(binding->>'credential_id'),'UTF8')),'hex');
 existing:=coalesce(object.properties->'external_id_refs','[]'::jsonb);
 select jsonb_agg(value order by value) into expected from (select distinct value from jsonb_array_elements(existing||jsonb_build_array(ref))) x;
 if payload->>'type_name'<>'Consumer' or payload->'properties' is distinct from jsonb_build_object('external_id_refs',expected) then raise exception 'identity ref invalid' using errcode='23514';end if;
 -- Only the owner-defined current writer can create this per-transaction witness.
 -- Caller GUC flags are never consulted.
 insert into control.nexloop_consumer_identity_link_receipts values(owner.tenant_id,p_world,ref,owner.consumer_id,binding->>'subject_id',binding->>'credential_id',payload,txid_current());
 -- Existing EIOS edit function independently verifies HMAC, current Action and
 -- Object/Property proofs, permit/fence/lease, revisions and terminal outcome.
 outcome:=authz.nexloop_edit_object_action(p_digest,p_world,p_text,p_signature,p_payload);
 perform authz.nexloop_assert_action_authority(p_digest,p_world,a);
 perform authz.nexloop_browser_identity_snapshot(p_digest,p_world);
 return outcome;
end $$;
alter function authz.nexloop_link_browser_identity_action(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_link_browser_identity_action(text,text,text,text,text) from public;
grant execute on function authz.nexloop_link_browser_identity_action(text,text,text,text,text) to nexloop_api;

-- Keep profile validation and verify retained local identity provenance.
-- Consumer profile values never grant runtime/dispatch authority.
create function ontology.nexloop_validate_consumer_profile_identity() returns trigger
language plpgsql set search_path=pg_catalog as $$
declare field text; profile_value jsonb;
begin
 if new.type_name<>'Consumer' then return new;end if;
 if tg_op='UPDATE' and jsonb_array_length(coalesce(old.properties->'external_id_refs','[]'::jsonb))>0
  and not (new.properties ? 'external_id_refs') then
  raise exception 'consumer_identity_removal_requires_governance' using errcode='23514';
 end if;
 foreach field in array array['display_name','locale','lifecycle_status'] loop
  if new.properties ? field then
   profile_value:=new.properties->field;
   if jsonb_typeof(profile_value)<>'string' or char_length(new.properties->>field) not between 1 and 256 then
    raise exception 'consumer_profile_display_invalid' using errcode='23514';
   end if;
  end if;
 end loop;
 if new.properties ? 'timezone' then
  if jsonb_typeof(new.properties->'timezone')<>'string'
   or new.properties->>'timezone' ~ '(^/|\.\.|^posix/|^right/)'
   or not exists(select 1 from pg_catalog.pg_timezone_names where name=new.properties->>'timezone') then
   raise exception 'consumer_profile_timezone_invalid' using errcode='23514';
  end if;
 end if;
 foreach field in array array['external_id_refs','contact_preferences'] loop
  if new.properties ? field then
   profile_value:=new.properties->field;
   if jsonb_typeof(profile_value)<>'array' then raise exception 'consumer_profile_verified_refs_unavailable' using errcode='23514';end if;
   if field='contact_preferences' and jsonb_array_length(profile_value)<>0 then raise exception 'consumer_profile_verified_refs_unavailable' using errcode='23514';end if;
   if field='external_id_refs' and tg_op='UPDATE' and not (coalesce(old.properties->'external_id_refs','[]'::jsonb)<@profile_value) then raise exception 'consumer_identity_removal_requires_governance' using errcode='23514';end if;
   if field='external_id_refs' and jsonb_array_length(profile_value)<>(select count(distinct r) from jsonb_array_elements(profile_value) r) then raise exception 'consumer_identity_duplicate_ref' using errcode='23514';end if;
   if field='external_id_refs' and jsonb_array_length(profile_value)>0 then
    if tg_op='INSERT' or exists(select 1 from jsonb_array_elements(profile_value) ref where jsonb_typeof(ref)<>'string' or (ref#>>'{}')!~'^nexloop:browser-local:[a-f0-9]{64}$'
     or not exists(select 1 from control.nexloop_consumer_identity_link_receipts p where p.tenant_id=new.tenant_id and p.world=new.world and p.consumer_id=new.object_id and p.identity_ref=ref#>>'{}'
      and (coalesce(old.properties->'external_id_refs','[]'::jsonb)@>jsonb_build_array(ref) or p.recorded_txid=txid_current()))) then
     raise exception 'consumer_profile_verified_refs_unavailable' using errcode='23514';
    end if;
   end if;
  end if;
 end loop;
 return new;
end $$;
alter function ontology.nexloop_validate_consumer_profile_identity() owner to nexloop_owner;
revoke all on function ontology.nexloop_validate_consumer_profile_identity() from public;
-- Replace the profile guard by appending to the immutable bootstrap lineage.
-- The published0059 source/checksum remains untouched.
drop trigger nexloop_consumer_profile_guard on ontology.objects;
create trigger nexloop_consumer_profile_identity_guard before insert or update of properties on ontology.objects
 for each row execute function ontology.nexloop_validate_consumer_profile_identity();
