    begin transaction read only;
    set local role nexloop_owner;
    set local row_security = on;
    select set_config('eios.tenant_id', :'tenant_id', true);
    do $guard$
    begin
      if current_user <> 'nexloop_owner'
         or coalesce(current_setting('eios.tenant_id', true), '') = ''
         or exists(select 1 from pg_roles where rolname=current_user and (rolsuper or rolbypassrls))
         or not exists(select 1 from pg_class where oid='control.nexloop_effect_action_admissions'::regclass
                       and relowner=current_user::regrole and relrowsecurity and relforcerowsecurity) then
        raise exception 'G1 owner / tenant / FORCE RLS verification failed';
      end if;
      if (select count(*) from control.nexloop_effect_actions) = 0 then
        raise exception 'G1 positive control is empty';
      end if;
      if (select count(*) from control.nexloop_effect_action_admissions where retired_at is null) <> 0 then
        raise exception 'G1 active admissions present: stop before M2.2';
      end if;
    end $guard$;
    select current_user, current_setting('eios.tenant_id') as verified_tenant,
           (select count(*) from control.nexloop_effect_actions) as positive_control,
           (select count(*) from control.nexloop_effect_action_admissions where retired_at is null) as active_admissions;
    rollback;
    