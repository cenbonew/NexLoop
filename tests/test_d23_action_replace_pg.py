"""D23 option B, batch 0 (migration action_version_replace; design §2, §2a): trusted configuration replaces a published Action version
by its next version one for one - the same insert deactivates the previous version, so the workbench's one-active-definition rule
(0125 action_for) keeps holding. As the configurator (session user nexloop_configurator, as control.nexloop_configure_manifest runs)
through a test-only definer that inserts like the manifest does. A replay changes nothing; a gap, another digest, another capability,
an inactive or doubly active previous version is refused and changes nothing; any other inserting path (NX-040 successors) is untouched."""
import json
import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from nexloop_eios.bootstrap import bootstrap

NAME, CAPABILITY = 'nexloop.alert.silence', 'alert.silence'


def definition(tenant, version, *, previous=None, capability=CAPABILITY, digest=None):
    d = {'tenant_id': tenant, 'definition_type': 'action', 'stable_name': NAME, 'version': version, 'status': 'published',
         'contract_digest': digest or f'{version:064x}', 'capability_binding': {'capability_name': capability, 'capability_version': '1.0.0', 'schema_hash': 'a' * 64},
         'input_schema': {} if previous is None else {'type': 'object'}}
    if previous is not None:
        d['previous_version'] = {'tenant_id': tenant, 'definition_type': 'action', 'stable_name': NAME, 'version': previous[0], 'contract_digest': previous[1]}
    return d


@pytest.fixture
def env(admin, pg):
    bootstrap(admin)
    tenant = str(uuid.uuid4())
    admin.execute("insert into control.nexloop_tenants(tenant_id,status) values(%s,'active')", (tenant,))
    admin.execute("""create function public.d23_configure(p_tenant text,p_definition jsonb) returns void language plpgsql security definer
        set search_path=pg_catalog,pg_temp as $$ begin
         perform set_config('eios.tenant_id',p_tenant,true);
         -- catalog-exception: synthetic definition rows for the version replacement trigger (D23 option B)
         insert into control.nexloop_action_definitions values(p_tenant,'real','eios:action:'||(p_definition->>'stable_name')||':'||(p_definition->>'version'),
          p_definition,jsonb_build_object('capability_name',p_definition->'capability_binding'->>'capability_name'),true) on conflict do nothing;
        end $$""")
    admin.execute('alter function public.d23_configure(text,jsonb) owner to nexloop_owner')
    admin.execute('grant execute on function public.d23_configure(text,jsonb) to nexloop_configurator')
    admin.execute('alter role nexloop_configurator login')  # the configurator window opens it for one call on stage
    v1 = definition(tenant, 1)
    # catalog-exception: synthetic definition rows for the version replacement trigger (D23 option B)
    admin.execute("insert into control.nexloop_action_definitions values(%s,'real',%s,%s,%s,true)",
                  (tenant, f'eios:action:{NAME}:1', Jsonb(v1), Jsonb({'capability_name': CAPABILITY})))
    with psycopg.connect(make_conninfo(pg, user='nexloop_configurator'), autocommit=True) as configurator:
        yield {'admin': admin, 'tenant': tenant, 'v1': v1, 'configure': lambda d: configurator.execute('select public.d23_configure(%s,%s)', (tenant, Jsonb(d)))}


def state(f):
    return dict(f['admin'].execute("select resource_id,active from control.nexloop_action_definitions where tenant_id=%s order by 1", (f['tenant'],)).fetchall())


def active_for_capability(f):
    """What 0125 authz.nexloop_workbench_action_for counts: active definitions bound to the capability."""
    return f['admin'].execute("""select count(*) from control.nexloop_action_definitions where tenant_id=%s and world='real' and active
        and definition->'capability_binding'->>'capability_name'=%s""", (f['tenant'], CAPABILITY)).fetchone()[0]


