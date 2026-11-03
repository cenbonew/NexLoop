-- S-DUP-2 (docs/implementation/S-DUP-extraction-dedupe-design.md §11): when a correction supersedes a Claim, its still-active duplicates
-- (same statement key, 0412 ontology.nexloop_claim_statement_key) are superseded with it. The migration number lives only in the file
-- name and the catalog; nothing below repeats it.
-- 1. ontology.nexloop_claim_supersessions: one row per duplicate superseded this way (the duplicate, the corrected Claim it follows, the
--    correcting Claim, the cause statement_duplicate). Ids only, no words; append-only.
-- 2. ontology.nexloop_supersede_statement_duplicates(tenant, world, claim, correction): supersedes the active Claims with the corrected
--    Claim's statement key (same Message span, kind, polarity, modality, condition presence, normalized value) through the usual
--    resolution_state update (the 0139 trigger stamps superseded_at) and records each. Returns the count.
-- 3. Triggers, fired at once (a read later in the same transaction - the matcher's next statement or candidate, a work feed touch, a
--    context pack - already sees the duplicates superseded; the real paths link the correction before superseding): after update of
--    resolution_state, a Claim that became superseded and has a correction (a Claim whose corrects_claim_id is it, or an applied proposal
--    that supersedes it) supersedes its duplicates; after update of corrects_claim_id, a correction linked to an already superseded Claim
--    does the same. A duplicate superseded here has no correction of its own, so nothing cascades further. Every path that supersedes a
--    corrected Claim (matcher apply, review: both authz.nexloop_record_claim_match) is covered. 0111 then raises the commitments whose
--    source Claim is a superseded duplicate (source_superseded), as for any superseded Claim.
-- 4. authz.nexloop_claim_duplicate_supersede_backfill (eios:action:nexloop.claim.supersede_duplicates:1, protocol
--    nexloop-claim-duplicate-supersede-v1): the claim matcher service applies 2. to corrections that took effect before this migration.
--    Tenant from the credential, world from its binding, nexloop_domain_worker only. Skips Consumers with any erasure request. Repeatable
--    (a superseded duplicate is not active any more). verb dry_run writes nothing; both verbs return counts only, including the
--    commitments whose source Claim is one of those duplicates (0111 raises them as source_superseded).

create table ontology.nexloop_claim_supersessions (
 tenant_id text not null,world text not null,claim_id text not null,corrected_claim_id text not null,correction_claim_id text not null,
 cause text not null check(cause='statement_duplicate'),recorded_at timestamptz not null default clock_timestamp(),
 primary key(tenant_id,world,claim_id),
 foreign key(tenant_id,world,claim_id) references ontology.nexloop_claims,
 foreign key(tenant_id,world,corrected_claim_id) references ontology.nexloop_claims
);
alter table ontology.nexloop_claim_supersessions owner to nexloop_owner;
alter table ontology.nexloop_claim_supersessions enable row level security;
alter table ontology.nexloop_claim_supersessions force row level security;
create policy claim_supersession_tenant on ontology.nexloop_claim_supersessions to nexloop_owner
 using (tenant_id=current_setting('eios.tenant_id',true)) with check (tenant_id=current_setting('eios.tenant_id',true));
create trigger claim_supersessions_append_only before update or delete on ontology.nexloop_claim_supersessions for each row execute function control.nexloop_nx022_append_only();
revoke all on ontology.nexloop_claim_supersessions from public,nexloop_api,nexloop_domain_worker,nexloop_action_worker,nexloop_scheduler,nexloop_runtime,nexloop_identity,nexloop_configurator;

