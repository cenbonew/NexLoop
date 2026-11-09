-- NX-062 (L6 records on 77728430..35c85613): the machine intent audit sets the tenant itself instead of relying on its caller, and the
-- object note member-erasure trigger restores the session's tenant afterwards. Bodies otherwise unchanged (the copy guard registers both
-- changes). The migration number is temporary; it lives only in the file name and the catalog.
create or replace function runtime.nexloop_machine_intent_audit(i runtime.nexloop_machine_intents,p_principal text,p_event text) returns void
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);
begin
 perform set_config('eios.tenant_id',i.tenant_id,true);
 -- Ids, codes and the key prefix only: never parameters or text.
 insert into runtime.audit_events(event_id,action,tenant_id,actor_id,trace_id,request_id,payload,created_at)
  values('machine-intent:'||gen_random_uuid(),'machine.intent.'||p_event,i.tenant_id,p_principal,'machine',i.intent_id,
   jsonb_build_object('client_id',i.client_id,'key_prefix',i.key_prefix,'intent_id',i.intent_id,'capability',i.capability,'world',i.world,
    'phase',i.phase,'result_code',i.result_code,'outcome_id',i.action_outcome_id,'approval_id',i.approval_id),clock_timestamp());
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
end $$;
create or replace function runtime.nexloop_object_note_member_erasure() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare p text;v text;prior text:=current_setting('eios.tenant_id',true);
begin
 perform set_config('eios.tenant_id',new.tenant_id,true);
 perform set_config('nexloop.object_note_maintenance','on',true);
 foreach p in array new.principals loop
  v:='pseudonym:'||encode(sha256(convert_to('object-note:'||p,'UTF8')),'hex');
  update runtime.nexloop_object_notes set author_principal_id=v,author_subject_id=v,text='[已擦除]',erased=true
   where tenant_id=new.tenant_id and author_kind='human' and author_principal_id=p;
 end loop;
 perform set_config('nexloop.object_note_maintenance','',true);
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return null;
end $$;
