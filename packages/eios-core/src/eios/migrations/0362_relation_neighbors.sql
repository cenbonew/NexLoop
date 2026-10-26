-- NX-054 P4b (docs/design/relation-read-p4.md §1; relation-types-design.md §6): the world model's neighbourhood reads relations for
-- the links the relation catalog covers. authz.nexloop_workbench_read is renamed and wrapped (the 0246 / 0345 pattern; every other
-- verb, and the signed-read verification of object_neighbors, unchanged in the wrapped function). For object_neighbors the wrapper
-- calls it and then:
--   - drops a reference-property group ('property:<p>') when a published relation type derives from that property - outgoing from the
--     centre (the centre's type is the source type), or incoming (the neighbour's type is the source type and the centre's the
--     target type): that link is the relation group now, with its name, direction and cardinality, instead of a guess from a value
--     that looks like an id;
--   - only when that relation group exists and holds at least the property group's total (L6 I-1 b); otherwise, and for every
--     link the first batch has no type for (id arrays, RelationshipAssessment source/target, principals), the property group stays.
-- A relation group checks each neighbour's READ, not the READ of the property the relation was derived from (as since 0226 for
-- relation groups); after P4b a covered link shows through its relation group only (L6 S-3, recorded in relation-read-p4.md §P4b).
-- The relation groups do not carry their cardinality yet: the published contract workbench-object-neighbors has no such field
-- (additionalProperties false); adding it is a contract change of its own.
-- The 25 a group, the totals and the per-object READ checks of the workbench (ADR-025/035) are unchanged. Before P3's backfill
-- balances, a covered link without its relation would be missing here: P4b ships after P4a's gap check reads 0 (design §1).
-- Temporary number; the dispatcher renumbers.

alter function authz.nexloop_workbench_read(text,text,text,text,text) rename to nexloop_workbench_read_before_relation_neighbors;
revoke all on function authz.nexloop_workbench_read_before_relation_neighbors(text,text,text,text,text) from public,nexloop_api;

create function authz.nexloop_workbench_neighbors_by_relation(p_tenant text,p_type text,p_value jsonb) returns jsonb
 language plpgsql stable security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare prior text:=current_setting('eios.tenant_id',true);out jsonb;
begin
 if p_value is null then return null;end if;
 perform set_config('eios.tenant_id',p_tenant,true);
 select coalesce(jsonb_agg(g order by ord),'[]'::jsonb) into out
 from jsonb_array_elements(p_value->'groups') with ordinality x(g,ord)
 where not (g->>'via' like 'property:%' and exists(select 1 from control.nexloop_relation_derivations m where m.tenant_id=p_tenant
    and m.source_property=substring(g->>'via' from 10)
    and ((g->>'direction'='outgoing' and g->>'property_holder'='centre' and m.source_type=p_type and m.target_type=g->>'type_name')
      or (g->>'direction'='incoming' and g->>'property_holder'='neighbour' and m.source_type=g->>'type_name' and m.target_type=p_type))
    -- L6 I-1 (b): only when the relation group of that type, direction and neighbour type is there and holds at least as many
    -- neighbours; otherwise links without their relation (not backfilled, an array element, a value derivation does not take)
    -- would vanish silently, so the property group stays.
    and exists(select 1 from jsonb_array_elements(p_value->'groups') r where r->>'via'=m.relation_name and r->>'direction'=g->>'direction'
      and r->>'type_name'=g->>'type_name' and (r->>'total')::bigint>=(g->>'total')::bigint)));
 perform set_config('eios.tenant_id',coalesce(prior,''),true);
 return p_value||jsonb_build_object('groups',out);
end $$;
alter function authz.nexloop_workbench_neighbors_by_relation(text,text,jsonb) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_neighbors_by_relation(text,text,jsonb) from public;

create function authz.nexloop_workbench_read(p_digest text,p_world text,p_text text,p_signature text,p_payload text) returns jsonb
 language plpgsql security definer set search_path=pg_catalog,pg_temp set row_security=on as $$
declare c jsonb:=p_payload::jsonb;v jsonb;
begin
 v:=authz.nexloop_workbench_read_before_relation_neighbors(p_digest,p_world,p_text,p_signature,p_payload);
 if c->>'verb' is distinct from 'object_neighbors' or v is null then return v;end if;
 -- Verified by the wrapped function (signature, member, role, Action authority) before it answered; the tenant is the member's.
 return authz.nexloop_workbench_neighbors_by_relation(authz.nexloop_workbench_member(p_digest,p_world)->>'tenant_id',c->>'type_name',v);
end $$;
alter function authz.nexloop_workbench_read(text,text,text,text,text) owner to nexloop_owner;
revoke all on function authz.nexloop_workbench_read(text,text,text,text,text) from public;
grant execute on function authz.nexloop_workbench_read(text,text,text,text,text) to nexloop_api;
