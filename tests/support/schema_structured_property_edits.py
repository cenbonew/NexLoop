"""Claim → Consumer phase two (migration schema_structured_property): the exact edits it makes to the 0252 change check and the 0257
publication gates, for the copy guard (old, new)."""
EDITS = {
 "runtime.nexloop_schema_change_valid": [
  [
   """  when 'add_optional_property' then (select array_agg(k order by k) from jsonb_object_keys(p_change) k)=array['property_name','value_type']
   and p_change->>'property_name'~'^[a-z][a-z0-9_]{0,63}$' and p_change->>'value_type' in ('string','integer','number','boolean','datetime')""",
   """  when 'add_optional_property' then runtime.nexloop_schema_optional_property_valid(p_change)"""
  ]
 ],
 "ontology.nexloop_schema_publication_gates": [
  [
   """  if r.change->>'value_type' not in ('string','integer','number','boolean','datetime') then f:=f||'value_type_not_allowed'::text;end if;""",
   """  if not runtime.nexloop_schema_property_type_allowed(r.change) then f:=f||'value_type_not_allowed'::text;end if;"""
  ],
  [
   """   'description','','display_name','','render_hint','plain','visibility','normal','type_descriptor',null);""",
   """   'description',coalesce(r.change->>'description',''),'display_name',coalesce(r.change->>'display_name',''),'render_hint','plain','visibility','normal',
   'type_descriptor',runtime.nexloop_structured_descriptor_expand(r.change->'type_descriptor'));"""
  ]
 ]
}
