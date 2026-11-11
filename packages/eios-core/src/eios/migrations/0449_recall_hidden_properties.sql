-- Recall of hidden properties (L3 P5 Q6 933f8f82; design docs/planning-drafts/recall-hidden-properties-20261111.md, L6 conditional
-- PASS 2026-11-11). Temporary number; the dispatcher renumbers. Published migrations are not edited.
--
-- 1. ontology.nexloop_recall_definition_rows (0067 body): a property whose visibility is "hidden" in the version read (the NX-040 F3
--    hide, 0254 - the rollback of a published optional property) yields no row: no name, display name, description or vocabulary
--    value. In the semantic layer hidden means retired, judged on the latest version only; authorization, object reads and writes,
--    Action bindings and Context projections do not read visibility and are unchanged. Every caller follows: definition_texts and
--    index_definition (0067) write no hidden rows, index_alias (0067) refuses an alias to a hidden property, nexloop_recall_scope's
--    alias branch (0379) drops aliases, glossary glosses and release aliases to it at query time, semantic_release_reindex (0249) and
--    glossary_reflow (0378) skip it, and review_decide (0082) refuses a merge_into a hidden property. Signature, owner and grants are
--    unchanged; search_path gains pg_temp (as 0379).
-- 2. Backfill: the type:<T> definition rows already written at a type's latest version for a property that is hidden there are
--    deleted (their vectors cascade); nexloop_recall_stale_definitions would never reflow such a type. Alias, glossary and release
--    alias rows are kept: the scope already ignores them, and a version that shows the property again revives them.

create or replace function ontology.nexloop_recall_definition_rows(p_tenant text,p_type text,p_version integer)
 returns table(ref text,target_kind text,gate_type text,gate_property text,field text,property_group text,body text)
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare d jsonb;p jsonb;g text;v text;tref text;pref text;
begin
 select definition into d from ontology.object_type_versions where tenant_id=p_tenant and type_name=p_type and version=p_version;
 if not found then raise exception 'recall definition unavailable' using errcode='42501';end if;
 tref:='eios:object_type:'||p_type;
 return query select tref,'object_type',p_type,null::text,x.f,null::text,x.b from (values
  ('name',p_type),('display_name',nullif(btrim(d->>'display_name'),'')),('display_name',nullif(btrim(d->>'plural_display_name'),'')),
  ('description',nullif(btrim(d->>'description'),''))) x(f,b) where x.b is not null;
 for p in select value from jsonb_array_elements(coalesce(d->'properties','[]'::jsonb)) loop
  continue when p->>'visibility' is not distinct from 'hidden';
  pref:='eios:property:'||p_type||'/'||(p->>'property_name');
  select pg.value->>'group_name' into g from jsonb_array_elements(coalesce(d->'property_groups','[]'::jsonb)) pg
   where pg.value->'property_names' ? (p->>'property_name') order by pg.value->>'group_name' limit 1;
  return query select pref,'property',p_type,p->>'property_name',x.f,g,x.b from (values
   ('name',p->>'property_name'),('display_name',nullif(btrim(p->>'display_name'),'')),('description',nullif(btrim(p->>'description'),''))) x(f,b)
   where x.b is not null;
  for v in select coalesce(e.value#>>'{}','') from jsonb_array_elements(coalesce(p->'type_descriptor'->'enum','[]'::jsonb)) e loop
   if btrim(v)<>'' then
    return query select 'nexloop:vocabulary:'||p_type||'/'||(p->>'property_name')||'/'||ontology.nexloop_recall_ref_part(v),
     'vocabulary_value',p_type,p->>'property_name','vocabulary_value',g,v;
   end if;
  end loop;
 end loop;
end $$;

do $backfill$
declare t text;
begin
 for t in select tenant_id from control.nexloop_tenants order by tenant_id loop
  perform set_config('eios.tenant_id',t,true);
  delete from ontology.nexloop_recall_entries e
   where e.tenant_id=t and e.index_kind='definition' and e.source_key like 'type:%' and e.target_kind in ('property','vocabulary_value')
    and e.source_revision=ontology.nexloop_recall_latest_version(t,e.gate_type)
    and not exists(select 1 from ontology.nexloop_recall_definition_rows(t,e.gate_type,e.source_revision::integer) d
     where d.ref=e.ref and d.field=e.field and d.body=e.body);
 end loop;
 perform set_config('eios.tenant_id','',true);
end $backfill$;
