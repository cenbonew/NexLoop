"""Test scaffolding for the Claim auto-write whitelist (migration claim_auto_write). Tests of matching mechanics (apply, conflicts,
reflow) need proposals to apply, as before the whitelist; on stage the owner publishes a manifest. These helpers stand in for that
publication. Sensitive properties stay refused (the trigger skips them; SQL refuses them at use anyway).
tests/test_claim_auto_write_pg.py tests the whitelist itself without these helpers."""


def allow(admin, tenant, pairs, world='real'):
    """Whitelist the given (type, property) pairs for one tenant and world."""
    for type_name, property_name in pairs:
        admin.execute("""insert into control.nexloop_auto_write_properties(tenant_id,world,type_name,property_name,revision,reason,set_by)
            values(%s,%s,%s,%s,1,'synthetic test whitelist','synthetic-test-manifest') on conflict do nothing""", (tenant, world, type_name, property_name))


def allow_published_properties(admin, world='real'):
    """Every property of every object type version published from now on (and already) is whitelisted for its tenant in `world`,
    except a sensitive one: the owner whitelisted the whole published schema."""
    admin.execute("""create or replace function public.nexloop_test_auto_write_published() returns trigger language plpgsql security definer set search_path=pg_catalog as $t$
        begin
         insert into control.nexloop_auto_write_properties(tenant_id,world,type_name,property_name,revision,reason,set_by)
          select new.tenant_id,tg_argv[0],new.type_name,p->>'property_name',1,'synthetic test whitelist','synthetic-test-manifest'
          from jsonb_array_elements(coalesce(new.definition->'properties','[]'::jsonb)) p
          where not control.nexloop_auto_write_sensitive(new.type_name,p->>'property_name')
          on conflict do nothing;
         return new;
        end $t$""")
    admin.execute('drop trigger if exists nexloop_test_auto_write_published on ontology.object_type_versions')
    admin.execute(f"create trigger nexloop_test_auto_write_published after insert on ontology.object_type_versions for each row "
                  f"execute function public.nexloop_test_auto_write_published('{world}')")
    admin.execute("""insert into control.nexloop_auto_write_properties(tenant_id,world,type_name,property_name,revision,reason,set_by)
        select v.tenant_id,%s,v.type_name,p->>'property_name',1,'synthetic test whitelist','synthetic-test-manifest'
        from ontology.object_type_versions v,jsonb_array_elements(coalesce(v.definition->'properties','[]'::jsonb)) p
        where not control.nexloop_auto_write_sensitive(v.type_name,p->>'property_name') on conflict do nothing""", (world,))
