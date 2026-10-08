-- NX-018 fresh Consumer profile guards; append-only independent bootstrap.
-- Consumer profile values never grant runtime/dispatch authority.
create function ontology.nexloop_validate_consumer_profile() returns trigger
language plpgsql set search_path=pg_catalog as $$
declare field text; value jsonb;
begin
 if new.type_name<>'Consumer' then return new;end if;
 foreach field in array array['display_name','locale','lifecycle_status'] loop
  if new.properties ? field then
   value:=new.properties->field;
   if jsonb_typeof(value)<>'string' or char_length(new.properties->>field) not between 1 and 256 then
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
   value:=new.properties->field;
   if jsonb_typeof(value)<>'array' then raise exception 'consumer_profile_verified_refs_unavailable' using errcode='23514';end if;
   if jsonb_array_length(value)<>0 then raise exception 'consumer_profile_verified_refs_unavailable' using errcode='23514';end if;
  end if;
 end loop;
 return new;
end $$;
alter function ontology.nexloop_validate_consumer_profile() owner to nexloop_owner;
revoke all on function ontology.nexloop_validate_consumer_profile() from public;
create trigger nexloop_consumer_profile_guard before insert or update of properties on ontology.objects
 for each row execute function ontology.nexloop_validate_consumer_profile();
