-- NX-046 fix: rejected_at and cooldown_until were derived from two separate
-- clock_timestamp() calls in one INSERT (0074), so their difference was not the
-- configured cooldown exactly (visible on slower hosts). One instant now feeds
-- both, and the table states the relation as a constraint. 0074 is unchanged.
alter table ontology.nexloop_candidate_rejections
 add column cooldown_seconds integer check(cooldown_seconds between 0 and 31536000),
 add constraint nexloop_candidate_rejections_single_instant
  check(cooldown_seconds is null or cooldown_until=rejected_at+make_interval(secs=>cooldown_seconds));

create or replace function ontology.nexloop_candidate_rejected() returns trigger
 language plpgsql security definer set search_path=pg_catalog set row_security=on as $$
declare seconds integer;version text;v_now timestamptz:=clock_timestamp();
begin
 select reject_cooldown_seconds,config_version into seconds,version from ontology.nexloop_merge_configurations where tenant_id=new.tenant_id and active;
 seconds:=coalesce(seconds,2592000);
 insert into ontology.nexloop_candidate_rejections(tenant_id,world,candidate_id,kind,dedupe_key,rejected_at,cooldown_until,cooldown_seconds,config_version)
  values(new.tenant_id,new.world,new.candidate_id,new.kind,new.dedupe_key,v_now,v_now+make_interval(secs=>seconds),seconds,version);
 -- Dependent Claims stay as original evidence only (never formal, never Context properties).
 update ontology.nexloop_claims set resolution_state='rejected_definition' where tenant_id=new.tenant_id and world=new.world
  and 'claim:'||claim_id=any(new.dependent_claims) and resolution_state='awaiting_definition';
 return new;
end $$;
alter function ontology.nexloop_candidate_rejected() owner to nexloop_owner;
revoke all on function ontology.nexloop_candidate_rejected() from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_identity,nexloop_configurator;
