-- Owner 2026-10-19: read-only checks of vector recall on stage after the embedding profile is active (stage-embedding sheet,
-- appendix). Run in a read-only session (PGOPTIONS="-c default_transaction_read_only=on") as the bootstrap role; set :T to the
-- tenant id. Definitions only in the text output (schema names and descriptions, no customer data); instances are counted.
-- Q1 coverage: entries of the active profile with and without a vector.
select r.index_kind,count(*) entries,count(e.entry_id) with_vector,count(*)-count(e.entry_id) without_vector
  from ontology.nexloop_recall_entries r
  join ontology.nexloop_recall_settings s on s.tenant_id=r.tenant_id
  left join ontology.nexloop_recall_embeddings e on e.entry_id=r.entry_id and e.profile_id=s.active_profile_id
 where r.tenant_id=:T and r.field<>'strong_id' group by 1 order by 1;
-- Q2 semantic neighbours without literal overlap: for each definition text, its nearest other definition text (another ref)
-- by vector, kept only when they share almost no character trigrams. Pairs here are what FTS and n-gram recall could not find.
with d as (
  select r.entry_id,r.ref,r.gate_type,r.field,r.body,e.embedding from ontology.nexloop_recall_entries r
  join ontology.nexloop_recall_settings s on s.tenant_id=r.tenant_id
  join ontology.nexloop_recall_embeddings e on e.entry_id=r.entry_id and e.profile_id=s.active_profile_id
  where r.tenant_id=:T and r.index_kind='definition' and r.field in ('name','display_name','description','vocabulary_value','alias'))
select a.ref,a.body query_text,b.ref neighbour_ref,b.body neighbour_text,
       round((extensions.cosine_distance(a.embedding,b.embedding))::numeric,3) vector_distance,
       round(extensions.similarity(a.body,b.body)::numeric,3) trigram_similarity
  from d a cross join lateral (select * from d b where b.ref<>a.ref order by extensions.cosine_distance(a.embedding,b.embedding) limit 1) b
 where extensions.similarity(a.body,b.body)<0.1
 order by vector_distance limit 20;
-- Q3 the same neighbour pairs, ranked: how many definition texts have a nearest neighbour within distance 0.35 that literal
-- matching would miss (trigram similarity < 0.1), against all definition texts.
with d as (
  select r.entry_id,r.ref,r.body,e.embedding from ontology.nexloop_recall_entries r
  join ontology.nexloop_recall_settings s on s.tenant_id=r.tenant_id
  join ontology.nexloop_recall_embeddings e on e.entry_id=r.entry_id and e.profile_id=s.active_profile_id
  where r.tenant_id=:T and r.index_kind='definition'),
n as (select a.body a_body,b.body b_body,extensions.cosine_distance(a.embedding,b.embedding) dist from d a
  cross join lateral (select * from d b where b.ref<>a.ref order by extensions.cosine_distance(a.embedding,b.embedding) limit 1) b)
select count(*) definition_texts,count(*) filter (where dist<0.35) close_neighbour,
       count(*) filter (where dist<0.35 and extensions.similarity(a_body,b_body)<0.1) close_without_literal_overlap from n;
-- Q4 a fixed probe pair (edit the two texts to two definition names of this tenant that mean the same thing in other words,
-- e.g. a property "联系电话" and a vocabulary value "手机号"): their vector distance and trigram similarity.
select a.body,b.body,round(extensions.cosine_distance(ea.embedding,eb.embedding)::numeric,3) vector_distance,
       round(extensions.similarity(a.body,b.body)::numeric,3) trigram_similarity
  from ontology.nexloop_recall_entries a join ontology.nexloop_recall_entries b on b.tenant_id=a.tenant_id
  join ontology.nexloop_recall_settings s on s.tenant_id=a.tenant_id
  join ontology.nexloop_recall_embeddings ea on ea.entry_id=a.entry_id and ea.profile_id=s.active_profile_id
  join ontology.nexloop_recall_embeddings eb on eb.entry_id=b.entry_id and eb.profile_id=s.active_profile_id
 where a.tenant_id=:T and a.index_kind='definition' and b.index_kind='definition' and a.body='联系电话' and b.body='手机号';