def test_the_next_version_replaces_the_previous_one_for_one_and_a_replay_changes_nothing(env):
    f = env
    v2 = definition(f['tenant'], 2, previous=(1, f['v1']['contract_digest']))
    f['configure'](v2)
    assert state(f) == {f'eios:action:{NAME}:1': False, f'eios:action:{NAME}:2': True} and active_for_capability(f) == 1
    f['configure'](v2)  # the same manifest again
    assert state(f) == {f'eios:action:{NAME}:1': False, f'eios:action:{NAME}:2': True}
    stored = f['admin'].execute("select definition from control.nexloop_action_definitions where resource_id=%s and tenant_id=%s",
                                (f'eios:action:{NAME}:1', f['tenant'])).fetchone()[0]
    assert stored == f['v1']  # published definitions stay as they were: only active moved


@pytest.mark.parametrize('label', ['gap', 'other digest', 'other capability', 'other name', 'previous inactive', 'two active',
                                   'no digest', 'null digests', 'other tenant', 'other type', 'other capability version', 'later version replacing nothing'])
def test_anything_but_a_one_for_one_replacement_is_refused_and_changes_nothing(env, label):
    f = env;t = f['tenant'];digest = f['v1']['contract_digest']
    candidate = definition(t, 2, previous=(1, digest))
    if label == 'gap':
        candidate = definition(t, 3, previous=(1, digest))
    elif label == 'other digest':
        candidate = definition(t, 2, previous=(1, 'b' * 64))
    elif label == 'other capability':
        candidate = definition(t, 2, previous=(1, digest), capability='alert.other')
    elif label == 'other name':
        candidate['previous_version']['stable_name'] = 'nexloop.other.action'
    elif label == 'previous inactive':
        f['admin'].execute("update control.nexloop_action_definitions set active=false where tenant_id=%s", (t,))
    elif label == 'no digest':
        candidate['previous_version'].pop('contract_digest')
    elif label == 'null digests':  # null equal to null is not a match
        f['admin'].execute("delete from control.nexloop_action_definitions where tenant_id=%s", (t,))
        bare = {k: v for k, v in f['v1'].items() if k != 'contract_digest'}
        # catalog-exception: synthetic definition rows for the version replacement trigger (D23 option B)
        f['admin'].execute("insert into control.nexloop_action_definitions values(%s,'real',%s,%s,%s,true)",
                           (t, f'eios:action:{NAME}:1', Jsonb(bare), Jsonb({'capability_name': CAPABILITY})))
        candidate['previous_version']['contract_digest'] = None
    elif label == 'other tenant':
        candidate['previous_version']['tenant_id'] = str(uuid.uuid4())
    elif label == 'other type':
        candidate['previous_version']['definition_type'] = 'function'
    elif label == 'other capability version':
        candidate['capability_binding'] = {**candidate['capability_binding'], 'capability_version': '2.0.0'}
    elif label == 'later version replacing nothing':  # C5: no previous_version, while version 1 is active
        candidate.pop('previous_version')
    elif label == 'two active':
        other = definition(t, 7)
        # catalog-exception: synthetic definition rows for the version replacement trigger (D23 option B)
        f['admin'].execute("insert into control.nexloop_action_definitions values(%s,'real',%s,%s,%s,true)",
                           (t, f'eios:action:{NAME}:7', Jsonb(other), Jsonb({'capability_name': CAPABILITY})))
    before = state(f)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match='action replacement invalid'):
        f['configure'](candidate)
    assert state(f) == before, label


def test_other_inserting_paths_are_untouched(env):
    """An NX-040 successor (inserted by the API's publication, not the configurator) keeps the previous version active, as before."""
    f = env
    v2 = definition(f['tenant'], 2, previous=(1, f['v1']['contract_digest']))
    # catalog-exception: synthetic definition rows for the version replacement trigger (D23 option B)
    f['admin'].execute("insert into control.nexloop_action_definitions values(%s,'real',%s,%s,%s,true)",
                       (f['tenant'], f'eios:action:{NAME}:2', Jsonb(v2), Jsonb({'capability_name': CAPABILITY})))
    assert state(f) == {f'eios:action:{NAME}:1': True, f'eios:action:{NAME}:2': True}


def test_a_first_version_through_the_configurator_is_untouched(env):
    f = env
    first = {**definition(f['tenant'], 1), 'stable_name': 'nexloop.alert.fresh'}
    f['configure'](first)
    assert state(f)['eios:action:nexloop.alert.fresh:1'] is True
    assert json.loads(json.dumps(first))['version'] == 1
