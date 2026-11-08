-- data_health recall: glossary aliases are not missing embeddings (stage 2026-11-08: data_health_recall_missing_embeddings fired on
-- 161 rows, all 0378 glossary alias rows). Temporary number; the dispatcher renumbers. 0378's alias rows (gloss_source manual or
-- machine, source_key 'glossary:<kind>/<identifier>') are lexical only by design: the SQL reflow rewrites them on every glossary
-- write and no path embeds them; vector recall reaches the definition entry each alias names. runtime.nexloop_data_health is renamed
-- once and wrapped: the recall block's entries, missing_embeddings, other_profile_vectors, coverage_ratio, reasons and status are
-- computed as in 0245 without those rows, and gloss_aliases counts them. A definition or instance entry without a vector of the
-- active profile is still missing; every other block and field is unchanged.
alter function runtime.nexloop_data_health(text,text) rename to nexloop_data_health_before_gloss_not_missing;
revoke all on function runtime.nexloop_data_health_before_gloss_not_missing(text,text) from public;
create function runtime.nexloop_data_health(p_tenant text,p_world text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare h jsonb:=runtime.nexloop_data_health_before_gloss_not_missing(p_tenant,p_world);r jsonb;v_profile text;v_entries bigint;v_missing bigint;
 v_other bigint;v_gloss bigint;reasons text[]:=array[]::text[];
begin
 perform set_config('eios.tenant_id',p_tenant,true);
 select active_profile_id into v_profile from ontology.nexloop_recall_settings where tenant_id=p_tenant;
 select count(*) filter (where e.gloss_source is null),
        count(*) filter (where e.gloss_source is null and (v_profile is null or not exists(select 1 from ontology.nexloop_recall_embeddings m
          where m.entry_id=e.entry_id and m.profile_id=v_profile))),
        count(*) filter (where e.gloss_source is not null)
  into v_entries,v_missing,v_gloss from ontology.nexloop_recall_entries e where e.tenant_id=p_tenant and (e.world is null or e.world=p_world);
 select count(*) into v_other from ontology.nexloop_recall_embeddings m join ontology.nexloop_recall_entries e on e.entry_id=m.entry_id
  where v_profile is not null and m.tenant_id=p_tenant and (e.world is null or e.world=p_world) and e.gloss_source is null and m.profile_id<>v_profile;
 if v_entries>0 and v_profile is null then reasons:=reasons||'no_active_profile'::text;end if;
 if v_missing>0 then reasons:=reasons||'missing_embeddings'::text;end if;
 if v_other>0 then reasons:=reasons||'profile_drift'::text;end if;
 r:=coalesce(h->'recall','{}'::jsonb)||jsonb_build_object('entries',v_entries,'missing_embeddings',v_missing,'other_profile_vectors',v_other,
  'gloss_aliases',v_gloss,'coverage_ratio',case when v_entries>0 then round((v_entries-v_missing)::numeric/v_entries,4) end,
  'status',case when cardinality(reasons)=0 then 'ok' else 'attention' end,'reasons',to_jsonb(reasons));
 return jsonb_set(h,'{recall}',r);
end $$;
alter function runtime.nexloop_data_health(text,text) owner to nexloop_owner;
revoke all on function runtime.nexloop_data_health(text,text) from public;