create function ontology.nexloop_supersede_statement_duplicates(p_tenant text,p_world text,p_claim text,p_correction text) returns integer
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);c ontology.nexloop_claims%rowtype;v_key text;v_count integer:=0;d text;
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select * into c from ontology.nexloop_claims where tenant_id=p_tenant and world=p_world and claim_id=p_claim;
 if found and c.source_message_id is not null then
  v_key:=ontology.nexloop_claim_statement_key(c.source_message_id,c.span_start,c.span_end,c.epistemic_kind,c.polarity,c.modality,c.condition_text,c.value);
  for d in select x.claim_id from ontology.nexloop_claims x where x.tenant_id=p_tenant and x.world=p_world and x.source_message_id=c.source_message_id
     and x.span_start=c.span_start and x.span_end=c.span_end and x.epistemic_kind=c.epistemic_kind and x.claim_id<>c.claim_id
     and x.resolution_state<>'superseded' and x.superseded_at is null
     and ontology.nexloop_claim_statement_key(x.source_message_id,x.span_start,x.span_end,x.epistemic_kind,x.polarity,x.modality,x.condition_text,x.value)=v_key
     order by x.claim_id for update loop
   update ontology.nexloop_claims set resolution_state='superseded' where tenant_id=p_tenant and world=p_world and claim_id=d;
   insert into ontology.nexloop_claim_supersessions(tenant_id,world,claim_id,corrected_claim_id,correction_claim_id,cause)
    values(p_tenant,p_world,d,c.claim_id,p_correction,'statement_duplicate') on conflict do nothing;
   v_count:=v_count+1;
  end loop;
 end if;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return v_count;
end $$;
alter function ontology.nexloop_supersede_statement_duplicates(text,text,text,text) owner to nexloop_owner;
revoke all on function ontology.nexloop_supersede_statement_duplicates(text,text,text,text) from public;

-- The correcting Claim of a superseded Claim, if a correction is what superseded it.
create function ontology.nexloop_claim_correction_of(p_tenant text,p_world text,p_claim text) returns text
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$
 select coalesce(
  (select c.claim_id from ontology.nexloop_claims c where c.tenant_id=p_tenant and c.world=p_world and c.corrects_claim_id=p_claim order by c.recorded_at,c.claim_id limit 1),
  (select p.claim_id from ontology.nexloop_mutation_proposals p where p.tenant_id=p_tenant and p.world=p_world and p.supersedes_claim=p_claim and p.status='applied'
    order by p.updated_at,p.proposal_id limit 1))
$$;
alter function ontology.nexloop_claim_correction_of(text,text,text) owner to nexloop_owner;
revoke all on function ontology.nexloop_claim_correction_of(text,text,text) from public;

create function ontology.nexloop_claim_on_superseded_duplicates() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v_correction text;
begin
 if new.resolution_state='superseded' and old.resolution_state is distinct from 'superseded' and new.source_message_id is not null then
  perform set_config('eios.tenant_id',new.tenant_id,true);
  v_correction:=ontology.nexloop_claim_correction_of(new.tenant_id,new.world,new.claim_id);
  if v_correction is not null then perform ontology.nexloop_supersede_statement_duplicates(new.tenant_id,new.world,new.claim_id,v_correction);end if;
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
 end if;
 return null;
end $$;
alter function ontology.nexloop_claim_on_superseded_duplicates() owner to nexloop_owner;
revoke all on function ontology.nexloop_claim_on_superseded_duplicates() from public;
create trigger claim_superseded_duplicates after update of resolution_state on ontology.nexloop_claims
 for each row execute function ontology.nexloop_claim_on_superseded_duplicates();

create function ontology.nexloop_claim_on_correction_linked() returns trigger
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);
begin
 if new.corrects_claim_id is not null and old.corrects_claim_id is distinct from new.corrects_claim_id then
  perform set_config('eios.tenant_id',new.tenant_id,true);
  if exists(select 1 from ontology.nexloop_claims x where x.tenant_id=new.tenant_id and x.world=new.world and x.claim_id=new.corrects_claim_id
     and x.resolution_state='superseded') then
   perform ontology.nexloop_supersede_statement_duplicates(new.tenant_id,new.world,new.corrects_claim_id,new.claim_id);
  end if;
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
 end if;
 return null;
