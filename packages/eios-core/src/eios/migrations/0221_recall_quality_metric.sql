-- Recall quality for the read-only metrics role (temporary number 0221; owner 2026-10-19, PR #14 review): the vector recall checks
-- of the stage-embedding sheet ran as nexloop_bootstrap, a superuser that is NOLOGIN after publication. runtime.nexloop_recall_quality
-- (authz wrapper granted to nexloop_metrics only) reports for one tenant, under its active profile: entries with and without a
-- vector per index kind, and over the published definition texts the nearest other definition by vector, counted when it is close
-- (cosine distance < 0.35) and when it is close without literal overlap (character trigram similarity < 0.1); the samples are
-- definition refs and the two numbers only, never a text. Nothing is written. Owner function under row security for the tenant.
-- As 0067 for the other extension functions: only the owner-run definers may call the trigram similarity used here.
grant execute on function extensions.similarity(text,text) to nexloop_owner;
create function runtime.nexloop_recall_quality(p_tenant text,p_limit integer) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);v_profile text;v_coverage jsonb;v_neighbours jsonb;v_samples jsonb;
begin
 if p_tenant is null or p_limit is null or p_limit not between 0 and 50 then raise exception 'recall quality input invalid' using errcode='22023';end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select active_profile_id into v_profile from ontology.nexloop_recall_settings where tenant_id=p_tenant;
 if v_profile is null then
  perform set_config('eios.tenant_id',coalesce(prior,''),true);
  return jsonb_build_object('active_profile',null);
 end if;
 select coalesce(jsonb_object_agg(kind,jsonb_build_object('entries',entries,'with_vector',with_vector,'without_vector',entries-with_vector)),'{}'::jsonb)
  into v_coverage from (select r.index_kind kind,count(*) entries,count(e.entry_id) with_vector from ontology.nexloop_recall_entries r
   left join ontology.nexloop_recall_embeddings e on e.entry_id=r.entry_id and e.profile_id=v_profile
   where r.tenant_id=p_tenant and r.field<>'strong_id' group by 1) c;
 with d as (select r.entry_id,r.ref,r.body,e.embedding from ontology.nexloop_recall_entries r
   join ontology.nexloop_recall_embeddings e on e.entry_id=r.entry_id and e.profile_id=v_profile
   where r.tenant_id=p_tenant and r.index_kind='definition'),
 n as (select a.ref a_ref,b.ref b_ref,extensions.cosine_distance(a.embedding,b.embedding) dist,extensions.similarity(a.body,b.body) sim from d a
   cross join lateral (select * from d b where b.ref<>a.ref order by extensions.cosine_distance(a.embedding,b.embedding) limit 1) b)
 select jsonb_build_object('definition_texts',count(*),'close',count(*) filter (where dist<0.35),
          'close_without_literal_overlap',count(*) filter (where dist<0.35 and sim<0.1)),
        coalesce((select jsonb_agg(x order by (x->>'vector_distance')::numeric) from (select jsonb_build_object('ref',a_ref,'neighbour_ref',b_ref,
          'vector_distance',round(dist::numeric,3),'trigram_similarity',round(sim::numeric,3)) x from n where dist<0.35 and sim<0.1
          order by dist limit p_limit) s),'[]'::jsonb)
  into v_neighbours,v_samples from n;
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return jsonb_build_object('active_profile',v_profile,'coverage',v_coverage,'neighbours',v_neighbours,'samples',v_samples);
end $$;
alter function runtime.nexloop_recall_quality(text,integer) owner to nexloop_owner;
revoke all on function runtime.nexloop_recall_quality(text,integer) from public;
create function authz.nexloop_recall_quality(p_tenant text,p_limit integer) returns jsonb
 language sql stable security definer set search_path=pg_catalog,pg_temp as $$ select runtime.nexloop_recall_quality(p_tenant,p_limit) $$;
alter function authz.nexloop_recall_quality(text,integer) owner to nexloop_owner;
revoke all on function authz.nexloop_recall_quality(text,integer) from public;
grant execute on function authz.nexloop_recall_quality(text,integer) to nexloop_metrics;
