-- I26 round 3 (L2 review side-session-notes/I26-review-l2.md, relayed by the dispatcher 2026-10-30). Temporary number; the dispatcher
-- renumbers (adjacent to the two I26 migrations before it). Published migrations are not edited.
--
-- 1. ontology.nexloop_recall_insert and ontology.nexloop_recall_scope (0067 bodies) read the tokenizer version from
--    ontology.nexloop_recall_tokenizer() instead of a literal: the version the index writes, the version recall reads and the stale
--    check are one constant.
-- 2. control.nexloop_recall_terms (I26-a body): a property gloss shorter than three characters ("状态", "名称") is no term. Such a
--    gloss maps to the same property of every type and would fan out into equal-score candidates; it is still searched when the
--    statement itself is (no term found). Gloss rows stay lexical only: they carry no vector (I27).

create or replace function ontology.nexloop_recall_insert(p_tenant text,p_world text,p_kind text,p_source text,p_revision bigint,p_ref text,p_target text,
  p_gate_type text,p_gate_property text,p_object text,p_field text,p_strong text,p_group text,p_body text) returns bigint
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare id bigint;
begin
 insert into ontology.nexloop_recall_entries(tenant_id,world,index_kind,source_key,source_revision,ref,target_kind,gate_type,gate_property,object_id,
   field,strong_key,property_group,body,tokenizer_version,tsv,ngrams)
  values(p_tenant,p_world,p_kind,p_source,p_revision,p_ref,p_target,p_gate_type,p_gate_property,p_object,p_field,p_strong,p_group,p_body,
   ontology.nexloop_recall_tokenizer(),ontology.nexloop_recall_tsv(p_body),ontology.nexloop_recall_ngrams(p_body))
  returning entry_id into id;
 return id;
end $$;

create or replace function ontology.nexloop_recall_scope(p_tenant text,p_world text,p_kind text,p_types text[],p_props text[],p_scope text[])
 returns setof bigint language sql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
 select e.entry_id from ontology.nexloop_recall_entries e
 where e.tenant_id=p_tenant and e.index_kind=p_kind and e.tokenizer_version=ontology.nexloop_recall_tokenizer()
  and (case when p_kind='instance' then e.world=p_world else (e.world is null or e.world=p_world) end)
  and e.gate_type=any(p_types) and (e.gate_property is null or e.gate_type||'/'||e.gate_property=any(p_props))
  and (p_scope is null or e.gate_type=any(p_scope))
  and (case when p_kind='instance' then exists(select 1 from ontology.objects o where o.tenant_id=e.tenant_id and o.world=e.world
        and o.type_name=e.gate_type and o.object_id=e.object_id and o.nexloop_revision=e.source_revision)
       -- An alias stays live only while its canonical ref exists in the latest definition.
       when e.field='alias' then case when ontology.nexloop_recall_latest_version(p_tenant,e.gate_type) is null then false
        else exists(select 1 from ontology.nexloop_recall_definition_rows(p_tenant,e.gate_type,ontology.nexloop_recall_latest_version(p_tenant,e.gate_type)) d where d.ref=e.ref) end
       else e.source_revision=ontology.nexloop_recall_latest_version(p_tenant,e.gate_type) end)
$$;

create or replace function control.nexloop_recall_terms(p_digest text,p_world text,p_request jsonb) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare t text:=ontology.nexloop_recall_tenant(p_digest,p_world);q jsonb:=p_request;txt text;words text;flat text;types text[];props text[];res jsonb;
begin
 txt:=q->>'text';
 if txt is null or char_length(txt) not between 1 and 4000 or jsonb_typeof(q->'readable_types')<>'array' or jsonb_typeof(q->'readable_properties')<>'array' then
  raise exception 'recall terms invalid' using errcode='22023';end if;
 types:=array(select jsonb_array_elements_text(q->'readable_types'));props:=array(select jsonb_array_elements_text(q->'readable_properties'));
 words:=ontology.nexloop_recall_term_words(txt);flat:=regexp_replace(lower(txt),'\s+','','g');
 select coalesce(jsonb_agg(b order by char_length(b) desc,b),'[]'::jsonb) into res from (
  select distinct btrim(e.body) b from ontology.nexloop_recall_entries e
   join ontology.nexloop_recall_scope(t,p_world,'definition',types,props,null) s(entry_id) using(entry_id)
  where e.field in ('name','display_name','alias','vocabulary_value') and char_length(btrim(e.body)) between 2 and 64
   and not (e.gloss_source is not null and e.target_kind='property' and char_length(btrim(e.body))<3)
   and case when e.body ~ '[㐀-䶿一-鿿豈-﫿]' then position(regexp_replace(lower(e.body),'\s+','','g') in flat)>0
            else ontology.nexloop_recall_term_words(e.body)<>'  ' and position(ontology.nexloop_recall_term_words(e.body) in words)>0 end
  order by 1 limit 200) x;
 return coalesce((select jsonb_agg(v) from (select v from jsonb_array_elements(res) v limit 16) y),'[]'::jsonb);
end $$;
