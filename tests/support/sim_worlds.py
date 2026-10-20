"""NX-041 S2 (D10): a sim:/shadow: service credential needs its world registered and active first. Test fixtures write the
registry as trusted configuration does (the governed create Action is tested on its own in test_nx041_sim_worlds_pg)."""
import re


SIM_WORLD = re.compile(r'(sim|shadow):[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


def sim_use_of(world, use='api'):
    """D10: the use column of a credential for this world - api, worker or agent for a simulation world, none for real and test."""
    return use if SIM_WORLD.fullmatch(str(world)) else None


def register_sim_world(admin, tenant, world, purpose='test fixture', use='api'):
    if not SIM_WORLD.fullmatch(str(world)):
        return
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active') on conflict(tenant_id) do nothing", (tenant,))
    admin.execute("""insert into control.nexloop_sim_worlds(world,tenant_id,created_by,purpose,status,expires_at,request_id)
        values(%s,%s,'fixture',%s,'active',clock_timestamp()+interval '30 days','fixture:'||%s) on conflict(world) do nothing""", (world, tenant, purpose, world))
    # D10 (owner: three credentials per world, one per use). A fixture issuing a new one for a use reissues that use's credential: the
    # previous one of the same use (an indexer seeded before a reader, say) is revoked first, as an owner would before the next.
    admin.execute("update authz.nexloop_service_credentials set status='revoked' where worlds=%s and sim_use=%s and status='active'", ([world], use))