end $$;
alter function ontology.nexloop_claim_on_correction_linked() owner to nexloop_owner;
revoke all on function ontology.nexloop_claim_on_correction_linked() from public;
create trigger claim_correction_linked after update of corrects_claim_id on ontology.nexloop_claims
 for each row execute function ontology.nexloop_claim_on_correction_linked();

create function authz.nexloop_claim_duplicate_supersede_backfill(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=authz.nexloop_plan_port_tenant(p_digest,p_world,p_text,p_signature,p_payload,'nexloop-claim-duplicate-supersede-v1',
  'eios:action:nexloop.claim.supersede_duplicates:1',array['nexloop_domain_worker']);
 c jsonb:=p_payload::jsonb;apply boolean;r record;v_correction text;v_key text;v_found integer;
 n jsonb:=jsonb_build_object('corrected',0,'duplicates',0,'superseded',0,'skipped_erasure',0,'commitments',0);v_dups text[];
begin
 if jsonb_typeof(c) is distinct from 'object' or (select count(*) from jsonb_object_keys(c))<>1 or c->>'verb' not in ('dry_run','apply') then
  raise exception 'claim duplicate supersede invalid' using errcode='22023';end if;
 apply:=c->>'verb'='apply';
 for r in select x.* from ontology.nexloop_claims x where x.tenant_id=t and x.world=p_world and x.resolution_state='superseded' and x.source_message_id is not null
   order by x.claim_id loop
  v_correction:=ontology.nexloop_claim_correction_of(t,p_world,r.claim_id);
  if v_correction is null then continue;end if;
  v_key:=ontology.nexloop_claim_statement_key(r.source_message_id,r.span_start,r.span_end,r.epistemic_kind,r.polarity,r.modality,r.condition_text,r.value);
  select count(*),coalesce(array_agg(x.claim_id),'{}') into v_found,v_dups from ontology.nexloop_claims x where x.tenant_id=t and x.world=p_world and x.source_message_id=r.source_message_id
    and x.span_start=r.span_start and x.span_end=r.span_end and x.epistemic_kind=r.epistemic_kind and x.claim_id<>r.claim_id
    and x.resolution_state<>'superseded' and x.superseded_at is null
    and ontology.nexloop_claim_statement_key(x.source_message_id,x.span_start,x.span_end,x.epistemic_kind,x.polarity,x.modality,x.condition_text,x.value)=v_key;
  if v_found=0 then continue;end if;
  n:=jsonb_set(n,'{corrected}',to_jsonb((n->>'corrected')::integer+1));
  if exists(select 1 from control.nexloop_consumer_erasures e where e.tenant_id=t and e.world=p_world and e.consumer_id=r.consumer_id) then
   n:=jsonb_set(n,'{skipped_erasure}',to_jsonb((n->>'skipped_erasure')::integer+v_found));continue;end if;
  n:=jsonb_set(n,'{duplicates}',to_jsonb((n->>'duplicates')::integer+v_found));
  n:=jsonb_set(n,'{commitments}',to_jsonb((n->>'commitments')::integer+(select count(*) from runtime.nexloop_commitments m
    where m.tenant_id=t and m.world=p_world and m.claim_id=any(v_dups))::integer));
  if apply then
   n:=jsonb_set(n,'{superseded}',to_jsonb((n->>'superseded')::integer+ontology.nexloop_supersede_statement_duplicates(t,p_world,r.claim_id,v_correction)));
  end if;
 end loop;
 return jsonb_build_object('verb',c->>'verb','counts',n);
end $$;
alter function authz.nexloop_claim_duplicate_supersede_backfill(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_claim_duplicate_supersede_backfill(text,text,text,text,text) from public;
grant execute on function authz.nexloop_claim_duplicate_supersede_backfill(text,text,text,text,text) to nexloop_domain_worker;
